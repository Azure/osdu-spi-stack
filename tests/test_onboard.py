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

"""spi onboard: plan by default, write in phases, project the roster."""

import json
import subprocess
from unittest.mock import patch

import pytest

from spi import onboard, pins
from spi.onboard import (
    DEPLOY_ENVIRONMENT,
    GITHUB_AUDIENCE,
    GITHUB_ISSUER,
    MAX_CREDENTIALS,
    OnboardError,
    Target,
    apply_plan,
    apply_remove,
    credential_subject,
    list_trust,
    plan_onboard,
    plan_remove,
)
from spi.pins import TRUSTED_REPOS_ANNOTATION, apply_image_lock

IDENTITY = "spi-stack-dev1-deployer"
RG = "spi-stack-dev1"
VALUES = {
    "AZURE_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
    "AZURE_TENANT_ID": "22222222-2222-2222-2222-222222222222",
    "AZURE_SUBSCRIPTION_ID": "33333333-3333-3333-3333-333333333333",
    "SPI_STACK_RESOURCE_GROUP": RG,
    "SPI_STACK_CLUSTER": "spi-stack-dev1",
}


def target(profile="core", values=None) -> Target:
    return Target(
        env="dev1",
        profile=profile,
        identity_name=IDENTITY,
        resource_group=RG,
        values=dict(VALUES if values is None else values),
    )


class FakeWorld:
    """GitHub, the identity, and the lock, behind the two seams onboard uses."""

    def __init__(self):
        self.repos = {"acme/osdu-spi-partition": "Acme/osdu-spi-partition"}
        self.environments: dict[str, dict] = {}
        self.variables: dict[str, dict[str, str]] = {}
        self.secrets: dict[str, set[str]] = {}
        self.credentials: list[dict] = []
        self.lock: dict | None = {
            "metadata": {"annotations": {}, "resourceVersion": "1"},
            "data": {"PARTITION_IMAGE": "x"},
        }
        self.org_visibility: dict[str, str] = {}
        self.writes: list[list[str]] = []
        self.failures: dict[str, str] = {}
        self.lock_writes = 0

    # -- helpers -----------------------------------------------------------
    def trust(self, service, repo, issuer=GITHUB_ISSUER, audiences=(GITHUB_AUDIENCE,)):
        self.credentials.append(
            {
                "name": f"fork-{service}",
                "issuer": issuer,
                "subject": credential_subject(repo),
                "audiences": list(audiences),
            }
        )

    def protect(self, repo, branches=("main", "fork_integration"), custom=True, extra=()):
        policies = [{"id": i + 1, "name": b, "type": "branch"} for i, b in enumerate(branches)]
        for j, (name, kind) in enumerate(extra):
            policies.append({"id": 100 + j, "name": name, "type": kind})
        self.environments[repo] = {"custom": custom, "policies": policies}

    def stamp(self, where, values=VALUES):
        self.secrets[where] = {"AZURE_CLIENT_ID"}
        self.variables[where] = {k: v for k, v in values.items() if k != "AZURE_CLIENT_ID"}

    def project(self, roster):
        assert self.lock is not None
        self.lock["metadata"]["annotations"][TRUSTED_REPOS_ANNOTATION] = json.dumps(
            roster, sort_keys=True
        )

    def projection(self):
        assert self.lock is not None
        raw = self.lock["metadata"]["annotations"].get(TRUSTED_REPOS_ANNOTATION, "")
        return json.loads(raw) if raw else {}

    # -- seams -------------------------------------------------------------
    def run_command(self, argv, **kwargs):
        key = " ".join(argv[:4])
        for prefix, message in self.failures.items():
            if " ".join(argv).startswith(prefix):
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr=message)
        if argv[:2] == ["gh", "api"]:
            return self._gh_api(argv)
        if argv[:3] == ["gh", "variable", "list"]:
            where = argv[4]
            payload = []
            for k, v in self.variables.get(where, {}).items():
                entry = {"name": k, "value": v}
                if argv[3] == "--org":
                    assert argv[-1] == "name,value,visibility", argv
                    entry["visibility"] = self.org_visibility.get(k, "all")
                payload.append(entry)
            return _ok(argv, payload)
        if argv[:3] == ["gh", "secret", "list"]:
            where = argv[4]
            return _ok(argv, [{"name": k} for k in self.secrets.get(where, ())])
        if argv[:3] == ["gh", "secret", "set"]:
            self.writes.append(argv)
            self.secrets.setdefault(argv[5], set()).add(argv[3])
            return _ok(argv, None)
        if argv[:3] == ["gh", "variable", "set"]:
            self.writes.append(argv)
            self.variables.setdefault(argv[5], {})[argv[3]] = argv[-1]
            return _ok(argv, None)
        if argv[:4] == ["az", "identity", "federated-credential", "list"]:
            return _ok(argv, list(self.credentials))
        if argv[:4] in (
            ["az", "identity", "federated-credential", "create"],
            ["az", "identity", "federated-credential", "update"],
        ):
            self.writes.append(argv)
            opts = dict(zip(argv[4::2], argv[5::2]))
            self.credentials = [c for c in self.credentials if c["name"] != opts["--name"]]
            self.credentials.append(
                {
                    "name": opts["--name"],
                    "issuer": opts["--issuer"],
                    "subject": opts["--subject"],
                    "audiences": [opts["--audiences"]],
                }
            )
            return _ok(argv, None)
        if argv[:4] == ["az", "identity", "federated-credential", "delete"]:
            self.writes.append(argv)
            name = argv[argv.index("--name") + 1]
            self.credentials = [c for c in self.credentials if c["name"] != name]
            return _ok(argv, None)
        raise AssertionError(f"unexpected command {key}")

    def _gh_api(self, argv):
        method = "GET"
        if "--method" in argv:
            method = argv[argv.index("--method") + 1]
        path = next(a for a in argv[2:] if a.startswith("repos/"))
        parts = path.split("/")
        repo = f"{parts[1]}/{parts[2]}"
        if len(parts) == 3:
            canonical = self.repos.get(repo.lower())
            if canonical is None:
                return _fail(argv, "gh: Not Found (HTTP 404)")
            return _ok(argv, {"full_name": canonical})
        env = self.environments.get(repo)
        if parts[3:] == ["environments", DEPLOY_ENVIRONMENT]:
            if method == "PUT":
                self.writes.append(argv)
                self.environments[repo] = {"custom": True, "policies": []}
                return _ok(argv, {})
            if env is None:
                return _fail(argv, "gh: Not Found (HTTP 404)")
            policy = {"custom_branch_policies": env["custom"], "protected_branches": False}
            return _ok(argv, {"name": DEPLOY_ENVIRONMENT, "deployment_branch_policy": policy})
        if parts[3:5] == ["environments", DEPLOY_ENVIRONMENT] and parts[5] == (
            "deployment-branch-policies"
        ):
            assert env is not None
            if method == "DELETE":
                self.writes.append(argv)
                env["policies"] = [p for p in env["policies"] if p["id"] != int(parts[6])]
                return _ok(argv, None)
            if method == "POST":
                self.writes.append(argv)
                name = argv[argv.index("-f") + 1].split("=", 1)[1]
                env["policies"].append(
                    {"id": len(env["policies"]) + 1, "name": name, "type": "branch"}
                )
                return _ok(argv, {})
            assert "--paginate" in argv and "--slurp" in argv, argv
            pages = [env["policies"][i : i + 2] for i in range(0, len(env["policies"]), 2)]
            return _ok(argv, [{"branch_policies": page} for page in pages or [[]]])
        raise AssertionError(f"unexpected gh api path {path}")

    def read_lock(self, required=True):
        if self.lock is None and required:
            raise pins.PinError("missing")
        return json.loads(json.dumps(self.lock)) if self.lock is not None else None

    def mutate_lock(self, mutator, description, max_attempts=5):
        self.lock_writes += 1
        desired = mutator(self.read_lock(required=False))
        assert self.lock is not None
        self.lock = {
            "metadata": {
                "annotations": desired["metadata"]["annotations"],
                "resourceVersion": str(int(self.lock["metadata"]["resourceVersion"]) + 1),
            },
            "data": desired["data"],
        }
        return self.lock


def _ok(argv, payload):
    return subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps(payload) if payload is not None else "", stderr=""
    )


def _fail(argv, message):
    return subprocess.CompletedProcess(argv, 1, stdout="", stderr=message)


@pytest.fixture
def world(monkeypatch):
    fake = FakeWorld()
    monkeypatch.setattr(onboard, "run_command", fake.run_command)
    monkeypatch.setattr(onboard, "read_lock", fake.read_lock)
    monkeypatch.setattr(onboard, "mutate_lock", fake.mutate_lock)
    monkeypatch.setattr(onboard.shutil, "which", lambda name: "/usr/bin/gh")
    return fake


def _states(rows):
    return {row.item: row.state for row in rows}


def _phases(plan):
    return [step.phase for step in plan.steps]


class TestRefusalsBeforeWrites:
    def test_only_core_environments_have_a_deploy_target(self, world):
        with pytest.raises(OnboardError, match="profile 'minimal'"):
            plan_onboard(target(profile="minimal"), "partition", "acme/osdu-spi-partition")
        assert world.writes == []

    def test_a_cluster_without_identity_values_cannot_onboard(self, world):
        values = dict(VALUES, AZURE_CLIENT_ID="")
        with pytest.raises(OnboardError, match="AZURE_CLIENT_ID"):
            plan_onboard(target(values=values), "partition", "acme/osdu-spi-partition")

    def test_unknown_service_and_the_loader_are_refused(self, world):
        with pytest.raises(OnboardError, match="Unknown service 'nope'"):
            plan_onboard(target(), "nope", "acme/osdu-spi-partition")
        with pytest.raises(OnboardError, match="Unknown service 'schema-load'"):
            plan_onboard(target(), "schema-load", "acme/osdu-spi-partition")

    def test_repo_must_look_like_owner_slash_name(self, world):
        with pytest.raises(OnboardError, match="<owner>/<name>"):
            plan_onboard(target(), "partition", "../evil")
        with pytest.raises(OnboardError, match="<owner>/<name>"):
            plan_onboard(target(), "partition", "acme")

    def test_a_repository_github_cannot_find_is_refused(self, world):
        with pytest.raises(OnboardError, match="not found on GitHub"):
            plan_onboard(target(), "partition", "acme/missing")

    def test_a_repository_already_backing_another_service_is_refused(self, world):
        world.trust("storage", "Acme/osdu-spi-partition")
        with pytest.raises(OnboardError, match="already backs storage"):
            plan_onboard(target(), "partition", "acme/osdu-spi-partition")

    def test_the_refusal_ignores_casing_on_hand_made_credentials(self, world):
        world.trust("storage", "acme/OSDU-SPI-PARTITION")
        with pytest.raises(OnboardError, match="already backs storage"):
            plan_onboard(target(), "partition", "acme/osdu-spi-partition")

    def test_org_must_own_the_repository(self, world):
        with pytest.raises(OnboardError, match="--org Other does not own"):
            plan_onboard(target(), "partition", "acme/osdu-spi-partition", org="Other")
        assert world.writes == []

    def test_remove_refuses_an_unknown_service(self, world):
        with pytest.raises(OnboardError, match="Unknown service 'partiton'"):
            plan_remove(target(), "partiton")

    def test_the_twenty_first_credential_is_refused(self, world):
        for i in range(MAX_CREDENTIALS):
            world.trust(f"svc{i}", f"Acme/fork{i}")
        with pytest.raises(OnboardError, match=f"already holds {MAX_CREDENTIALS}"):
            plan_onboard(target(), "partition", "acme/osdu-spi-partition")

    def test_a_trusted_service_at_the_cap_can_still_be_repaired(self, world):
        for i in range(MAX_CREDENTIALS - 1):
            world.trust(f"svc{i}", f"Acme/fork{i}")
        world.trust("partition", "Acme/osdu-spi-partition")
        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition")
        assert plan.repo == "Acme/osdu-spi-partition"

    def test_unreadable_protection_rules_block_rather_than_pass(self, world):
        world.failures["gh api repos/Acme/osdu-spi-partition/environments"] = "HTTP 403"
        with pytest.raises(OnboardError, match="Could not read environment spi-stack"):
            plan_onboard(target(), "partition", "acme/osdu-spi-partition")

    def test_a_service_never_trusted_needs_a_repo(self, world):
        with pytest.raises(OnboardError, match="pass --repo"):
            plan_onboard(target(), "partition", "")


class TestPlanning:
    def test_a_fresh_repository_plans_every_phase_and_writes_nothing(self, world):
        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition")

        assert plan.repo == "Acme/osdu-spi-partition"
        assert _phases(plan) == ["repository"] * 8 + ["azure", "cluster"]
        assert world.writes == []
        assert world.lock_writes == 0
        states = _states(plan.rows)
        assert states["spi-stack environment on Acme/osdu-spi-partition"] == "missing"
        assert states["AZURE_CLIENT_ID on Acme/osdu-spi-partition"] == "missing"
        assert states["fork-partition on spi-stack-dev1-deployer"] == "missing"
        assert states[f"{TRUSTED_REPOS_ANNOTATION} on osdu-image-lock"] == "missing"

    def test_the_plan_carries_the_canonical_casing_into_every_command(self, world):
        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition")
        joined = [" ".join(step.argv) for step in plan.steps]
        assert all("acme/osdu-spi-partition" not in line for line in joined)
        credential = next(s for s in plan.steps if s.phase == "azure")
        assert "repo:Acme/osdu-spi-partition:environment:spi-stack" in credential.argv
        assert GITHUB_ISSUER in credential.argv and GITHUB_AUDIENCE in credential.argv

    def test_a_fully_onboarded_repository_reports_exists_and_plans_only_the_secret(self, world):
        world.protect("Acme/osdu-spi-partition")
        world.stamp("Acme/osdu-spi-partition")
        world.trust("partition", "Acme/osdu-spi-partition")
        world.project({"partition": "Acme/osdu-spi-partition"})

        plan = plan_onboard(target(), "partition", "")

        states = _states(plan.rows)
        assert states["spi-stack environment on Acme/osdu-spi-partition"] == "correct"
        assert states["AZURE_CLIENT_ID on Acme/osdu-spi-partition"] == "unverified"
        assert states["SPI_STACK_CLUSTER on Acme/osdu-spi-partition"] == "correct"
        assert states["fork-partition on spi-stack-dev1-deployer"] == "correct"
        assert states[f"{TRUSTED_REPOS_ANNOTATION} on osdu-image-lock"] == "correct"
        assert [s.argv[:3] for s in plan.steps] == [["gh", "secret", "set"]]

    def test_drift_is_named_per_row(self, world):
        world.protect("Acme/osdu-spi-partition", branches=("main",))
        world.stamp("Acme/osdu-spi-partition", dict(VALUES, SPI_STACK_CLUSTER="old-cluster"))
        world.trust("partition", "acme/osdu-spi-partition")
        world.project({"partition": "acme/osdu-spi-partition"})

        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition")

        rows = {row.item: row for row in plan.rows}
        env_row = rows["spi-stack environment on Acme/osdu-spi-partition"]
        assert (env_row.state, env_row.detail) == ("drifted", "missing fork_integration")
        cluster_row = rows["SPI_STACK_CLUSTER on Acme/osdu-spi-partition"]
        assert (cluster_row.state, cluster_row.detail) == ("drifted", "is 'old-cluster'")
        assert rows["fork-partition on spi-stack-dev1-deployer"].state == "drifted"
        assert rows[f"{TRUSTED_REPOS_ANNOTATION} on osdu-image-lock"].state == "drifted"
        assert [s.argv[:3] for s in plan.steps if s.phase == "azure"] == [
            ["az", "identity", "federated-credential"]
        ]
        assert "update" in next(s.argv for s in plan.steps if s.phase == "azure")

    def test_a_wildcard_policy_is_drift_and_its_removal_is_planned(self, world):
        world.protect("Acme/osdu-spi-partition", extra=(("release/*", "branch"),))
        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition")
        row = next(r for r in plan.rows if r.item.startswith("spi-stack environment"))
        assert (row.state, row.detail) == ("drifted", "admits release/* (branch)")
        deletes = [s.argv for s in plan.steps if "DELETE" in s.argv]
        assert deletes and deletes[0][-1].endswith("/deployment-branch-policies/100")
        assert plan.protection is not None and not plan.protection.satisfied

    def test_policies_on_a_later_page_still_count(self, world):
        world.protect(
            "Acme/osdu-spi-partition",
            extra=(("a/*", "branch"), ("b/*", "branch"), ("c/*", "branch")),
        )
        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition")
        row = next(r for r in plan.rows if r.item.startswith("spi-stack environment"))
        assert row.detail == "admits a/* (branch), b/* (branch), c/* (branch)"

    def test_a_tag_policy_named_main_does_not_count_as_the_branch(self, world):
        world.protect(
            "Acme/osdu-spi-partition", branches=("fork_integration",), extra=(("main", "tag"),)
        )
        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition")
        row = next(r for r in plan.rows if r.item.startswith("spi-stack environment"))
        assert row.detail == "missing main; admits main (tag)"

    def test_write_removes_the_extra_policy_before_enabling_trust(self, world):
        world.protect("Acme/osdu-spi-partition", extra=(("release/*", "branch"),))
        apply_plan(plan_onboard(target(), "partition", "acme/osdu-spi-partition"))
        names = [p["name"] for p in world.environments["Acme/osdu-spi-partition"]["policies"]]
        assert names == ["main", "fork_integration"]
        assert len(world.credentials) == 1

    def test_org_values_are_planned_at_organization_level(self, world):
        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition", org="Acme")
        value_steps = [s.argv for s in plan.steps if s.argv[1] in ("secret", "variable")]
        assert all("--org" in argv and "Acme" in argv for argv in value_steps)
        assert all("--visibility" in argv for argv in value_steps)
        assert _states(plan.rows)["AZURE_CLIENT_ID on Acme"] == "missing"

    def test_an_org_variable_the_fork_cannot_see_is_drift(self, world):
        world.stamp("Acme")
        world.org_visibility["SPI_STACK_CLUSTER"] = "selected"
        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition", org="Acme")
        row = next(r for r in plan.rows if r.item == "SPI_STACK_CLUSTER on Acme")
        assert (row.state, row.detail) == ("drifted", "is 'spi-stack-dev1 (visibility selected)'")
        assert _states(plan.rows)["SPI_STACK_RESOURCE_GROUP on Acme"] == "correct"
        sets = [s.argv[3] for s in plan.steps if s.argv[:3] == ["gh", "variable", "set"]]
        assert sets == ["SPI_STACK_CLUSTER"]

    def test_skip_repo_leaves_github_out_but_still_reports_protection(self, world):
        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition", skip_repo=True)
        assert _phases(plan) == ["azure", "cluster"]
        assert _states(plan.rows)["spi-stack environment on Acme/osdu-spi-partition"] == "missing"
        assert not any("AZURE_CLIENT_ID" in row.item for row in plan.rows)

    def test_a_corrupt_projection_is_drift_the_identity_overwrites(self, world):
        world.protect("Acme/osdu-spi-partition")
        world.lock["metadata"]["annotations"][TRUSTED_REPOS_ANNOTATION] = "{not json"

        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition")
        assert _states(plan.rows)[f"{TRUSTED_REPOS_ANNOTATION} on osdu-image-lock"] == "missing"
        apply_plan(plan)

        assert world.projection() == {"partition": "Acme/osdu-spi-partition"}

    def test_a_missing_lock_is_an_error_not_an_empty_projection(self, world):
        world.lock = None
        with pytest.raises(OnboardError, match="osdu-image-lock ConfigMap is missing"):
            plan_onboard(target(), "partition", "acme/osdu-spi-partition")


class TestApplying:
    def test_write_runs_the_phases_in_order_and_reports_the_end_state(self, world):
        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition")

        rows = apply_plan(plan)

        kinds = [" ".join(w[:3]) for w in world.writes]
        assert kinds[0] == "gh api --method"
        assert world.writes[0][-4:] == [
            "-F",
            "deployment_branch_policy[protected_branches]=false",
            "-F",
            "deployment_branch_policy[custom_branch_policies]=true",
        ]
        assert all("--repo" in w and "Acme/osdu-spi-partition" in w for w in world.writes[3:8])
        assert kinds.index("az identity federated-credential") > kinds.index("gh variable set")
        assert [p["name"] for p in world.environments["Acme/osdu-spi-partition"]["policies"]] == [
            "main",
            "fork_integration",
        ]
        assert world.variables["Acme/osdu-spi-partition"]["SPI_STACK_CLUSTER"] == "spi-stack-dev1"
        assert (
            world.credentials[0]["subject"] == "repo:Acme/osdu-spi-partition:environment:spi-stack"
        )
        assert world.projection() == {"partition": "Acme/osdu-spi-partition"}
        assert world.lock["data"] == {"PARTITION_IMAGE": "x"}
        states = _states(rows)
        assert set(states.values()) == {"correct"}
        assert next(r.detail for r in rows if r.item.startswith("AZURE_CLIENT_ID")) == "stamped"

    def test_trust_is_never_enabled_without_the_protection_rules(self, world):
        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition", skip_repo=True)

        with pytest.raises(OnboardError, match="does not protect spi-stack") as exc:
            apply_plan(plan)

        assert "Pending: azure, cluster" in str(exc.value)
        assert world.credentials == []
        assert world.projection() == {}

    def test_a_failed_credential_write_names_completed_and_pending_phases(self, world):
        world.protect("Acme/osdu-spi-partition")
        world.failures["az identity federated-credential create"] = "AuthorizationFailed"
        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition")

        with pytest.raises(OnboardError, match="Completed: repository. Pending: azure, cluster"):
            apply_plan(plan)

        assert world.projection() == {}
        assert "AZURE_CLIENT_ID" in world.secrets["Acme/osdu-spi-partition"]

    def test_rerunning_after_a_failure_resumes_from_observed_state(self, world):
        world.protect("Acme/osdu-spi-partition")
        world.failures["az identity federated-credential create"] = "AuthorizationFailed"
        with pytest.raises(OnboardError):
            apply_plan(plan_onboard(target(), "partition", "acme/osdu-spi-partition"))
        world.failures.clear()
        first_writes = len(world.writes)

        apply_plan(plan_onboard(target(), "partition", "acme/osdu-spi-partition"))

        later = [" ".join(w[:3]) for w in world.writes[first_writes:]]
        assert later == ["gh secret set", "az identity federated-credential"]
        assert world.projection() == {"partition": "Acme/osdu-spi-partition"}

    def test_a_busy_identity_is_retried_after_a_roster_re_read(self, world, monkeypatch):
        world.protect("Acme/osdu-spi-partition")
        slept = []
        monkeypatch.setattr(onboard.time, "sleep", slept.append)
        world.failures["az identity federated-credential create"] = "Conflict (409): busy"
        creates = []
        real = world.run_command

        def flaky(argv, **kwargs):
            if argv[:4] == ["az", "identity", "federated-credential", "create"]:
                creates.append(argv)
                if len(creates) == 2:
                    world.failures.clear()
            return real(argv, **kwargs)

        monkeypatch.setattr(onboard, "run_command", flaky)

        apply_plan(plan_onboard(target(), "partition", "acme/osdu-spi-partition"))

        assert len(creates) == 2 and slept == [5]
        assert world.projection() == {"partition": "Acme/osdu-spi-partition"}

    def test_a_competitor_finishing_the_same_write_ends_the_retry(self, world, monkeypatch):
        world.protect("Acme/osdu-spi-partition")
        monkeypatch.setattr(onboard.time, "sleep", lambda s: None)
        world.failures["az identity federated-credential create"] = "Conflict (409): busy"
        real = world.run_command

        def competitor_wins(argv, **kwargs):
            result = real(argv, **kwargs)
            if argv[:4] == ["az", "identity", "federated-credential", "create"]:
                world.failures.clear()
                world.trust("partition", "Acme/osdu-spi-partition")
            return result

        monkeypatch.setattr(onboard, "run_command", competitor_wins)

        apply_plan(plan_onboard(target(), "partition", "acme/osdu-spi-partition"))

        assert [c["name"] for c in world.credentials] == ["fork-partition"]

    def test_a_non_conflict_failure_is_not_retried(self, world, monkeypatch):
        world.protect("Acme/osdu-spi-partition")
        monkeypatch.setattr(onboard.time, "sleep", lambda s: pytest.fail("slept"))
        world.failures["az identity federated-credential create"] = "AuthorizationFailed"
        with pytest.raises(OnboardError, match="AuthorizationFailed"):
            apply_plan(plan_onboard(target(), "partition", "acme/osdu-spi-partition"))

    def test_a_credential_revoked_between_plan_and_write_is_restored(self, world):
        world.protect("Acme/osdu-spi-partition")
        world.stamp("Acme/osdu-spi-partition")
        world.trust("partition", "Acme/osdu-spi-partition")
        world.project({"partition": "Acme/osdu-spi-partition"})
        plan = plan_onboard(target(), "partition", "")
        assert not any(s.phase == "azure" for s in plan.steps)
        world.credentials.clear()

        apply_plan(plan)

        assert [c["name"] for c in world.credentials] == ["fork-partition"]

    def test_a_competitor_landing_mid_apply_is_reported_correct_not_drifted(
        self, world, monkeypatch
    ):
        world.protect("Acme/osdu-spi-partition")
        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition")
        real = world.run_command

        def competitor(argv, **kwargs):
            result = real(argv, **kwargs)
            if argv[:4] == ["az", "identity", "federated-credential", "create"]:
                world.trust("storage", "Acme/osdu-spi-storage")
            return result

        monkeypatch.setattr(onboard, "run_command", competitor)

        rows = apply_plan(plan)

        assert set(_states(rows).values()) == {"correct"}
        assert world.projection() == {
            "partition": "Acme/osdu-spi-partition",
            "storage": "Acme/osdu-spi-storage",
        }

    def test_the_projection_keeps_other_services_and_the_pins_annotation(self, world):
        world.protect("Acme/osdu-spi-partition")
        world.trust("storage", "Acme/osdu-spi-storage")
        world.project({"storage": "Acme/osdu-spi-storage"})
        world.lock["metadata"]["annotations"][pins.PINS_ANNOTATION] = "{}"

        apply_plan(plan_onboard(target(), "partition", "acme/osdu-spi-partition"))

        assert world.projection() == {
            "partition": "Acme/osdu-spi-partition",
            "storage": "Acme/osdu-spi-storage",
        }
        assert world.lock["metadata"]["annotations"][pins.PINS_ANNOTATION] == "{}"

    def test_a_correct_projection_is_not_rewritten(self, world):
        world.protect("Acme/osdu-spi-partition")
        world.stamp("Acme/osdu-spi-partition")
        world.trust("partition", "Acme/osdu-spi-partition")
        world.project({"partition": "Acme/osdu-spi-partition"})

        apply_plan(plan_onboard(target(), "partition", ""))

        assert world.lock_writes == 0


class TestListAndRemove:
    def test_list_reports_trust_against_the_projection(self, world):
        world.trust("partition", "Acme/osdu-spi-partition")
        world.trust("storage", "Acme/osdu-spi-storage")
        world.project({"partition": "Acme/osdu-spi-partition", "legal": "Acme/osdu-spi-legal"})
        world.credentials.append(
            {
                "name": "other",
                "issuer": GITHUB_ISSUER,
                "subject": "repo:x/y:ref:refs/heads/main",
                "audiences": [],
            }
        )

        rows = {row.item: row for row in list_trust(target())}

        assert rows["partition"].state == "correct"
        assert (rows["storage"].state, rows["storage"].detail) == (
            "drifted",
            "Acme/osdu-spi-storage; projection missing",
        )
        assert rows["legal"].state == "drifted"
        assert rows["other"].state == "unverified"

    def test_a_credential_with_a_drifted_issuer_is_not_projected(self, world):
        world.trust("partition", "Acme/osdu-spi-partition", issuer="https://evil.example")
        world.trust("storage", "Acme/osdu-spi-storage", audiences=("api://other",))
        world.trust("legal", "Acme/osdu-spi-legal")

        assert onboard.sync_projection_from_identity(IDENTITY, RG) == {
            "legal": "Acme/osdu-spi-legal"
        }
        rows = {row.item: row for row in list_trust(target())}
        assert rows["fork-partition"].state == "unverified"
        assert rows["fork-storage"].state == "unverified"
        assert rows["legal"].state == "correct"

    def test_projection_reads_the_roster_fresh_so_a_racing_onboard_is_kept(self, world):
        """Snapshot at plan time, competitor lands, our write must not drop it."""
        world.protect("Acme/osdu-spi-partition")
        plan = plan_onboard(target(), "partition", "acme/osdu-spi-partition")
        world.trust("storage", "Acme/osdu-spi-storage")
        world.project({"storage": "Acme/osdu-spi-storage"})

        apply_plan(plan)

        assert world.projection() == {
            "partition": "Acme/osdu-spi-partition",
            "storage": "Acme/osdu-spi-storage",
        }

    def test_removal_names_the_pending_phase_when_projection_fails(self, world):
        world.trust("partition", "Acme/osdu-spi-partition")
        world.project({"partition": "Acme/osdu-spi-partition"})
        plan = plan_remove(target(), "partition")
        world.lock = None

        with pytest.raises(OnboardError, match="Completed: azure. Pending: cluster"):
            apply_remove(plan)

        assert world.credentials == []

    def test_remove_plans_the_delete_and_the_projection_without_writing(self, world):
        world.trust("partition", "Acme/osdu-spi-partition")
        world.project({"partition": "Acme/osdu-spi-partition"})

        plan = plan_remove(target(), "partition")

        assert [s.argv[3] for s in plan.steps if s.phase == "azure"] == ["delete"]
        assert "--yes" in plan.steps[0].argv
        assert world.writes == [] and world.lock_writes == 0

    def test_remove_revokes_and_reprojects(self, world):
        world.trust("partition", "Acme/osdu-spi-partition")
        world.trust("storage", "Acme/osdu-spi-storage")
        world.project({"partition": "Acme/osdu-spi-partition", "storage": "Acme/osdu-spi-storage"})

        rows = apply_remove(plan_remove(target(), "partition"))

        assert [c["name"] for c in world.credentials] == ["fork-storage"]
        assert world.projection() == {"storage": "Acme/osdu-spi-storage"}
        assert _states(rows)["fork-partition"] == "correct"

    def test_removal_reports_a_credential_added_mid_run_as_correct(self, world, monkeypatch):
        world.trust("partition", "Acme/osdu-spi-partition")
        world.project({"partition": "Acme/osdu-spi-partition"})
        plan = plan_remove(target(), "partition")
        real = world.run_command

        def competitor(argv, **kwargs):
            result = real(argv, **kwargs)
            if argv[:4] == ["az", "identity", "federated-credential", "delete"]:
                world.trust("storage", "Acme/osdu-spi-storage")
            return result

        monkeypatch.setattr(onboard, "run_command", competitor)

        rows = apply_remove(plan)

        assert set(_states(rows).values()) == {"correct"}
        assert world.projection() == {"storage": "Acme/osdu-spi-storage"}

    def test_removing_an_untrusted_service_is_a_no_op(self, world):
        plan = plan_remove(target(), "partition")
        assert plan.steps == []
        apply_remove(plan)
        assert world.writes == []


class TestBootstrapProjection:
    def test_spi_up_rebuilds_the_projection_from_the_identity(self, world):
        from spi.config import Config
        from spi.deploy import _project_trusted_repos

        world.trust("partition", "Acme/osdu-spi-partition")

        _project_trusted_repos(Config.from_env("dev1"))

        assert world.projection() == {"partition": "Acme/osdu-spi-partition"}
        assert world.lock_writes == 1

    def test_only_core_clusters_carry_a_lock_to_project_into(self, world):
        from spi.config import Config, Profile
        from spi.deploy import _project_trusted_repos

        world.lock = None
        _project_trusted_repos(Config.from_env("dev1", profile=Profile.MINIMAL))
        assert world.lock_writes == 0

    def test_a_corrupt_projection_is_rewritten_at_bootstrap(self, world):
        world.trust("partition", "Acme/osdu-spi-partition")
        world.lock["metadata"]["annotations"][TRUSTED_REPOS_ANNOTATION] = "[]"

        onboard.sync_projection_from_identity(IDENTITY, RG)

        assert world.projection() == {"partition": "Acme/osdu-spi-partition"}

    def test_a_lock_refresh_carries_the_projection_forward(self, world, monkeypatch):
        world.project({"partition": "Acme/osdu-spi-partition"})
        monkeypatch.setattr(pins, "read_lock", world.read_lock)
        monkeypatch.setattr(pins, "mutate_lock", world.mutate_lock)

        from spi.images import ResolvedImage, image_lock_names

        resolved = {
            name: ResolvedImage(name, f"repo/{name}", "tag", "then", "sha256:new")
            for name in image_lock_names()
        }
        apply_image_lock(resolved, "master")

        assert world.projection() == {"partition": "Acme/osdu-spi-partition"}


class TestCli:
    def _run(self, args, **seams):
        from typer.testing import CliRunner

        from spi.cli import app

        with (
            patch("spi.cli.verify_spi_cluster", return_value="spi-stack-dev1"),
            patch("spi.onboard.load_target", return_value=target()),
            patch("spi.onboard.plan_onboard") as plan,
            patch("spi.onboard.apply_plan") as apply,
            patch("spi.onboard.list_trust", return_value=[]) as listing,
            patch("spi.onboard.plan_remove") as remove,
            patch("spi.onboard.render_plan"),
        ):
            plan.return_value.repo = "Acme/osdu-spi-partition"
            apply.return_value = []
            remove.return_value.steps = []
            result = CliRunner().invoke(app, args)
            return result, plan, apply, listing, remove

    def test_plan_is_the_default(self):
        result, plan, apply, _listing, _remove = self._run(
            ["onboard", "partition", "--repo", "acme/osdu-spi-partition"]
        )
        assert result.exit_code == 0, result.output
        plan.assert_called_once()
        apply.assert_not_called()

    def test_write_applies(self):
        result, _plan, apply, _listing, _remove = self._run(
            ["onboard", "partition", "--repo", "acme/osdu-spi-partition", "--write"]
        )
        assert result.exit_code == 0, result.output
        apply.assert_called_once()
        assert "first workflow run" in result.output

    def test_list_takes_no_service(self):
        result, plan, _apply, listing, _remove = self._run(["onboard", "--list"])
        assert result.exit_code == 0, result.output
        listing.assert_called_once()
        plan.assert_not_called()
        result, *_ = self._run(["onboard", "partition", "--list"])
        assert result.exit_code != 0

    def test_list_rejects_org_and_skip_repo_too(self):
        for extra in (["--org", "Acme"], ["--skip-repo"]):
            result, *_ = self._run(["onboard", "--list", *extra])
            assert result.exit_code != 0, extra

    def test_org_and_skip_repo_contradict(self):
        result, plan, *_ = self._run(
            ["onboard", "partition", "--repo", "a/b", "--org", "a", "--skip-repo"]
        )
        assert result.exit_code != 0
        plan.assert_not_called()

    def test_a_service_is_required_outside_list(self):
        result, *_ = self._run(["onboard"])
        assert result.exit_code != 0

    def test_refusals_exit_one_with_the_reason(self):
        from typer.testing import CliRunner

        from spi.cli import app

        with (
            patch("spi.cli.verify_spi_cluster", return_value="spi-stack-dev1"),
            patch("spi.onboard.load_target", return_value=target(profile="minimal")),
            patch("spi.onboard.read_roster", return_value=[]),
        ):
            result = CliRunner().invoke(app, ["onboard", "partition", "--repo", "a/b"])
        assert result.exit_code == 1
        assert "Onboarding needs a core environment" in result.output
