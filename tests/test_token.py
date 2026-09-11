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

"""spi token: the cluster-federated mint as the deploy or no-access identity."""

import io
import json
import re
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from spi import deploy, token
from spi.bootstrap import ensure_namespaces
from spi.config import Config
from spi.templates import (
    DEPLOYER_SERVICE_ACCOUNT,
    MEMBER_SERVICE_ACCOUNT,
    NO_ACCESS_SERVICE_ACCOUNT,
    TESTER_NAMESPACE,
    workload_identity_sa,
)

INFRA_DIR = Path(__file__).resolve().parent.parent / "infra"
CLUSTER_CFG = {
    "DEPLOY_IDENTITY_CLIENT_ID": "deployer-client-id",
    "MEMBER_IDENTITY_CLIENT_ID": "member-client-id",
    "NO_ACCESS_IDENTITY_CLIENT_ID": "no-access-client-id",
    "AZURE_TENANT_ID": "tenant-id",
}


class _Response(io.BytesIO):
    """urlopen's context-managed response, minus the socket."""


def _kubectl_ok(argv, **kwargs):
    return CompletedProcess(argv, 0, stdout="projected-sa-token\n", stderr="")


def _mint(
    no_access=False,
    resource=None,
    *,
    caller="deploy",
    entra=None,
    kubectl=_kubectl_ok,
    aad="uami-id",
):
    entra = entra if entra is not None else {"access_token": "bearer", "expires_on": "1"}
    captured = {}

    def urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = dict(
            pair.split("=", 1) for pair in urllib.parse.unquote(request.data.decode()).split("&")
        )
        return _Response(json.dumps(entra).encode())

    with (
        patch("spi.token.read_cluster_config", return_value=CLUSTER_CFG),
        patch("spi.token._read_osdu_config", return_value={"AAD_CLIENT_ID": aad}),
        patch("spi.token.read_workload_identity_client_id", return_value="uami-id"),
        patch("spi.token.run_process", side_effect=kubectl) as run_process,
        patch("spi.token.urllib.request.urlopen", side_effect=urlopen),
    ):
        minted = token.mint_token(caller=caller, no_access=no_access, resource=resource)
    captured["kubectl"] = run_process.call_args.args[0]
    return minted, captured


def test_mints_as_the_deploy_identity_through_its_service_account():
    minted, captured = _mint()

    assert captured["kubectl"][:4] == ["kubectl", "create", "token", DEPLOYER_SERVICE_ACCOUNT]
    assert captured["kubectl"][captured["kubectl"].index("-n") + 1] == TESTER_NAMESPACE
    assert "--audience" in captured["kubectl"]
    assert captured["url"] == "https://login.microsoftonline.com/tenant-id/oauth2/token"
    assert captured["body"]["client_id"] == "deployer-client-id"
    assert captured["body"]["client_assertion"] == "projected-sa-token"
    assert captured["body"]["resource"] == "https://management.azure.com"
    assert minted.access_token == "bearer"
    assert minted.as_dict()["service_account"] == f"{TESTER_NAMESPACE}/{DEPLOYER_SERVICE_ACCOUNT}"


def test_no_access_selects_the_other_identity_and_account():
    minted, captured = _mint(no_access=True)

    assert captured["kubectl"][3] == NO_ACCESS_SERVICE_ACCOUNT
    assert captured["body"]["client_id"] == "no-access-client-id"
    assert minted.client_id == "no-access-client-id"


def test_member_selects_the_member_identity_and_account():
    minted, captured = _mint(caller="member")

    assert captured["kubectl"][3] == MEMBER_SERVICE_ACCOUNT
    assert captured["body"]["client_id"] == "member-client-id"
    assert minted.client_id == "member-client-id"


def test_audience_follows_an_aad_client_id_override():
    """An operator override to an app registration is mintable; spi info
    publishes it as token_audience and the mint must agree."""
    _, captured = _mint(aad="app-registration-id")

    assert captured["body"]["resource"] == "app-registration-id"


def test_explicit_resource_wins():
    _, captured = _mint(resource="https://vault.azure.net")

    assert captured["body"]["resource"] == "https://vault.azure.net"


def test_missing_cluster_config_names_spi_up():
    with patch("spi.token.read_cluster_config", return_value={}):
        with pytest.raises(token.TokenError, match="DEPLOY_IDENTITY_CLIENT_ID, AZURE_TENANT_ID"):
            token.mint_token()


def test_missing_tenant_alone_is_named_alone():
    partial = {k: v for k, v in CLUSTER_CFG.items() if k != "AZURE_TENANT_ID"}
    with patch("spi.token.read_cluster_config", return_value=partial):
        with pytest.raises(token.TokenError, match="carries no AZURE_TENANT_ID;"):
            token.mint_token()


def test_missing_member_identity_names_the_key_not_the_deploy_identity():
    partial = {k: v for k, v in CLUSTER_CFG.items() if k != "MEMBER_IDENTITY_CLIENT_ID"}
    with patch("spi.token.read_cluster_config", return_value=partial):
        with pytest.raises(token.TokenError, match="carries no MEMBER_IDENTITY_CLIENT_ID;") as exc:
            token.mint_token(caller="member")
    assert "deploy identity" not in str(exc.value)


def test_a_socket_timeout_is_a_token_error():
    with (
        patch("spi.token.read_cluster_config", return_value=CLUSTER_CFG),
        patch("spi.token._read_osdu_config", return_value={}),
        patch("spi.token.read_workload_identity_client_id", return_value=""),
        patch("spi.token.run_process", side_effect=_kubectl_ok),
        patch("spi.token.urllib.request.urlopen", side_effect=TimeoutError("timed out")),
    ):
        with pytest.raises(token.TokenError, match="Could not reach Entra: timed out"):
            token.mint_token()


def test_missing_service_account_names_spi_up():
    def kubectl(argv, **kwargs):
        return CompletedProcess(argv, 1, stdout="", stderr='serviceaccounts "x" not found')

    with pytest.raises(token.TokenError, match=f"{TESTER_NAMESPACE}/{DEPLOYER_SERVICE_ACCOUNT}"):
        _mint(kubectl=kubectl)


def test_missing_federated_credential_names_spi_up():
    error = urllib.error.HTTPError(
        "https://login.microsoftonline.com/tenant-id/oauth2/token",
        400,
        "Bad Request",
        Message(),
        io.BytesIO(
            json.dumps({"error_description": "AADSTS70021: No matching federated"}).encode()
        ),
    )
    with (
        patch("spi.token.read_cluster_config", return_value=CLUSTER_CFG),
        patch("spi.token._read_osdu_config", return_value={}),
        patch("spi.token.read_workload_identity_client_id", return_value=""),
        patch("spi.token.run_process", side_effect=_kubectl_ok),
        patch("spi.token.urllib.request.urlopen", side_effect=error),
    ):
        with pytest.raises(token.TokenError, match="no federated credential.*spi up"):
            token.mint_token()


def test_bicep_federates_both_identities_to_the_tester_accounts():
    """The CLI constants and the Bicep subjects must agree, or spi token
    exchanges a projected token Entra has never heard of."""
    source = (INFRA_DIR / "modules" / "identity.bicep").read_text()

    assert f"param testerNamespace string = '{TESTER_NAMESPACE}'" in source
    assert f"param deployerServiceAccountName string = '{DEPLOYER_SERVICE_ACCOUNT}'" in source
    assert f"param memberServiceAccountName string = '{MEMBER_SERVICE_ACCOUNT}'" in source
    assert f"param noAccessServiceAccountName string = '{NO_ACCESS_SERVICE_ACCOUNT}'" in source
    subjects = re.findall(
        r"subject: 'system:serviceaccount:\$\{testerNamespace\}:\$\{(\w+)\}'", source
    )
    assert sorted(subjects) == [
        "deployerServiceAccountName",
        "memberServiceAccountName",
        "noAccessServiceAccountName",
    ]
    for parent in ("deployIdentity", "noAccessIdentity"):
        assert f"parent: {parent}\n  name: 'cluster-${{testerNamespace}}'" in source


def test_deploy_applies_annotated_tester_accounts():
    outputs = {
        "identity_client_id": "uami-id",
        "tenant_id": "tenant-id",
        "deploy_identity_client_id": "deployer-client-id",
        "member_identity_client_id": "member-client-id",
        "no_access_identity_client_id": "no-access-client-id",
    }
    with (
        patch("spi.deploy.display_yaml"),
        patch("spi.deploy.kubectl_apply_yaml") as apply_yaml,
    ):
        deploy._create_osdu_config(Config.from_env("dev1"), outputs)
    applied = [call.args[0] for call in apply_yaml.call_args_list]

    deployer = next(y for y in applied if f"name: {DEPLOYER_SERVICE_ACCOUNT}\n" in y)
    assert f"namespace: {TESTER_NAMESPACE}\n" in deployer
    assert 'azure.workload.identity/client-id: "deployer-client-id"' in deployer
    no_access = next(y for y in applied if f"name: {NO_ACCESS_SERVICE_ACCOUNT}\n" in y)
    assert 'azure.workload.identity/client-id: "no-access-client-id"' in no_access
    member = next(y for y in applied if f"name: {MEMBER_SERVICE_ACCOUNT}\n" in y)
    assert 'azure.workload.identity/client-id: "member-client-id"' in member


def test_deploy_skips_tester_accounts_without_identity_outputs():
    with (
        patch("spi.deploy.display_yaml"),
        patch("spi.deploy.kubectl_apply_yaml") as apply_yaml,
    ):
        deploy._create_osdu_config(Config.from_env("dev1"), {"identity_client_id": "uami-id"})
    applied = "".join(call.args[0] for call in apply_yaml.call_args_list)

    assert DEPLOYER_SERVICE_ACCOUNT not in applied
    assert applied.count("kind: ServiceAccount") == 2


def test_workload_identity_sa_default_name_is_unchanged():
    assert "name: workload-identity-sa\n" in workload_identity_sa("osdu", "c", "t")


def test_bootstrap_creates_the_test_namespace():
    with (
        patch("spi.bootstrap.kubectl_json", return_value=None),
        patch("spi.bootstrap.run_process") as run_process,
        patch("spi.bootstrap.kubectl_apply_yaml"),
    ):
        ensure_namespaces("asm-1-30")
    created = [call.args[0][3] for call in run_process.call_args_list]

    assert TESTER_NAMESPACE in created


def test_namespaces_component_declares_the_test_namespace():
    source = (
        Path(__file__).resolve().parent.parent
        / "software"
        / "components"
        / "namespaces"
        / "namespaces.yaml"
    ).read_text()

    assert f"name: {TESTER_NAMESPACE}\n" in source
    block = source.split(f"name: {TESTER_NAMESPACE}\n", 1)[1].split("---", 1)[0]
    assert "istio.io/rev" not in block
