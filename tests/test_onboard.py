# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for the pure logic in `spi onboard`.

These cover the security-relevant string construction (namespace-scoped role
assignment scopes, identity/role names) without touching az/kubectl/gh.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

import spi.onboard as onboard_module
from spi.onboard import (
    DEPLOY_DATA_ACTIONS,
    FEDERATED_CREDENTIAL_LIMIT,
    NO_DATA_ACCESS_IDENTITY_NAME,
    NO_DATA_ACCESS_TOKEN_ENVS,
    OSDU_BRANCHES,
    OnboardInputs,
    _ensure_custom_deploy_role,
    _ensure_flux_read_rbac,
    _gh_delete_variable,
    _gh_get_variable,
    _no_data_access_federated_credentials,
    _remove_no_data_access_federated_credentials,
    _resolve_no_data_access_profile,
    _service_federated_credentials,
    _should_write_secrets,
)

CLUSTER_ID = (
    "/subscriptions/sub-1/resourceGroups/spi-stack-dev3/providers/"
    "Microsoft.ContainerService/managedClusters/spi-stack-dev3"
)


def _inputs() -> OnboardInputs:
    inp = OnboardInputs(
        service="partition",
        repo="my-org/partition",
        aks_cluster="spi-stack-dev3",
        aks_rg="spi-stack-dev3",
        identities_rg="spi-stack-dev3",
        namespace="osdu",
        flux_namespace="osdu-flux",
    )
    inp.cluster_resource_id = CLUSTER_ID
    inp.github_oidc_subject_prefix = "repo:my-org@123/partition@456"
    return inp


def test_identity_and_role_names_are_service_scoped():
    inp = _inputs()
    assert inp.identity_name == "spi-ci-partition"
    assert inp.deploy_role_name == "spi-ci-partition-deploy"
    assert inp.no_data_access_identity_name == NO_DATA_ACCESS_IDENTITY_NAME


def test_no_data_access_profile_is_opt_in_by_service():
    storage = _inputs()
    storage.service = "storage"
    assert storage.uses_no_data_access_identity is True
    assert storage.resolved_no_data_access_token_env == "NO_DATA_ACCESS_TESTER_ACCESS_TOKEN"

    schema = _inputs()
    schema.service = "schema"
    assert schema.uses_no_data_access_identity is False
    assert schema.resolved_no_data_access_token_env == ""

    assert NO_DATA_ACCESS_TOKEN_ENVS == {"storage": "NO_DATA_ACCESS_TESTER_ACCESS_TOKEN"}


def test_no_data_access_profile_allows_explicit_override():
    inp = _inputs()
    inp.no_data_access_token_env = ""
    assert inp.uses_no_data_access_identity is False

    inp.service = "schema"
    inp.no_data_access_token_env = "CUSTOM_NO_ACCESS_TOKEN"
    assert inp.uses_no_data_access_identity is True
    assert inp.resolved_no_data_access_token_env == "CUSTOM_NO_ACCESS_TOKEN"


def test_no_data_access_profile_reuses_existing_repo_value(monkeypatch):
    inp = _inputs()
    inp.service = "workflow"
    monkeypatch.setattr(
        onboard_module,
        "_gh_get_variable",
        lambda _inp, name: "NO_ACCESS_USER_TOKEN" if name == "NO_DATA_ACCESS_TOKEN_ENV" else "",
    )

    _resolve_no_data_access_profile(inp)

    assert inp.uses_no_data_access_identity is True
    assert inp.resolved_no_data_access_token_env == "NO_ACCESS_USER_TOKEN"


def test_namespace_scope_targets_the_service_namespace():
    inp = _inputs()
    assert inp.namespace_scope == f"{CLUSTER_ID}/namespaces/osdu"
    assert inp.flux_namespace_scope == f"{CLUSTER_ID}/namespaces/osdu-flux"


def test_namespace_scope_is_not_cluster_wide():
    # Security: the deploy role must never be assignable at the bare cluster scope.
    inp = _inputs()
    assert inp.namespace_scope != CLUSTER_ID
    assert inp.namespace_scope.endswith("/namespaces/osdu")


def test_deploy_data_actions_are_least_privilege():
    # No wildcard / delete / secrets-read actions in the deploy role.
    blob = " ".join(DEPLOY_DATA_ACTIONS).lower()
    assert "*" not in blob
    assert "secrets" not in blob
    assert "/delete" not in blob
    # Deployments get write (set image); pods/events are read-only.
    assert "apps/deployments/write" in blob
    assert "pods/read" in blob
    assert "events/read" in blob
    # pods/log/read and the Flux CRD read are not registered AKS dataActions, so they are
    # NOT in the Azure role (they would make `az role definition create` fail); Flux read is
    # granted via native k8s RBAC instead.
    assert "pods/log" not in blob
    assert "kustomize" not in blob


def test_custom_role_rehome_preserves_existing_cluster_scope(monkeypatch):
    inp = _inputs()
    old_cluster = (
        "/subscriptions/sub-1/resourceGroups/spi-stack-old/providers/"
        "Microsoft.ContainerService/managedClusters/spi-stack-old"
    )
    existing_role = {
        "id": ("/subscriptions/sub-1/providers/Microsoft.Authorization/roleDefinitions/role-guid"),
        "name": "role-guid",
        "roleName": "spi-ci-partition-deploy",
        "roleType": "CustomRole",
        "assignableScopes": [old_cluster],
    }
    captured = {}

    monkeypatch.setattr(
        onboard_module,
        "_az_json",
        lambda *_args, **_kwargs: [existing_role],
    )

    def capture_role(command, **_kwargs):
        definition = command[command.index("--role-definition") + 1]
        captured["command"] = command
        captured["role"] = json.loads(Path(definition.removeprefix("@")).read_text())

    monkeypatch.setattr(onboard_module, "_run", capture_role)

    _ensure_custom_deploy_role(inp)

    assert "update" in captured["command"]
    assert captured["role"]["id"].endswith("/roleDefinitions/role-guid")
    assert captured["role"]["roleName"] == "spi-ci-partition-deploy"
    assert captured["role"]["assignableScopes"] == [old_cluster, CLUSTER_ID]
    assert captured["role"]["permissions"][0]["dataActions"] == DEPLOY_DATA_ACTIONS


def test_flux_reader_covers_kustomizations_and_helmreleases(monkeypatch):
    inp = _inputs()
    inp.identity_principal_id = "service-principal-object-id"
    manifests = []

    def capture_manifest(command, **_kwargs):
        manifests.append(Path(command[-1]).read_text(encoding="utf-8"))

    monkeypatch.setattr(onboard_module, "_run", capture_manifest)

    _ensure_flux_read_rbac(inp)

    assert len(manifests) == 1
    assert 'apiGroups: ["kustomize.toolkit.fluxcd.io"]' in manifests[0]
    assert 'resources: ["kustomizations"]' in manifests[0]
    assert 'apiGroups: ["helm.toolkit.fluxcd.io"]' in manifests[0]
    assert 'resources: ["helmreleases"]' in manifests[0]


def test_osdu_branch_subjects_cover_the_three_branches():
    assert set(OSDU_BRANCHES) == {"main", "fork_integration", "fork_upstream"}


def test_service_federated_subjects_use_resolved_github_prefix():
    subjects = _service_federated_credentials(_inputs())
    assert subjects["spi-ci-partition-pull-request"] == (
        "repo:my-org@123/partition@456:pull_request"
    )
    assert subjects["spi-ci-partition-branch-main"] == (
        "repo:my-org@123/partition@456:ref:refs/heads/main"
    )


def test_shared_no_data_identity_uses_existing_repo_subjects():
    subjects = _no_data_access_federated_credentials(_inputs())
    assert subjects["spi-no-data-partition-pull-request"] == (
        "repo:my-org@123/partition@456:pull_request"
    )
    assert subjects["spi-no-data-partition-branch-main"] == (
        "repo:my-org@123/partition@456:ref:refs/heads/main"
    )
    assert len(subjects) == 4
    assert FEDERATED_CREDENTIAL_LIMIT // len(subjects) == 5


def test_federated_credential_subject_change_updates_in_place(monkeypatch):
    calls = []
    monkeypatch.setattr(
        onboard_module,
        "_az_json",
        lambda *_args, **_kwargs: [
            {
                "name": "spi-no-data-partition-pull-request",
                "subject": "repo:old-org/partition:pull_request",
            }
        ],
    )
    monkeypatch.setattr(
        onboard_module,
        "_run",
        lambda command, **_kwargs: calls.append(command),
    )

    onboard_module._reconcile_federated_credentials(
        _inputs(),
        NO_DATA_ACCESS_IDENTITY_NAME,
        {"spi-no-data-partition-pull-request": ("repo:my-org@123/partition@456:pull_request")},
    )

    assert len(calls) == 1
    assert "update" in calls[0]
    assert "create" not in calls[0]
    assert "repo:my-org@123/partition@456:pull_request" in calls[0]


def test_federated_credential_limit_fails_before_create(monkeypatch):
    existing = [
        {"name": f"credential-{index}", "subject": f"subject-{index}"}
        for index in range(FEDERATED_CREDENTIAL_LIMIT)
    ]
    monkeypatch.setattr(
        onboard_module,
        "_az_json",
        lambda *_args, **_kwargs: existing,
    )
    monkeypatch.setattr(
        onboard_module,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("must not mutate at the credential limit"),
    )

    with pytest.raises(typer.Exit):
        onboard_module._reconcile_federated_credentials(
            _inputs(),
            NO_DATA_ACCESS_IDENTITY_NAME,
            _no_data_access_federated_credentials(_inputs()),
        )


def test_disabling_profile_removes_repo_federated_credentials(monkeypatch):
    calls = []

    def fake_az_json(args, **_kwargs):
        if args[:2] == ["identity", "show"]:
            return {"clientId": "shared-client", "principalId": "shared-principal"}
        return [
            {
                "name": "spi-no-data-partition-pull-request",
                "subject": "repo:my-org@123/partition@456:pull_request",
            }
        ]

    monkeypatch.setattr(onboard_module, "_az_json", fake_az_json)
    monkeypatch.setattr(
        onboard_module,
        "_run",
        lambda command, **_kwargs: calls.append(command),
    )

    _remove_no_data_access_federated_credentials(_inputs())

    assert len(calls) == 1
    assert "delete" in calls[0]
    assert "spi-no-data-partition-pull-request" in calls[0]


def test_repo_variable_lookup_treats_only_404_as_missing(monkeypatch):
    monkeypatch.setattr(
        onboard_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="gh: variable not found (HTTP 404)"
        ),
    )
    assert _gh_get_variable(_inputs(), "MISSING") == ""


def test_repo_variable_lookup_surfaces_non_404_errors(monkeypatch):
    monkeypatch.setattr(
        onboard_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="gh: service unavailable (HTTP 503)"
        ),
    )
    with pytest.raises(typer.Exit):
        _gh_get_variable(_inputs(), "BROKEN")


def test_repo_variable_delete_attempts_empty_values_and_ignores_404(monkeypatch):
    calls = []
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="gh: variable not found (HTTP 404)",
            ),
        ]
    )
    monkeypatch.setattr(
        onboard_module.subprocess,
        "run",
        lambda *args, **_kwargs: calls.append(args[0]) or next(responses),
    )

    _gh_delete_variable(_inputs(), "EMPTY_BUT_PRESENT")
    _gh_delete_variable(_inputs(), "MISSING")

    assert len(calls) == 2
    assert all("delete" in command for command in calls)


def test_secret_write_policy_rehome_and_idempotency():
    # First onboard: no secret yet -> write.
    assert _should_write_secrets(secret_present=False, is_rehome=False, force=False) is True
    # Idempotent re-run against the same cluster (same identity already set) -> skip.
    assert _should_write_secrets(secret_present=True, is_rehome=False, force=False) is False
    # Re-home onto a new cluster (identity changed) -> rewrite so the secret follows the variable.
    assert _should_write_secrets(secret_present=True, is_rehome=True, force=False) is True
    # Explicit force -> rewrite.
    assert _should_write_secrets(secret_present=True, is_rehome=False, force=True) is True
