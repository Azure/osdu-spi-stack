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

"""spi onboard: planning is pure over an observed State; apply runs phases in order."""

import json
import subprocess
from dataclasses import dataclass, field, replace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from spi import onboard
from spi.onboard import (
    GITHUB_AUDIENCE,
    GITHUB_ISSUER,
    MAX_CREDENTIALS,
    Credential,
    OnboardError,
    Plan,
    Protection,
    State,
    Target,
    apply_plan,
    credential_subject,
    plan_rows,
    plan_steps,
    refuse,
)
from spi.pins import TRUSTED_REPOS_ANNOTATION

REPO = "Acme/osdu-spi-partition"
TARGET = Target(
    env="dev1",
    profile="core",
    identity_name="spi-stack-dev1-deployer",
    resource_group="spi-stack-dev1",
    no_access_identity_name="spi-stack-dev1-noaccess",
    values={
        "AZURE_CLIENT_ID": "client-id",
        "AZURE_TENANT_ID": "tenant-id",
        "AZURE_SUBSCRIPTION_ID": "sub-id",
        "SPI_STACK_RESOURCE_GROUP": "spi-stack-dev1",
        "SPI_STACK_CLUSTER": "spi-stack-dev1",
    },
)
PROTECTED = Protection(exists=True)
ABSENT = Protection(exists=False)


def cred(service: str, repo: str, **overrides) -> Credential:
    fields = {
        "name": f"fork-{service}",
        "issuer": GITHUB_ISSUER,
        "subject": credential_subject(repo),
        "audiences": (GITHUB_AUDIENCE,),
    }
    fields.update(overrides)
    return Credential(**fields)


def correct_values() -> dict:
    return {
        name: ("" if name == "AZURE_CLIENT_ID" else value) for name, value in TARGET.values.items()
    }


def make_plan(
    state: State, *, service="partition", repo=REPO, org="", skip_repo=False, remove=False
) -> Plan:
    plan = Plan(
        TARGET,
        service,
        repo,
        subject=credential_subject(repo) if repo else "",
        org=org,
        skip_repo=skip_repo,
        remove=remove,
        state=state,
    )
    plan.steps = plan_steps(plan)
    plan.rows = plan_rows(plan)
    return plan


def verbs(plan: Plan) -> list[str]:
    return [" ".join(step.argv[:4]) for step in plan.steps]


def states(plan: Plan) -> dict[str, str]:
    return {row.item: row.state for row in plan.rows}


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


class TestPlanning:
    def test_a_fresh_repository_plans_all_three_phases_in_order(self):
        plan = make_plan(
            State(roster=(), projection={}, protection=ABSENT, values=dict.fromkeys(TARGET.values))
        )

        assert [s.phase for s in plan.steps] == ["repository"] * 6 + ["azure"] * 2 + ["cluster"]
        assert verbs(plan)[0] == "gh api --method PUT"
        assert verbs(plan)[1] == "gh secret set AZURE_CLIENT_ID"
        assert verbs(plan)[-3:-1] == ["az identity federated-credential create"] * 2
        assert [s.argv[s.argv.index("--identity-name") + 1] for s in plan.steps[-3:-1]] == [
            TARGET.identity_name,
            TARGET.no_access_identity_name,
        ]
        assert (
            plan.steps[-1].argv[-1]
            == f"{TRUSTED_REPOS_ANNOTATION}={json.dumps({'partition': REPO})}"
        )
        assert set(states(plan).values()) == {"missing"}

    def test_a_correct_repository_only_restamps_the_unreadable_secret(self):
        state = State(
            (cred("partition", REPO),),
            {"partition": REPO},
            PROTECTED,
            correct_values(),
            no_access_roster=(cred("partition", REPO),),
        )

        plan = make_plan(state)

        assert verbs(plan) == ["gh secret set AZURE_CLIENT_ID"]
        assert states(plan)["AZURE_CLIENT_ID on Acme/osdu-spi-partition"] == "unverified"
        assert set(states(plan).values()) == {"correct", "unverified"}

    def test_a_drifted_variable_gets_one_set_and_reads_drifted(self):
        values = correct_values() | {"SPI_STACK_CLUSTER": "old-cluster"}
        roster = (cred("partition", REPO),)
        plan = make_plan(
            State(roster, {"partition": REPO}, PROTECTED, values, no_access_roster=roster)
        )

        assert verbs(plan) == ["gh secret set AZURE_CLIENT_ID", "gh variable set SPI_STACK_CLUSTER"]
        assert plan.steps[1].argv[-2:] == ["--body", "spi-stack-dev1"]
        assert states(plan)["SPI_STACK_CLUSTER on Acme/osdu-spi-partition"] == "drifted"

    def test_org_scope_writes_organization_values_visible_to_all(self):
        plan = make_plan(State((), {}, PROTECTED, dict.fromkeys(TARGET.values)), org="Acme")

        secret = plan.steps[0].argv
        assert secret[:4] == ["gh", "secret", "set", "AZURE_CLIENT_ID"]
        assert secret[4:8] == ["--org", "Acme", "--visibility", "all"]
        assert all("on Acme" in row.item for row in plan.rows if "AZURE" in row.item)

    def test_a_restricted_environment_is_opened_to_every_branch(self):
        protection = Protection(True, "only main (branch), v* (tag)")
        plan = make_plan(State((), {}, protection, correct_values()))

        assert verbs(plan)[0] == "gh api --method PUT"
        assert plan.steps[0].argv[-2:] == ["-F", "deployment_branch_policy=null"]
        assert plan.steps[0].description.startswith("Open the spi-stack environment")
        assert plan.rows[0].detail == "admits only main (branch), v* (tag)"
        assert make_plan(State((), {}, PROTECTED, correct_values())).rows[0].detail == (
            "every branch"
        )

    def test_a_credential_naming_another_repository_is_updated_not_created(self):
        state = State(
            (cred("partition", "Old/fork"),), {"partition": "Old/fork"}, PROTECTED, correct_values()
        )

        plan = make_plan(state)

        assert "az identity federated-credential update" in verbs(plan)
        assert states(plan)["fork-partition on spi-stack-dev1-deployer"] == "drifted"
        assert states(plan)[f"{TRUSTED_REPOS_ANNOTATION} on osdu-image-lock"] == "drifted"

    def test_skip_repo_withholds_trust_until_the_rules_exist(self):
        plan = make_plan(State((), {}, ABSENT), skip_repo=True)

        assert plan.blocked
        assert plan.steps == []
        assert states(plan) == {
            "spi-stack environment on Acme/osdu-spi-partition": "missing",
            "fork-partition on spi-stack-dev1-deployer": "missing",
            "fork-partition on spi-stack-dev1-noaccess": "missing",
            f"{TRUSTED_REPOS_ANNOTATION} on osdu-image-lock": "missing",
        }

    def test_skip_repo_with_rules_in_place_plans_only_trust_and_projection(self):
        plan = make_plan(State((), {}, PROTECTED), skip_repo=True)

        assert [s.phase for s in plan.steps] == ["azure", "azure", "cluster"]
        assert not any("AZURE_CLIENT_ID" in row.item for row in plan.rows)

    def test_the_no_access_identity_is_trusted_alongside_the_deployer(self):
        """One identity trusted and the other not is the drift a partial write
        leaves behind; the plan repairs only the missing half."""
        plan = make_plan(
            State((cred("partition", REPO),), {"partition": REPO}, PROTECTED), skip_repo=True
        )

        assert verbs(plan) == ["az identity federated-credential create"]
        step = plan.steps[0]
        assert step.argv[step.argv.index("--identity-name") + 1] == TARGET.no_access_identity_name
        assert step.argv[step.argv.index("--subject") + 1] == credential_subject(REPO)
        assert states(plan)["fork-partition on spi-stack-dev1-deployer"] == "correct"
        assert states(plan)["fork-partition on spi-stack-dev1-noaccess"] == "missing"

    def test_a_target_without_a_no_access_identity_plans_the_deployer_alone(self):
        target = replace(TARGET, no_access_identity_name="")
        plan = Plan(target, "partition", REPO, skip_repo=True, state=State((), {}, PROTECTED))
        plan.steps = plan_steps(plan)
        plan.rows = plan_rows(plan)

        assert [s.phase for s in plan.steps] == ["azure", "cluster"]
        assert "fork-partition on spi-stack-dev1-noaccess" not in states(plan)

    def test_the_subscription_is_pinned_on_the_az_command(self):
        plan = make_plan(State((), {}, PROTECTED), skip_repo=True)

        assert plan.steps[0].argv[plan.steps[0].argv.index("--subscription") + 1] == "sub-id"

    def test_projection_keeps_sibling_services_and_drops_unshaped_credentials(self):
        roster = (
            cred("partition", REPO),
            cred("schema", "Acme/osdu-spi-schema"),
            cred("legal", "Acme/osdu-spi-legal", issuer="https://elsewhere"),
            Credential(
                "by-hand", GITHUB_ISSUER, "repo:Acme/x:ref:refs/heads/main", (GITHUB_AUDIENCE,)
            ),
        )
        plan = make_plan(State(roster, {}, PROTECTED), skip_repo=True)

        assert json.loads(plan.steps[-1].argv[-1].split("=", 1)[1]) == {
            "partition": REPO,
            "schema": "Acme/osdu-spi-schema",
        }

    def test_remove_plans_revoke_then_reprojection(self):
        roster = (cred("partition", REPO), cred("schema", "Acme/osdu-spi-schema"))
        plan = make_plan(
            State(
                roster,
                {"partition": REPO, "schema": "Acme/osdu-spi-schema"},
                no_access_roster=roster,
            ),
            repo="",
            remove=True,
        )

        assert verbs(plan) == [
            "az identity federated-credential delete",
            "az identity federated-credential delete",
            "kubectl annotate configmap osdu-image-lock",
        ]
        assert [s.argv[s.argv.index("--identity-name") + 1] for s in plan.steps[:2]] == [
            TARGET.identity_name,
            TARGET.no_access_identity_name,
        ]
        assert json.loads(plan.steps[2].argv[-1].split("=", 1)[1]) == {
            "schema": "Acme/osdu-spi-schema"
        }
        assert plan.rows[0].detail == f"trusts {REPO}"

    def test_removing_an_absent_service_has_nothing_to_do(self):
        plan = make_plan(State((), {}), repo="", remove=True)

        assert plan.steps == []
        assert plan.rows[0].state == "correct"


class TestRefusals:
    def test_an_organization_that_does_not_own_the_repository(self):
        with pytest.raises(OnboardError, match="does not own"):
            refuse(make_plan(State((), {}, PROTECTED, correct_values()), org="Other"))

    def test_a_repository_already_backing_another_service_regardless_of_case(self):
        state = State((cred("schema", REPO.lower()),), {}, PROTECTED, correct_values())

        with pytest.raises(OnboardError, match="already backs schema"):
            refuse(make_plan(state))

    def test_the_twenty_first_credential(self):
        roster = tuple(cred(f"svc{i}", f"Acme/fork{i}") for i in range(MAX_CREDENTIALS))

        with pytest.raises(OnboardError, match="Azure maximum"):
            refuse(make_plan(State(roster, {}, PROTECTED, correct_values())))

    def test_an_existing_credential_is_not_counted_against_the_cap(self):
        roster = tuple(cred(f"svc{i}", f"Acme/fork{i}") for i in range(MAX_CREDENTIALS - 1))
        roster += (cred("partition", REPO),)

        refuse(make_plan(State(roster, {}, PROTECTED, correct_values())))

    def test_the_cap_applies_to_the_no_access_identity_too(self):
        full = tuple(cred(f"svc{i}", f"Acme/fork{i}") for i in range(MAX_CREDENTIALS))

        with pytest.raises(OnboardError, match="spi-stack-dev1-noaccess already holds"):
            refuse(make_plan(State((), {}, PROTECTED, correct_values(), no_access_roster=full)))

    def test_the_target_must_be_a_core_environment_publishing_the_identity(self):
        with pytest.raises(OnboardError, match="needs a core environment"):
            onboard.require_target(Target("dev1", "minimal", "id", "rg", TARGET.values))
        with pytest.raises(OnboardError, match="AZURE_CLIENT_ID"):
            onboard.require_target(
                Target("dev1", "core", "id", "rg", TARGET.values | {"AZURE_CLIENT_ID": ""})
            )

    def test_unknown_and_non_deployable_services(self):
        with pytest.raises(OnboardError, match="Unknown service"):
            onboard.require_known_service("nope")
        with pytest.raises(OnboardError, match="Unknown service"):
            onboard.require_known_service("schema-load")


# ---------------------------------------------------------------------------
# Reads: canned command output keyed by argv prefix
# ---------------------------------------------------------------------------


class Shell:
    def __init__(self, **responses):
        self.responses = {tuple(k.split("__")): v for k, v in responses.items()}
        self.calls: list[list[str]] = []

    def add(self, prefix: str, payload) -> None:
        self.responses[tuple(prefix.split("__"))] = payload

    def __call__(self, argv, **_):
        self.calls.append(list(argv))
        for prefix, payload in self.responses.items():
            if tuple(argv[: len(prefix)]) == prefix:
                if isinstance(payload, subprocess.CompletedProcess):
                    return payload
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(argv, 1, "", f"HTTP 404: Not Found {argv}")


def failed(stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 1, "", stderr)


class TestReads:
    def test_protection_reads_every_page_of_a_custom_policy_as_drift(self, monkeypatch):
        shell = Shell()
        shell.add(
            "gh__api__repos/Acme/osdu-spi-partition/environments/spi-stack",
            {"deployment_branch_policy": {"custom_branch_policies": True}},
        )
        shell.add(
            "gh__api__--paginate",
            [
                {"branch_policies": [{"id": 1, "name": "main", "type": "branch"}]},
                {
                    "branch_policies": [
                        {"id": 2, "name": "fork_integration", "type": "branch"},
                        {"id": 3, "name": "main", "type": "tag"},
                    ]
                },
            ],
        )
        monkeypatch.setattr(onboard, "run_command", shell)

        protection = onboard.read_protection(REPO)

        assert protection.restriction == "only main (branch), fork_integration (branch), main (tag)"
        assert not protection.satisfied

    def test_protection_reads_open_and_protected_only_environments(self, monkeypatch):
        shell = Shell()
        shell.add(
            "gh__api__repos/Acme/osdu-spi-partition/environments/spi-stack",
            {"deployment_branch_policy": None},
        )
        monkeypatch.setattr(onboard, "run_command", shell)
        assert onboard.read_protection(REPO) == PROTECTED

        shell = Shell()
        shell.add(
            "gh__api__repos/Acme/osdu-spi-partition/environments/spi-stack",
            {"deployment_branch_policy": {"protected_branches": True}},
        )
        monkeypatch.setattr(onboard, "run_command", shell)
        assert onboard.read_protection(REPO) == Protection(True, "protected branches only")

    def test_a_missing_environment_reads_absent_while_other_errors_raise(self, monkeypatch):
        monkeypatch.setattr(onboard, "run_command", Shell())
        assert onboard.read_protection(REPO) == ABSENT

        monkeypatch.setattr(onboard, "run_command", Shell(gh=failed("HTTP 403: Forbidden")))
        with pytest.raises(OnboardError, match="Could not read environment"):
            onboard.read_protection(REPO)

    def test_values_read_at_the_written_scope_with_org_visibility_as_drift(self, monkeypatch):
        shell = Shell(
            gh__variable=[
                {"name": "AZURE_TENANT_ID", "value": "tenant-id", "visibility": "all"},
                {"name": "SPI_STACK_CLUSTER", "value": "spi-stack-dev1", "visibility": "selected"},
            ],
            gh__secret=[{"name": "AZURE_CLIENT_ID"}],
        )
        monkeypatch.setattr(onboard, "run_command", shell)

        values = onboard.read_values(REPO, "Acme")

        assert shell.calls[0][3:5] == ["--org", "Acme"]
        assert values == {
            "AZURE_CLIENT_ID": "",
            "AZURE_TENANT_ID": "tenant-id",
            "AZURE_SUBSCRIPTION_ID": None,
            "SPI_STACK_RESOURCE_GROUP": None,
            "SPI_STACK_CLUSTER": "spi-stack-dev1 (selected)",
        }

    def test_the_roster_is_read_in_the_published_subscription(self, monkeypatch):
        shell = Shell(
            az=[
                {
                    "name": "fork-partition",
                    "issuer": GITHUB_ISSUER,
                    "subject": credential_subject(REPO),
                    "audiences": [GITHUB_AUDIENCE],
                }
            ]
        )
        monkeypatch.setattr(onboard, "run_command", shell)

        roster = onboard.read_roster(TARGET)

        assert roster == (cred("partition", REPO),)
        assert shell.calls[0][shell.calls[0].index("--subscription") + 1] == "sub-id"

    def test_a_missing_no_access_identity_names_the_fix(self, monkeypatch):
        """An environment provisioned before the identity existed must say
        which command creates it rather than echoing the ARM error."""
        monkeypatch.setattr(
            onboard,
            "run_command",
            Shell(
                az=failed(
                    "(ResourceNotFound) The Resource 'Microsoft.ManagedIdentity/"
                    "userAssignedIdentities/spi-stack-dev1-noaccess' was not found."
                )
            ),
        )

        with pytest.raises(OnboardError, match="spi-stack-dev1-noaccess not found; run 'spi up'"):
            onboard.read_no_access_roster(TARGET)

        assert onboard.read_no_access_roster(replace(TARGET, no_access_identity_name="")) == ()

    def test_other_no_access_read_failures_pass_through(self, monkeypatch):
        monkeypatch.setattr(onboard, "run_command", Shell(az=failed("AuthorizationFailed")))

        with pytest.raises(OnboardError, match="AuthorizationFailed"):
            onboard.read_no_access_roster(TARGET)

    def test_load_target_derives_both_identity_names_from_the_cluster(self, monkeypatch):
        monkeypatch.setattr(
            "spi.info.collect_info",
            lambda: {
                "environment": {"name": "dev1", "profile": "core"},
                "deploy_identity": {
                    "client_id": "client-id",
                    "no_access_client_id": "noaccess-id",
                    "tenant_id": "tenant-id",
                    "subscription_id": "sub-id",
                    "resource_group": "spi-stack-dev1",
                    "cluster": "spi-stack-dev1",
                },
            },
        )

        target = onboard.load_target()

        assert target.identity_name == "spi-stack-dev1-deployer"
        assert target.no_access_identity_name == "spi-stack-dev1-noaccess"
        assert target.no_access().az_scope()[:2] == ["--identity-name", "spi-stack-dev1-noaccess"]
        assert "noaccess-id" not in target.values.values()

    def test_malformed_subjects_never_read_as_a_repository(self):
        for subject in (
            "repo:../evil:environment:spi-stack",
            "repo:a/b/c:environment:spi-stack",
            "repo:Acme/x:ref:refs/heads/main",
        ):
            assert Credential("fork-x", GITHUB_ISSUER, subject, (GITHUB_AUDIENCE,)).repo == ""

    def test_repositories_are_resolved_to_their_stored_casing(self, monkeypatch):
        monkeypatch.setattr(onboard, "run_command", Shell(gh__api={"full_name": REPO}))

        assert onboard.resolve_repository("acme/OSDU-SPI-PARTITION") == REPO
        with pytest.raises(OnboardError, match="must be <owner>/<name>"):
            onboard.resolve_repository("../evil")

    def test_omitting_repo_reuses_and_recanonicalizes_the_trusted_repository(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            onboard, "read_roster", lambda target: (cred("partition", REPO.lower()),)
        )
        monkeypatch.setattr(
            onboard, "resolve_repository", lambda spec: seen.setdefault("spec", spec) and REPO
        )
        monkeypatch.setattr(onboard, "read_subject", credential_subject)
        monkeypatch.setattr(
            onboard,
            "observe",
            lambda *a, **k: State(
                (cred("partition", REPO.lower()),), {}, PROTECTED, correct_values()
            ),
        )

        plan = onboard.plan_onboard(TARGET, "partition")

        assert seen["spec"] == REPO.lower()
        assert plan.repo == REPO
        with pytest.raises(OnboardError, match="pass --repo"):
            monkeypatch.setattr(onboard, "read_roster", lambda target: ())
            onboard.plan_onboard(TARGET, "partition")


# ---------------------------------------------------------------------------
# Apply: phase order, failure reporting, and the write-time gates
# ---------------------------------------------------------------------------


@dataclass
class Live:
    """Observed state that recorded writes mutate, without a gh/az simulator."""

    roster: tuple[Credential, ...] = ()
    no_access_roster: tuple[Credential, ...] = ()
    protection: Protection = ABSENT
    values: dict = field(default_factory=lambda: dict.fromkeys(TARGET.values))
    projection: dict = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    fail: dict[str, list[str]] = field(default_factory=dict)
    projected: int = 0

    def observed(self, *, values: bool = True) -> State:
        """What observe() would return; --skip-repo never reads the values."""

        return State(
            self.roster,
            self.projection,
            self.protection,
            dict(self.values) if values else {},
            no_access_roster=self.no_access_roster,
        )

    def roster_of(self, target) -> tuple[Credential, ...]:
        if target.identity_name == TARGET.no_access_identity_name:
            return self.no_access_roster
        assert target.identity_name == TARGET.identity_name
        return self.roster

    def run(self, argv, **_):
        self.calls.append(list(argv))
        for prefix, errors in self.fail.items():
            if errors and " ".join(argv).startswith(prefix):
                return failed(errors.pop(0))
        if argv[:3] == ["gh", "api", "--method"]:
            self.protection = PROTECTED
        if argv[:3] == ["gh", "variable", "set"]:
            self.values[argv[3]] = argv[-1]
        if argv[:3] == ["gh", "secret", "set"]:
            self.values[argv[3]] = ""
        if argv[:3] == ["az", "identity", "federated-credential"]:
            service = argv[5].removeprefix("fork-")
            identity = argv[argv.index("--identity-name") + 1]
            no_access = identity == TARGET.no_access_identity_name
            current = self.no_access_roster if no_access else self.roster
            others = tuple(c for c in current if c.service != service)
            if argv[3] != "delete":
                repo = argv[argv.index("--subject") + 1].split(":")[1]
                others += (cred(service, repo),)
            if no_access:
                self.no_access_roster = others
            else:
                self.roster = others
        return subprocess.CompletedProcess(argv, 0, "", "")

    def project(self, target, description=""):
        self.projected += 1
        self.projection = onboard.roster_repos(self.roster)
        return self.projection


@pytest.fixture
def live(monkeypatch):
    world = Live()
    monkeypatch.setattr(onboard, "run_command", world.run)
    monkeypatch.setattr(onboard, "read_roster", world.roster_of)
    monkeypatch.setattr(onboard, "read_protection", lambda repo: world.protection)
    monkeypatch.setattr(onboard, "read_values", lambda repo, org: dict(world.values))
    monkeypatch.setattr(onboard, "read_projection", lambda: dict(world.projection))
    monkeypatch.setattr(onboard, "project_roster", world.project)
    monkeypatch.setattr(onboard.time, "sleep", lambda s: None)
    return world


class TestApply:
    def test_phases_run_in_order_and_rows_are_reobserved(self, live):
        plan = make_plan(live.observed())

        rows = apply_plan(plan)

        tools = [call[0] for call in live.calls]
        assert tools == ["gh"] * 6 + ["az"] * 2
        assert live.no_access_roster == live.roster == (cred("partition", REPO),)
        assert live.projected == 1
        assert {row.state for row in rows} == {"correct"}
        assert next(r.detail for r in rows if r.item.startswith("AZURE_CLIENT_ID")) == "stamped"

    def test_a_repository_failure_names_the_pending_phases_and_writes_nothing_else(self, live):
        live.fail["gh api --method PUT"] = ["HTTP 403: admin required"]
        plan = make_plan(live.observed())

        with pytest.raises(OnboardError, match="admin required") as exc:
            apply_plan(plan)

        assert "Completed: nothing. Pending: repository, azure, cluster" in str(exc.value)
        assert live.roster == ()
        assert live.projected == 0

    def test_trust_is_never_enabled_when_the_rules_are_missing_at_write_time(self, live):
        live.protection = PROTECTED
        plan = make_plan(live.observed(values=False), skip_repo=True)
        live.protection = Protection(True, "protected branches only")

        with pytest.raises(OnboardError, match="does not protect spi-stack") as exc:
            apply_plan(plan)

        assert "Completed: nothing. Pending: azure, cluster" in str(exc.value)
        assert live.roster == ()

    def test_a_blocked_plan_refuses_to_apply(self, live):
        with pytest.raises(OnboardError, match="does not protect spi-stack"):
            apply_plan(make_plan(live.observed(values=False), skip_repo=True))

    def test_a_busy_identity_is_retried_after_backoff(self, live):
        live.protection = PROTECTED
        live.fail["az identity federated-credential create"] = ["Conflict: concurrent write"]

        rows = apply_plan(make_plan(live.observed(values=False), skip_repo=True))

        assert [c[3] for c in live.calls if c[0] == "az"] == ["create", "create", "create"]
        assert live.roster == live.no_access_roster == (cred("partition", REPO),)
        assert {row.state for row in rows} == {"correct"}

    def test_a_non_conflict_credential_failure_stops_after_one_attempt(self, live):
        live.protection = PROTECTED
        live.fail["az identity federated-credential create"] = ["AuthorizationFailed"]

        with pytest.raises(OnboardError, match="AuthorizationFailed") as exc:
            apply_plan(make_plan(live.observed(values=False), skip_repo=True))

        assert "Completed: nothing. Pending: azure, cluster" in str(exc.value)
        assert len([c for c in live.calls if c[0] == "az"]) == 1

    def test_remove_revokes_then_reprojects(self, live):
        live.roster = (cred("partition", REPO), cred("schema", "Acme/osdu-spi-schema"))
        live.no_access_roster = live.roster
        live.projection = onboard.roster_repos(live.roster)

        rows = apply_plan(make_plan(live.observed(), repo="", remove=True))

        assert [c[3] for c in live.calls] == ["delete", "delete"]
        assert live.projection == {"schema": "Acme/osdu-spi-schema"}
        assert live.no_access_roster == (cred("schema", "Acme/osdu-spi-schema"),)
        assert [(r.state, r.detail) for r in rows[:2]] == [("correct", "absent")] * 2

    def test_nothing_to_change_touches_nothing(self, live):
        live.roster = live.no_access_roster = (cred("partition", REPO),)
        live.projection = {"partition": REPO}
        live.protection = PROTECTED

        rows = apply_plan(make_plan(live.observed(values=False), skip_repo=True))

        assert live.calls == []
        assert {row.state for row in rows} == {"correct"}


class TestProjection:
    def test_project_roster_keeps_data_and_other_annotations(self, monkeypatch):
        lock = {
            "data": {"partition": "img@sha256:1"},
            "metadata": {"annotations": {"spi-stack.osdu.dev/pins": "{}"}},
        }
        written = {}
        monkeypatch.setattr(onboard, "read_roster", lambda target: (cred("partition", REPO),))
        monkeypatch.setattr(
            onboard, "mutate_lock", lambda compute, description: written.update(compute(lock))
        )

        assert onboard.project_roster(TARGET) == {"partition": REPO}
        assert written["data"] == lock["data"]
        assert written["metadata"]["annotations"] == {
            "spi-stack.osdu.dev/pins": "{}",
            TRUSTED_REPOS_ANNOTATION: json.dumps({"partition": REPO}),
        }

    def test_bootstrap_projects_only_when_the_lock_disagrees(self, monkeypatch):
        calls = []
        monkeypatch.setattr(onboard, "read_roster", lambda target: (cred("partition", REPO),))
        monkeypatch.setattr(onboard, "read_projection", lambda: {"partition": REPO})
        monkeypatch.setattr(onboard, "project_roster", lambda *a: calls.append(a))

        assert onboard.sync_projection_from_identity("id", "rg") == {"partition": REPO}
        assert calls == []

        monkeypatch.setattr(onboard, "read_projection", lambda: {})
        onboard.sync_projection_from_identity("id", "rg")
        assert len(calls) == 1

    def test_list_pairs_trust_with_projection_and_flags_the_rest(self, monkeypatch):
        roster = (
            cred("partition", REPO),
            cred("schema", "Acme/osdu-spi-schema"),
            Credential(
                "by-hand", GITHUB_ISSUER, "repo:Acme/x:ref:refs/heads/main", (GITHUB_AUDIENCE,)
            ),
        )
        monkeypatch.setattr(onboard, "read_roster", lambda target: roster)
        monkeypatch.setattr(
            onboard, "read_projection", lambda: {"partition": REPO, "legal": "Acme/legal"}
        )

        rows = {row.item: (row.state, row.detail) for row in onboard.list_trust(TARGET)}

        assert rows["partition"] == ("correct", REPO)
        assert rows["schema"][0] == "drifted"
        assert rows["legal"] == ("drifted", "projected Acme/legal but not trusted")
        assert rows["by-hand"][0] == "unverified"
        assert rows["fork-partition on spi-stack-dev1-noaccess"] == ("correct", REPO)

    def test_list_still_answers_when_the_no_access_identity_is_missing(self, monkeypatch):
        """An environment provisioned before the identity existed keeps its
        deployer listing and gets one row naming the fix."""
        monkeypatch.setattr(onboard, "read_roster", lambda target: (cred("partition", REPO),))
        monkeypatch.setattr(onboard, "read_projection", lambda: {"partition": REPO})
        monkeypatch.setattr(
            onboard,
            "read_no_access_roster",
            lambda target: (_ for _ in ()).throw(OnboardError("spi-stack-dev1-noaccess not found")),
        )

        rows = {row.item: (row.state, row.detail) for row in onboard.list_trust(TARGET)}

        assert rows["partition"] == ("correct", REPO)
        assert rows["spi-stack-dev1-noaccess"][0] == "missing"
        assert "not found" in rows["spi-stack-dev1-noaccess"][1]

    def test_list_reports_the_no_access_identity_lagging_or_leading(self, monkeypatch):
        deployer = (cred("partition", REPO), cred("schema", "Acme/osdu-spi-schema"))
        no_access = (cred("schema", "Acme/other-schema"), cred("legal", "Acme/legal"))
        monkeypatch.setattr(
            onboard,
            "read_roster",
            lambda target: no_access if "noaccess" in target.identity_name else deployer,
        )
        monkeypatch.setattr(onboard, "read_projection", lambda: onboard.roster_repos(deployer))

        rows = {row.item: (row.state, row.detail) for row in onboard.list_trust(TARGET)}

        assert rows["fork-partition on spi-stack-dev1-noaccess"][0] == "missing"
        assert rows["fork-schema on spi-stack-dev1-noaccess"] == (
            "drifted",
            "trusts Acme/other-schema, not Acme/osdu-spi-schema",
        )
        assert rows["fork-legal on spi-stack-dev1-noaccess"][0] == "drifted"


# ---------------------------------------------------------------------------
# Rendering and CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_a_blocked_plan_warns_instead_of_reporting_success(self, capsys):
        onboard.render_plan(make_plan(State((), {}, ABSENT), skip_repo=True))

        out = capsys.readouterr().out
        assert "Trust steps are withheld" in out
        assert "Nothing to change" not in out

    @pytest.mark.parametrize(
        "args, message",
        [
            (["--list", "partition"], "takes no other options"),
            (["partition", "--remove", "--repo", REPO], "takes only the service"),
            (["partition", "--org", "Acme", "--skip-repo"], "stamps GitHub values"),
            ([], "name the service"),
        ],
    )
    def test_contradictory_options_are_refused_before_any_read(self, args, message):
        from spi.cli import app

        with patch("spi.cli.verify_spi_cluster") as verify:
            result = CliRunner().invoke(app, ["onboard", *args])

        assert result.exit_code != 0
        assert message in result.output.replace("\n", "")
        verify.assert_not_called()

    def test_refusals_exit_one_with_the_reason(self):
        from spi.cli import app

        with (
            patch("spi.cli.verify_spi_cluster", return_value="ctx"),
            patch("spi.onboard.load_target", return_value=TARGET),
            patch("spi.onboard.plan_onboard", side_effect=OnboardError("already backs schema")),
        ):
            result = CliRunner().invoke(app, ["onboard", "partition", "--repo", REPO])

        assert result.exit_code == 1
        assert "already backs schema" in result.output


# ---------------------------------------------------------------------------
# Subject forms: GitHub signs repo:<owner>/<name> or repo:<owner>@<id>/<name>@<id>
# ---------------------------------------------------------------------------

ID_SUBJECT = "repo:Acme@199854422/osdu-spi-partition@1351440282:environment:spi-stack"


class TestSubjectForms:
    def test_both_forms_name_the_repository(self):
        assert cred("partition", REPO).repo == REPO
        assert cred("partition", REPO, subject=ID_SUBJECT).repo == REPO
        assert cred("partition", REPO, subject="repo:Acme/x:environment:other").repo == ""

    def test_roster_projects_either_form(self):
        roster = (cred("partition", REPO, subject=ID_SUBJECT),)

        assert onboard.roster_repos(roster) == {"partition": REPO}

    def test_a_classic_credential_is_drift_against_an_id_subject(self):
        """Entra matches the string, so the credential is rewritten, not kept."""
        plan = Plan(
            TARGET,
            "partition",
            REPO,
            subject=ID_SUBJECT,
            skip_repo=True,
            state=State((cred("partition", REPO),), {"partition": REPO}, PROTECTED),
        )
        plan.steps = plan_steps(plan)
        plan.rows = plan_rows(plan)

        assert verbs(plan)[0] == "az identity federated-credential update"
        assert plan.steps[0].argv[plan.steps[0].argv.index("--subject") + 1] == ID_SUBJECT
        assert states(plan)["fork-partition on spi-stack-dev1-deployer"] == "drifted"

    def test_read_subject_uses_the_reported_prefix(self, monkeypatch):
        shell = Shell()
        shell.add(
            f"gh__api__repos/{REPO}/actions/oidc/customization/sub",
            {"use_default": True, "sub_claim_prefix": "repo:Acme@1/osdu-spi-partition@2"},
        )
        monkeypatch.setattr(onboard, "run_command", shell)

        assert onboard.read_subject(REPO) == (
            "repo:Acme@1/osdu-spi-partition@2:environment:spi-stack"
        )

    def test_read_subject_falls_back_to_the_classic_form(self, monkeypatch):
        monkeypatch.setattr(onboard, "run_command", Shell())

        assert onboard.read_subject(REPO) == credential_subject(REPO)

    def test_a_custom_template_is_refused(self, monkeypatch):
        shell = Shell()
        shell.add(
            f"gh__api__repos/{REPO}/actions/oidc/customization/sub",
            {"use_default": False, "include_claim_keys": ["repo", "job_workflow_ref"]},
        )
        monkeypatch.setattr(onboard, "run_command", shell)

        with pytest.raises(OnboardError, match="customizes its OIDC subject"):
            onboard.read_subject(REPO)
