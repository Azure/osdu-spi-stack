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
    COMMUNITY_SOURCE,
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
from spi.pins import CANONICAL_SOURCES_ANNOTATION, TRUSTED_REPOS_ANNOTATION

REPO = "Acme/osdu-spi-partition"
TARGET = Target(
    env="dev1",
    profile="core",
    identity_name="spi-stack-dev1-deployer",
    resource_group="spi-stack-dev1",
    no_access_identity_name="spi-stack-dev1-noaccess",
    member_identity_name="spi-stack-dev1-member",
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
READ_SOURCE_TAGS = onboard.read_source_tags


@pytest.fixture(autouse=True)
def no_source_tags(monkeypatch):
    """Tests about trust see no source tags; source tests set their own."""

    monkeypatch.setattr(onboard, "read_source_tags", lambda *args, **kwargs: {})
    monkeypatch.setattr(onboard, "read_source_projection", lambda: {})


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
    state: State,
    *,
    service="partition",
    repo=REPO,
    org="",
    skip_repo=False,
    remove=False,
    canonical_source="",
    recorded: str | None = COMMUNITY_SOURCE,
) -> Plan:
    """``recorded`` seeds the service's source tag, so trust tests plan no source write."""

    if recorded is not None and service not in state.sources:
        state = replace(state, sources={**state.sources, service: recorded})
    plan = Plan(
        TARGET,
        service,
        repo,
        subject=credential_subject(repo) if repo else "",
        org=org,
        skip_repo=skip_repo,
        remove=remove,
        canonical_source=canonical_source,
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
    def test_a_fresh_repository_plans_every_phase_in_order(self):
        plan = make_plan(
            State(roster=(), projection={}, protection=ABSENT, values=dict.fromkeys(TARGET.values)),
            recorded=None,
        )

        assert [s.phase for s in plan.steps] == (
            ["repository"] * 6 + ["azure"] * 3 + ["source", "cluster"]
        )
        assert verbs(plan)[0] == "gh api --method PUT"
        assert verbs(plan)[1] == "gh secret set AZURE_CLIENT_ID"
        assert verbs(plan)[-5:-2] == ["az identity federated-credential create"] * 3
        assert plan.steps[-2].argv[5:7] == ["--set", "tags.spi-source-partition=community"]
        assert [s.argv[s.argv.index("--identity-name") + 1] for s in plan.steps[-5:-2]] == [
            TARGET.identity_name,
            TARGET.member_identity_name,
            TARGET.no_access_identity_name,
        ]
        assert (
            plan.steps[-1].argv[-1]
            == f"{TRUSTED_REPOS_ANNOTATION}={json.dumps({'partition': REPO})}"
        )
        assert set(states(plan).values()) == {"missing", "correct"}
        assert states(plan)[f"{CANONICAL_SOURCES_ANNOTATION} on osdu-image-lock"] == "correct"
        assert states(plan)["spi-source-partition on spi-stack-dev1"] == "missing"

    def test_a_correct_repository_only_restamps_the_unreadable_secret(self):
        state = State(
            (cred("partition", REPO),),
            {"partition": REPO},
            PROTECTED,
            correct_values(),
            no_access_roster=(cred("partition", REPO),),
            member_roster=(cred("partition", REPO),),
        )

        plan = make_plan(state)

        assert verbs(plan) == ["gh secret set AZURE_CLIENT_ID"]
        assert states(plan)["AZURE_CLIENT_ID on Acme/osdu-spi-partition"] == "unverified"
        assert set(states(plan).values()) == {"correct", "unverified"}

    def test_a_drifted_variable_gets_one_set_and_reads_drifted(self):
        values = correct_values() | {"SPI_STACK_CLUSTER": "old-cluster"}
        roster = (cred("partition", REPO),)
        plan = make_plan(
            State(
                roster,
                {"partition": REPO},
                PROTECTED,
                values,
                no_access_roster=roster,
                member_roster=roster,
            )
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
            "fork-partition on spi-stack-dev1-member": "missing",
            "fork-partition on spi-stack-dev1-noaccess": "missing",
            "spi-source-partition on spi-stack-dev1": "correct",
            f"{TRUSTED_REPOS_ANNOTATION} on osdu-image-lock": "missing",
            f"{CANONICAL_SOURCES_ANNOTATION} on osdu-image-lock": "correct",
        }

    def test_skip_repo_with_rules_in_place_plans_only_trust_and_projection(self):
        plan = make_plan(State((), {}, PROTECTED), skip_repo=True)

        assert [s.phase for s in plan.steps] == ["azure", "azure", "azure", "cluster"]
        assert not any("AZURE_CLIENT_ID" in row.item for row in plan.rows)

    @pytest.mark.parametrize(
        "member_trusted, mirror_identity",
        [
            (True, TARGET.no_access_identity_name),
            (False, TARGET.member_identity_name),
        ],
    )
    def test_a_mirror_identity_is_trusted_alongside_the_deployer(
        self, member_trusted, mirror_identity
    ):
        """One identity trusted and another not is the drift a partial write
        leaves behind; the plan repairs only the missing piece."""
        trusted_roster = (cred("partition", REPO),)
        state = State(
            (cred("partition", REPO),),
            {"partition": REPO},
            PROTECTED,
            member_roster=trusted_roster if member_trusted else (),
            no_access_roster=() if member_trusted else trusted_roster,
        )
        plan = make_plan(state, skip_repo=True)

        assert verbs(plan) == ["az identity federated-credential create"]
        step = plan.steps[0]
        assert step.argv[step.argv.index("--identity-name") + 1] == mirror_identity
        assert step.argv[step.argv.index("--subject") + 1] == credential_subject(REPO)
        assert states(plan)["fork-partition on spi-stack-dev1-deployer"] == "correct"
        assert states(plan)[f"fork-partition on {mirror_identity}"] == "missing"

    def test_a_target_without_mirror_identities_plans_the_deployer_alone(self):
        target = replace(TARGET, no_access_identity_name="", member_identity_name="")
        state = State((), {}, PROTECTED, sources={"partition": COMMUNITY_SOURCE})
        plan = Plan(target, "partition", REPO, skip_repo=True, state=state)
        plan.steps = plan_steps(plan)
        plan.rows = plan_rows(plan)

        assert [s.phase for s in plan.steps] == ["azure", "cluster"]
        assert "fork-partition on spi-stack-dev1-noaccess" not in states(plan)
        assert "fork-partition on spi-stack-dev1-member" not in states(plan)

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
                member_roster=roster,
            ),
            repo="",
            remove=True,
        )

        assert verbs(plan) == [
            "az identity federated-credential delete",
            "az identity federated-credential delete",
            "az identity federated-credential delete",
            "kubectl annotate configmap osdu-image-lock",
        ]
        assert [s.argv[s.argv.index("--identity-name") + 1] for s in plan.steps[:3]] == [
            TARGET.identity_name,
            TARGET.member_identity_name,
            TARGET.no_access_identity_name,
        ]
        assert json.loads(plan.steps[3].argv[-1].split("=", 1)[1]) == {
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

    @pytest.mark.parametrize(
        "full_member, mirror_identity",
        [
            (True, TARGET.member_identity_name),
            (False, TARGET.no_access_identity_name),
        ],
    )
    def test_the_cap_applies_to_each_mirror_identity_too(self, full_member, mirror_identity):
        full = tuple(cred(f"svc{i}", f"Acme/fork{i}") for i in range(MAX_CREDENTIALS))
        state = State(
            (),
            {},
            PROTECTED,
            correct_values(),
            member_roster=full if full_member else (),
            no_access_roster=() if full_member else full,
        )

        with pytest.raises(OnboardError, match=f"{mirror_identity} already holds"):
            refuse(make_plan(state))

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

    @pytest.mark.parametrize(
        "label, identity_field, read_mirror_roster",
        [
            ("No-access", "no_access_identity_name", "read_no_access_roster"),
            ("Member", "member_identity_name", "read_member_roster"),
        ],
    )
    def test_a_missing_mirror_identity_names_the_fix(
        self, monkeypatch, label, identity_field, read_mirror_roster
    ):
        """An environment provisioned before the identity existed must say
        which command creates it rather than echoing the ARM error."""
        mirror_identity = getattr(TARGET, identity_field)
        monkeypatch.setattr(
            onboard,
            "run_command",
            Shell(
                az=failed(
                    "(ResourceNotFound) The Resource 'Microsoft.ManagedIdentity/"
                    f"userAssignedIdentities/{mirror_identity}' was not found."
                )
            ),
        )

        with pytest.raises(OnboardError, match=f"{label} identity {mirror_identity} not found"):
            getattr(onboard, read_mirror_roster)(TARGET)

        assert getattr(onboard, read_mirror_roster)(replace(TARGET, **{identity_field: ""})) == ()

    @pytest.mark.parametrize("read_mirror_roster", ["read_no_access_roster", "read_member_roster"])
    def test_other_mirror_read_failures_pass_through(self, monkeypatch, read_mirror_roster):
        monkeypatch.setattr(onboard, "run_command", Shell(az=failed("AuthorizationFailed")))

        with pytest.raises(OnboardError, match="AuthorizationFailed"):
            getattr(onboard, read_mirror_roster)(TARGET)

    def test_load_target_derives_all_identity_names_from_the_cluster(self, monkeypatch):
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
        assert target.member_identity_name == "spi-stack-dev1-member"
        assert target.no_access().az_scope()[:2] == ["--identity-name", "spi-stack-dev1-noaccess"]
        assert target.member().az_scope()[:2] == ["--identity-name", "spi-stack-dev1-member"]
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
    member_roster: tuple[Credential, ...] = ()
    no_access_roster: tuple[Credential, ...] = ()
    protection: Protection = ABSENT
    values: dict = field(default_factory=lambda: dict.fromkeys(TARGET.values))
    projection: dict = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    fail: dict[str, list[str]] = field(default_factory=dict)
    projected: int = 0
    # What gh api repositories/<id> answers; empty means GitHub is unreadable.
    github: dict = field(default_factory=dict)
    sources: dict = field(default_factory=lambda: {"partition": COMMUNITY_SOURCE})
    source_projection: dict = field(default_factory=dict)

    def observed(self, *, values: bool = True) -> State:
        """What observe() would return; --skip-repo never reads the values."""

        return State(
            self.roster,
            self.projection,
            self.protection,
            dict(self.values) if values else {},
            no_access_roster=self.no_access_roster,
            member_roster=self.member_roster,
            sources=dict(self.sources),
            source_projection=dict(self.source_projection),
        )

    def _attr(self, identity_name: str) -> str:
        if identity_name == TARGET.member_identity_name:
            return "member_roster"
        if identity_name == TARGET.no_access_identity_name:
            return "no_access_roster"
        assert identity_name == TARGET.identity_name
        return "roster"

    def roster_of(self, target) -> tuple[Credential, ...]:
        return getattr(self, self._attr(target.identity_name))

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
            attr = self._attr(identity)
            current = getattr(self, attr)
            others = tuple(c for c in current if c.service != service)
            if argv[3] != "delete":
                subject = argv[argv.index("--subject") + 1]
                others += (cred(service, "", subject=subject),)
            setattr(self, attr, others)
        if argv[:3] == ["az", "group", "update"]:
            key, value = argv[argv.index("--set") + 1].removeprefix("tags.").split("=", 1)
            self.sources[key.removeprefix("spi-source-")] = value
        if argv[:2] == ["gh", "api"] and argv[2].startswith("repositories/"):
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.github), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def project(self, target, description="", named=None):
        self.projected += 1
        self.projection = onboard.roster_repos(self.roster, {**self.projection, **(named or {})})
        return self.projection

    def project_sources(self, target, description=""):
        self.source_projection = onboard.fork_sources(self.sources)
        return self.source_projection


@pytest.fixture
def live(monkeypatch):
    world = Live()
    monkeypatch.setattr(onboard, "run_command", world.run)
    monkeypatch.setattr(onboard, "read_roster", world.roster_of)
    monkeypatch.setattr(onboard, "read_protection", lambda repo: world.protection)
    monkeypatch.setattr(onboard, "read_values", lambda repo, org: dict(world.values))
    monkeypatch.setattr(onboard, "read_projection", lambda: dict(world.projection))
    monkeypatch.setattr(onboard, "project_roster", world.project)
    monkeypatch.setattr(onboard, "read_source_tags", lambda *a, **k: dict(world.sources))
    monkeypatch.setattr(onboard, "read_source_projection", lambda: dict(world.source_projection))
    monkeypatch.setattr(onboard, "project_sources", world.project_sources)
    monkeypatch.setattr(onboard.time, "sleep", lambda s: None)
    return world


class TestApply:
    def test_phases_run_in_order_and_rows_are_reobserved(self, live):
        plan = make_plan(live.observed())

        rows = apply_plan(plan)

        tools = [call[0] for call in live.calls]
        assert tools == ["gh"] * 6 + ["az"] * 3
        assert (
            live.member_roster == live.no_access_roster == live.roster == (cred("partition", REPO),)
        )
        assert live.projected == 1
        assert {row.state for row in rows} == {"correct"}
        assert next(r.detail for r in rows if r.item.startswith("AZURE_CLIENT_ID")) == "stamped"

    def test_a_repository_failure_names_the_pending_phases_and_writes_nothing_else(self, live):
        live.fail["gh api --method PUT"] = ["HTTP 403: admin required"]
        plan = make_plan(live.observed())

        with pytest.raises(OnboardError, match="admin required") as exc:
            apply_plan(plan)

        assert "Completed: nothing. Pending: repository, azure, source, cluster" in str(exc.value)
        assert live.roster == ()
        assert live.projected == 0

    def test_trust_is_never_enabled_when_the_rules_are_missing_at_write_time(self, live):
        live.protection = PROTECTED
        plan = make_plan(live.observed(values=False), skip_repo=True)
        live.protection = Protection(True, "protected branches only")

        with pytest.raises(OnboardError, match="does not protect spi-stack") as exc:
            apply_plan(plan)

        assert "Completed: nothing. Pending: azure, source, cluster" in str(exc.value)
        assert live.roster == ()

    def test_a_blocked_plan_refuses_to_apply(self, live):
        with pytest.raises(OnboardError, match="does not protect spi-stack"):
            apply_plan(make_plan(live.observed(values=False), skip_repo=True))

    def test_a_busy_identity_is_retried_after_backoff(self, live):
        live.protection = PROTECTED
        live.fail["az identity federated-credential create"] = ["Conflict: concurrent write"]

        rows = apply_plan(make_plan(live.observed(values=False), skip_repo=True))

        assert [c[3] for c in live.calls if c[0] == "az"] == ["create"] * 4
        assert (
            live.roster == live.member_roster == live.no_access_roster == (cred("partition", REPO),)
        )
        assert {row.state for row in rows} == {"correct"}

    def test_a_non_conflict_credential_failure_stops_after_one_attempt(self, live):
        live.protection = PROTECTED
        live.fail["az identity federated-credential create"] = ["AuthorizationFailed"]

        with pytest.raises(OnboardError, match="AuthorizationFailed") as exc:
            apply_plan(make_plan(live.observed(values=False), skip_repo=True))

        assert "Completed: nothing. Pending: azure, source, cluster" in str(exc.value)
        assert len([c for c in live.calls if c[0] == "az"]) == 1

    def test_remove_revokes_then_reprojects(self, live):
        live.roster = (cred("partition", REPO), cred("schema", "Acme/osdu-spi-schema"))
        live.member_roster = live.no_access_roster = live.roster
        live.projection = onboard.roster_repos(live.roster)

        rows = apply_plan(make_plan(live.observed(), repo="", remove=True))

        assert [c[3] for c in live.calls] == ["delete"] * 3
        assert live.projection == {"schema": "Acme/osdu-spi-schema"}
        assert (
            live.member_roster == live.no_access_roster == (cred("schema", "Acme/osdu-spi-schema"),)
        )
        assert [(r.state, r.detail) for r in rows[:3]] == [("correct", "absent")] * 3

    def test_nothing_to_change_touches_nothing(self, live):
        live.roster = live.member_roster = live.no_access_roster = (cred("partition", REPO),)
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

    def test_bootstrap_projects_sources_only_when_the_lock_disagrees(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            onboard,
            "read_source_tags",
            lambda rg: {"partition": REPO, "legal": COMMUNITY_SOURCE},
        )
        monkeypatch.setattr(onboard, "read_source_projection", lambda: {"partition": REPO})
        monkeypatch.setattr(onboard, "project_sources", lambda *a: calls.append(a) or {})

        assert onboard.sync_sources_from_tags("rg") == {"partition": REPO}
        assert calls == []

        monkeypatch.setattr(onboard, "read_source_projection", lambda: {})
        onboard.sync_sources_from_tags("rg")
        assert len(calls) == 1

    def test_list_pairs_trust_with_projection_and_flags_the_rest(self, monkeypatch):
        roster = (
            cred("partition", REPO),
            cred("schema", "Acme/osdu-spi-schema"),
            Credential(
                "by-hand", GITHUB_ISSUER, "repo:Acme/x:ref:refs/heads/main", (GITHUB_AUDIENCE,)
            ),
            Credential(
                "cluster-spi-test",
                "https://oidc.example/aks",
                "system:serviceaccount:spi-test:spi-deployer",
                (GITHUB_AUDIENCE,),
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
        assert rows["cluster-spi-test"] == (
            "correct",
            "cluster issuer; system:serviceaccount:spi-test:spi-deployer",
        )
        assert rows["fork-partition on spi-stack-dev1-noaccess"] == ("correct", REPO)
        assert rows["fork-partition on spi-stack-dev1-member"] == ("correct", REPO)

    @pytest.mark.parametrize(
        "mirror_identity, read_mirror_roster",
        [
            (TARGET.no_access_identity_name, "read_no_access_roster"),
            (TARGET.member_identity_name, "read_member_roster"),
        ],
    )
    def test_list_still_answers_when_a_mirror_identity_is_missing(
        self, monkeypatch, mirror_identity, read_mirror_roster
    ):
        """An environment provisioned before the identity existed keeps its
        deployer listing and gets one row naming the fix."""
        monkeypatch.setattr(onboard, "read_roster", lambda target: (cred("partition", REPO),))
        monkeypatch.setattr(onboard, "read_projection", lambda: {"partition": REPO})
        monkeypatch.setattr(
            onboard,
            read_mirror_roster,
            lambda target: (_ for _ in ()).throw(OnboardError(f"{mirror_identity} not found")),
        )

        rows = {row.item: (row.state, row.detail) for row in onboard.list_trust(TARGET)}

        assert rows["partition"] == ("correct", REPO)
        assert rows[mirror_identity][0] == "missing"
        assert "not found" in rows[mirror_identity][1]

    @pytest.mark.parametrize(
        "mirror_identity", [TARGET.no_access_identity_name, TARGET.member_identity_name]
    )
    def test_list_reports_a_mirror_identity_lagging_or_leading(self, monkeypatch, mirror_identity):
        deployer = (cred("partition", REPO), cred("schema", "Acme/osdu-spi-schema"))
        lagging = (cred("schema", "Acme/other-schema"), cred("legal", "Acme/legal"))
        monkeypatch.setattr(
            onboard,
            "read_roster",
            lambda target: lagging if target.identity_name == mirror_identity else deployer,
        )
        monkeypatch.setattr(onboard, "read_projection", lambda: onboard.roster_repos(deployer))

        rows = {row.item: (row.state, row.detail) for row in onboard.list_trust(TARGET)}

        assert rows[f"fork-partition on {mirror_identity}"][0] == "missing"
        assert rows[f"fork-schema on {mirror_identity}"] == (
            "drifted",
            "trusts Acme/other-schema, not Acme/osdu-spi-schema",
        )
        assert rows[f"fork-legal on {mirror_identity}"][0] == "drifted"


# ---------------------------------------------------------------------------
# Rendering and CLI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Canonical source: the spi-source-<service> tag and its lock projection
# ---------------------------------------------------------------------------

TRUSTED = (cred("partition", REPO),)
SOURCE_TAG = "spi-source-partition on spi-stack-dev1"
SOURCES_ROW = f"{CANONICAL_SOURCES_ANNOTATION} on osdu-image-lock"


def trusted_state(**overrides) -> State:
    state = State(
        TRUSTED, {"partition": REPO}, PROTECTED, member_roster=TRUSTED, no_access_roster=TRUSTED
    )
    return replace(state, **overrides)


def projected_sources(plan: Plan) -> dict:
    step = next(s for s in plan.steps if CANONICAL_SOURCES_ANNOTATION in s.argv[-1])
    return json.loads(step.argv[-1].split("=", 1)[1])


class TestSourcePolicy:
    def test_promotion_records_the_fork_then_projects_it(self):
        plan = make_plan(trusted_state(), skip_repo=True, canonical_source="fork")

        assert [s.phase for s in plan.steps] == ["source", "cluster"]
        assert plan.steps[0].argv[:7] == [
            "az",
            "group",
            "update",
            "--name",
            "spi-stack-dev1",
            "--set",
            f"tags.spi-source-partition={REPO}",
        ]
        assert plan.steps[0].argv[-2:] == ["--subscription", "sub-id"]
        assert projected_sources(plan) == {"partition": REPO}
        assert states(plan)[SOURCE_TAG] == "drifted"

    def test_an_omitted_option_keeps_a_recorded_fork(self):
        state = trusted_state(sources={"partition": REPO}, source_projection={"partition": REPO})

        plan = make_plan(state, skip_repo=True)

        assert plan.steps == []
        assert states(plan)[SOURCE_TAG] == "correct"

    def test_an_omitted_option_refuses_a_fork_recorded_for_another_repository(self):
        plan = make_plan(trusted_state(sources={"partition": "Old/fork"}), skip_repo=True)

        with pytest.raises(OnboardError, match="follows Old/fork"):
            refuse(plan)

    def test_community_keeps_trust_and_drops_only_this_service_from_the_projection(self):
        forks = {"partition": REPO, "legal": "Acme/osdu-spi-legal"}
        state = trusted_state(sources=forks, source_projection=forks)

        plan = make_plan(state, skip_repo=True, canonical_source="community")

        assert verbs(plan) == [
            "az group update --name",
            "kubectl annotate configmap osdu-image-lock",
        ]
        assert plan.steps[0].argv[6] == "tags.spi-source-partition=community"
        assert projected_sources(plan) == {"legal": "Acme/osdu-spi-legal"}

    def test_removal_records_community_before_revoking(self):
        state = trusted_state(sources={"partition": REPO}, source_projection={"partition": REPO})

        plan = make_plan(state, repo="", remove=True)

        assert [s.phase for s in plan.steps] == ["source"] + ["azure"] * 3 + ["cluster"] * 2
        assert plan.steps[0].argv[6] == "tags.spi-source-partition=community"
        assert projected_sources(plan) == {}

    def test_apply_writes_the_tag_then_the_projection(self, live):
        live.roster = live.member_roster = live.no_access_roster = TRUSTED
        live.projection = {"partition": REPO}
        live.protection = PROTECTED

        rows = apply_plan(
            make_plan(live.observed(values=False), skip_repo=True, canonical_source="fork")
        )

        assert [call[:3] for call in live.calls] == [["az", "group", "update"]]
        assert live.sources == {"partition": REPO}
        assert live.source_projection == {"partition": REPO}
        assert {row.state for row in rows} == {"correct"}

    def test_a_removal_that_cannot_record_community_revokes_nothing(self, live):
        live.roster = live.member_roster = live.no_access_roster = TRUSTED
        live.sources = {"partition": REPO}
        live.fail["az group update"] = ["AuthorizationFailed"]

        with pytest.raises(OnboardError, match="Pending: source, azure, cluster"):
            apply_plan(make_plan(live.observed(), repo="", remove=True))

        assert live.roster == TRUSTED


class TestPromotionCheck:
    def _fork_image(self, monkeypatch, service: str, loader=None):
        from spi.images import ResolvedImage

        image = ResolvedImage(
            service, f"ghcr.io/acme/osdu-spi-{service}", "sha-bbbbbbbbbbbb", "", "sha256:d"
        )
        monkeypatch.setattr(onboard, "resolve_fork_image", lambda svc, repo: (image, "b" * 40))
        monkeypatch.setattr(onboard, "resolve_fork_loader", lambda repository, commit: loader)

    def test_a_promotion_names_the_image_the_next_refresh_resolves(self, monkeypatch):
        self._fork_image(monkeypatch, "partition")

        assert onboard.check_promotion("partition", REPO) == (
            "ghcr.io/acme/osdu-spi-partition:sha-bbbbbbbbbbbb"
        )

    def test_schema_without_its_loader_is_refused_naming_the_package(self, monkeypatch):
        self._fork_image(monkeypatch, "schema", loader=None)

        with pytest.raises(OnboardError, match="osdu-spi-schema-load:sha-bbbbbbbbbbbb"):
            onboard.check_promotion("schema", "Acme/osdu-spi-schema")

    def test_an_unresolvable_fork_is_refused(self, monkeypatch):
        from spi.images import ImageResolutionError

        def unresolvable(service, repo):
            raise ImageResolutionError("publishes no main-snapshot image")

        monkeypatch.setattr(onboard, "resolve_fork_image", unresolvable)

        with pytest.raises(OnboardError, match="partition cannot follow .*main-snapshot"):
            onboard.check_promotion("partition", REPO)

    @pytest.mark.parametrize(
        "recorded, option, checked",
        [
            (COMMUNITY_SOURCE, "fork", True),
            (REPO, "fork", False),
            (REPO, "", False),
            (COMMUNITY_SOURCE, "", False),
        ],
    )
    def test_only_a_change_to_the_fork_is_checked(self, monkeypatch, recorded, option, checked):
        calls = []
        monkeypatch.setattr(onboard, "read_roster", lambda target: TRUSTED)
        monkeypatch.setattr(onboard, "resolve_repository", lambda spec: spec)
        monkeypatch.setattr(onboard, "read_subject", lambda repo: credential_subject(repo))
        monkeypatch.setattr(
            onboard, "observe", lambda *a, **k: trusted_state(sources={"partition": recorded})
        )
        monkeypatch.setattr(
            onboard, "check_promotion", lambda service, repo: calls.append(repo) or "img"
        )

        onboard.plan_onboard(TARGET, "partition", REPO, skip_repo=True, canonical_source=option)

        assert calls == ([REPO] if checked else [])


class TestSourceReads:
    def test_tags_parse_to_service_and_source(self):
        tags = {
            "spi-source-partition": REPO,
            "spi-source-legal": COMMUNITY_SOURCE,
            "spi-name-suffix": "90380",
        }

        assert onboard.parse_source_tags(tags) == {
            "partition": REPO,
            "legal": COMMUNITY_SOURCE,
        }

    def test_an_invalid_tag_is_an_error_not_community(self):
        with pytest.raises(OnboardError, match="neither community nor <owner>/<name>"):
            onboard.parse_source_tags({"spi-source-partition": "upstream"})

    def test_a_missing_group_reads_as_no_tags_only_when_tolerated(self, monkeypatch):
        monkeypatch.setattr(
            onboard, "run_command", lambda argv, **_: failed("(ResourceGroupNotFound) not found")
        )

        assert READ_SOURCE_TAGS("rg", missing_ok=True) == {}
        with pytest.raises(OnboardError, match="Could not read source tags"):
            READ_SOURCE_TAGS("rg")

    def test_tags_are_read_in_the_given_subscription(self, monkeypatch):
        shell = Shell(az__group__show={"spi-source-partition": REPO})
        monkeypatch.setattr(onboard, "run_command", shell)

        assert READ_SOURCE_TAGS("rg", "sub-id") == {"partition": REPO}
        assert shell.calls[0][-2:] == ["--subscription", "sub-id"]

    def test_the_deploy_policy_keeps_only_forks_the_identity_trusts(self, monkeypatch):
        monkeypatch.setattr(
            onboard, "read_source_tags", lambda *a, **k: {"partition": REPO, "legal": "community"}
        )
        monkeypatch.setattr(onboard, "read_roster", lambda target: ())
        monkeypatch.setattr(onboard, "roster_repos", lambda roster: {"partition": REPO.lower()})

        assert onboard.read_source_policy("rg", "deployer") == {"partition": REPO}

    @pytest.mark.parametrize("trusted", [{}, {"partition": "Other/osdu-spi-partition"}])
    def test_the_deploy_policy_refuses_a_fork_the_identity_does_not_trust(
        self, monkeypatch, trusted
    ):
        monkeypatch.setattr(onboard, "read_source_tags", lambda *a, **k: {"partition": REPO})
        monkeypatch.setattr(onboard, "read_roster", lambda target: ())
        monkeypatch.setattr(onboard, "roster_repos", lambda roster: trusted)

        with pytest.raises(OnboardError, match=f"partition follows {REPO} but"):
            onboard.read_source_policy("rg", "deployer")

    def test_a_community_policy_never_reads_the_roster(self, monkeypatch):
        monkeypatch.setattr(onboard, "read_source_tags", lambda *a, **k: {"legal": "community"})
        monkeypatch.setattr(onboard, "read_roster", pytest.fail)

        assert onboard.read_source_policy("rg", "deployer") == {}

    def test_list_checks_each_source_against_trust_and_the_projection(self):
        rows = onboard.source_rows(
            trusted={"partition": REPO, "legal": "Acme/osdu-spi-legal", "file": "Acme/file"},
            sources={"partition": REPO, "storage": "Acme/storage", "file": "Acme/file"},
            projection={"partition": REPO, "storage": "Acme/storage"},
        )

        assert [(r.item, r.state) for r in rows] == [
            ("spi-source-file", "drifted"),
            ("spi-source-legal", "missing"),
            ("spi-source-partition", "correct"),
            ("spi-source-storage", "drifted"),
        ]
        assert rows[0].detail == "Acme/file; projection community"
        assert rows[3].detail == "follows Acme/storage, which is not trusted for it"


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
            (["partition", "--remove", "--canonical-source", "fork"], "takes only the service"),
            (["partition", "--canonical-source", "upstream"], "expected fork or community"),
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

    @pytest.mark.parametrize(
        "subject",
        [
            "repo:Acme@1/osdu-spi-partition:environment:spi-stack",
            "repo:Acme/osdu-spi-partition@2:environment:spi-stack",
        ],
    )
    def test_ids_must_come_as_a_pair(self, subject):
        credential = cred("partition", REPO, subject=subject)

        assert credential.repo == ""
        assert not credential.well_formed
        assert onboard.roster_repos((credential,)) == {}

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

    def test_read_subject_uses_the_classic_form_when_no_prefix_is_reported(self, monkeypatch):
        shell = Shell()
        shell.add(f"gh__api__repos/{REPO}/actions/oidc/customization/sub", {"use_default": True})
        monkeypatch.setattr(onboard, "run_command", shell)

        assert onboard.read_subject(REPO) == credential_subject(REPO)

    def test_an_unreadable_subject_endpoint_stops_onboarding(self, monkeypatch):
        """A 404 can hide a token without access; guessing the form writes a dead credential."""
        monkeypatch.setattr(onboard, "run_command", Shell())

        with pytest.raises(OnboardError, match="Could not read OIDC subject"):
            onboard.read_subject(REPO)

    def test_a_custom_template_is_refused(self, monkeypatch):
        shell = Shell()
        shell.add(
            f"gh__api__repos/{REPO}/actions/oidc/customization/sub",
            {"use_default": False, "include_claim_keys": ["repo", "job_workflow_ref"]},
        )
        monkeypatch.setattr(onboard, "run_command", shell)

        with pytest.raises(OnboardError, match="customizes its OIDC subject"):
            onboard.read_subject(REPO)


# ---------------------------------------------------------------------------
# Custom templates: an organization can sign repository ids instead of the name
# ---------------------------------------------------------------------------

OWNER_ID, REPO_ID = "6844498", "1167996450"
ID_KEYS = ["repository_owner_id", "repository_id", "context"]
CLAIM_SUBJECT = f"repository_owner_id:{OWNER_ID}:repository_id:{REPO_ID}:environment:spi-stack"


@pytest.fixture(autouse=True)
def _forget_github_repositories():
    onboard.github_repository.cache_clear()
    yield
    onboard.github_repository.cache_clear()


def github(keys=ID_KEYS, owner_id: str = OWNER_ID) -> Shell:
    shell = Shell()
    shell.add(
        f"gh__api__repos/{REPO}/actions/oidc/customization/sub",
        {"use_default": False, "include_claim_keys": keys, "sub_claim_prefix": f"repo:{REPO}"},
    )
    repository = {"id": int(REPO_ID), "full_name": REPO, "owner": {"id": int(owner_id)}}
    shell.add(f"gh__api__repos/{REPO}", repository)
    shell.add(f"gh__api__repositories/{REPO_ID}", repository)
    return shell


def no_gh(argv, **_):
    raise FileNotFoundError(argv[0])


def claim_plan(state: State, service: str = "partition") -> Plan:
    state = replace(state, sources={service: COMMUNITY_SOURCE, **state.sources})
    plan = Plan(TARGET, service, REPO, subject=CLAIM_SUBJECT, skip_repo=True, state=state)
    plan.steps = plan_steps(plan)
    plan.rows = plan_rows(plan)
    return plan


class TestClaimKeyTemplate:
    def test_the_subject_follows_the_template_not_the_reported_prefix(self, monkeypatch):
        """The Azure organization's template; GitHub still reports repo:<owner>/<name>."""
        monkeypatch.setattr(onboard, "run_command", github())

        assert onboard.read_subject(REPO) == CLAIM_SUBJECT

    def test_the_subject_keeps_the_template_key_order(self, monkeypatch):
        monkeypatch.setattr(onboard, "run_command", github(keys=["context", "repository_id"]))

        subject = onboard.read_subject(REPO)

        assert subject == f"environment:spi-stack:repository_id:{REPO_ID}"
        assert cred("partition", REPO, subject=subject).ids == {"repository_id": REPO_ID}

    @pytest.mark.parametrize(
        "keys",
        [
            [],
            ["repository_id"],
            ["repository_owner_id", "context"],
            ["repository_id", "context", "job_workflow_ref"],
            ["repository_id", "repository_id", "context"],
        ],
    )
    def test_a_template_without_both_anchors_or_with_other_claims_is_refused(
        self, monkeypatch, keys
    ):
        """Without context any workflow in the repository could mint the identity."""
        monkeypatch.setattr(onboard, "run_command", github(keys=keys))

        with pytest.raises(OnboardError, match="customizes its OIDC subject"):
            onboard.read_subject(REPO)

    @pytest.mark.parametrize(
        "subject",
        [
            f"repository_id:{REPO_ID}:environment:other",
            f"repository_owner_id:{OWNER_ID}:environment:spi-stack",
            f"repository_id:{REPO_ID}:ref:refs/heads/main",
            "repository_id:abc:environment:spi-stack",
            f"repository_id:{REPO_ID}:repository_id:1:environment:spi-stack",
        ],
    )
    def test_other_claim_subjects_are_not_this_clis(self, subject):
        credential = cred("partition", REPO, subject=subject)

        assert credential.ids == {}
        assert not credential.well_formed

    def test_an_id_credential_resolves_its_name_through_github(self, monkeypatch):
        credential = cred("partition", REPO, subject=CLAIM_SUBJECT)
        monkeypatch.setattr(onboard, "run_command", github())

        assert credential.repo == ""
        assert credential.well_formed
        assert onboard.roster_repos((credential,)) == {"partition": REPO}

    @pytest.mark.parametrize("runner", [Shell(), no_gh], ids=["unreadable", "no-gh"])
    def test_without_github_the_last_projected_name_is_kept(self, monkeypatch, runner):
        """spi up in CI or on a laptop without gh must not drop a trusted fork."""
        roster = (cred("partition", REPO, subject=CLAIM_SUBJECT),)
        monkeypatch.setattr(onboard, "run_command", runner)

        assert onboard.roster_repos(roster, {"partition": REPO}) == {"partition": REPO}
        assert onboard.roster_repos(roster) == {}

    def test_a_repository_under_another_owner_is_not_trusted(self, monkeypatch):
        """A transferred repository keeps its id, but GitHub signs the new owner's id."""
        roster = (cred("partition", REPO, subject=CLAIM_SUBJECT),)
        monkeypatch.setattr(onboard, "run_command", github(owner_id="1"))

        assert onboard.roster_repos(roster, {"partition": REPO}) == {}

    def test_a_fresh_repository_is_trusted_under_the_rendered_subject(self):
        plan = claim_plan(State((), {}, PROTECTED))

        creates = [s for s in plan.steps if s.phase == "azure"]
        assert [s.argv[s.argv.index("--subject") + 1] for s in creates] == [CLAIM_SUBJECT] * 3
        assert plan.steps[-1].argv[-1] == (
            f"{TRUSTED_REPOS_ANNOTATION}={json.dumps({'partition': REPO})}"
        )

    def test_a_matching_id_credential_needs_no_change(self, monkeypatch):
        roster = (cred("partition", REPO, subject=CLAIM_SUBJECT),)
        monkeypatch.setattr(onboard, "run_command", github())

        plan = claim_plan(
            State(
                roster,
                {"partition": REPO},
                PROTECTED,
                no_access_roster=roster,
                member_roster=roster,
            )
        )

        assert plan.steps == []
        assert {row.state for row in plan.rows} == {"correct"}

    def test_a_repository_already_backing_a_service_is_refused_by_its_subject(self, monkeypatch):
        monkeypatch.setattr(onboard, "run_command", Shell())
        plan = claim_plan(State((cred("schema", REPO, subject=CLAIM_SUBJECT),), {}, PROTECTED))

        with pytest.raises(OnboardError, match="already backs schema"):
            refuse(plan)

    def test_bootstrap_without_gh_leaves_the_projection_alone(self, monkeypatch):
        calls = []
        roster = (cred("partition", REPO, subject=CLAIM_SUBJECT),)
        monkeypatch.setattr(onboard, "run_command", no_gh)
        monkeypatch.setattr(onboard, "read_roster", lambda target: roster)
        monkeypatch.setattr(onboard, "read_projection", lambda: {"partition": REPO})
        monkeypatch.setattr(onboard, "project_roster", lambda *a: calls.append(a))

        assert onboard.sync_projection_from_identity("id", "rg") == {"partition": REPO}
        assert calls == []

    def test_project_roster_names_an_id_credential_from_github(self, monkeypatch):
        lock = {"data": {}, "metadata": {"annotations": {}}}
        written = {}
        roster = (cred("partition", REPO, subject=CLAIM_SUBJECT),)
        monkeypatch.setattr(onboard, "run_command", github())
        monkeypatch.setattr(onboard, "read_roster", lambda target: roster)
        monkeypatch.setattr(
            onboard, "mutate_lock", lambda compute, description: written.update(compute(lock))
        )

        assert onboard.project_roster(TARGET) == {"partition": REPO}
        assert written["metadata"]["annotations"] == {
            TRUSTED_REPOS_ANNOTATION: json.dumps({"partition": REPO})
        }

    def test_list_names_an_id_credential(self, monkeypatch):
        roster = (cred("partition", REPO, subject=CLAIM_SUBJECT),)
        monkeypatch.setattr(onboard, "run_command", github())
        monkeypatch.setattr(onboard, "read_roster", lambda target: roster)
        monkeypatch.setattr(onboard, "read_projection", lambda: {"partition": REPO})

        rows = {row.item: (row.state, row.detail) for row in onboard.list_trust(TARGET)}

        assert rows["partition"] == ("correct", REPO)
        assert rows["fork-partition on spi-stack-dev1-member"] == ("correct", REPO)

    def test_the_apply_path_writes_and_reobserves_the_rendered_subject(self, live):
        live.protection = PROTECTED
        live.github = {"id": int(REPO_ID), "full_name": REPO, "owner": {"id": int(OWNER_ID)}}
        plan = Plan(TARGET, "partition", REPO, subject=CLAIM_SUBJECT, skip_repo=True)
        plan.state = live.observed(values=False)
        plan.steps = plan_steps(plan)
        plan.rows = plan_rows(plan)

        rows = apply_plan(plan)

        written = {c.subject for c in live.roster + live.member_roster + live.no_access_roster}
        assert written == {CLAIM_SUBJECT}
        assert live.projection == {"partition": REPO}
        assert {row.state for row in rows} == {"correct"}

    def test_refuse_resolves_an_id_credential_in_another_key_order(self, monkeypatch):
        monkeypatch.setattr(onboard, "run_command", github())
        other_order = (
            f"repository_id:{REPO_ID}:repository_owner_id:{OWNER_ID}:environment:spi-stack"
        )
        plan = claim_plan(State((cred("schema", REPO, subject=other_order),), {}, PROTECTED))

        with pytest.raises(OnboardError, match="already backs schema"):
            refuse(plan)

    def test_non_ascii_digits_are_not_ids(self):
        assert cred("partition", REPO, subject="repository_id:²:environment:spi-stack").ids == {}


class TestUnnamedCredentials:
    """An id credential GitHub cannot name is reported, never silently dropped or trusted."""

    def test_bootstrap_warns_about_each_unnamed_credential(self, monkeypatch, capsys):
        roster = (cred("partition", REPO, subject=CLAIM_SUBJECT),)
        monkeypatch.setattr(onboard, "run_command", no_gh)
        monkeypatch.setattr(onboard, "read_roster", lambda target: roster)
        monkeypatch.setattr(onboard, "read_projection", dict)
        monkeypatch.setattr(onboard, "project_roster", lambda *a: {})

        assert onboard.sync_projection_from_identity("id", "rg") == {}
        out = capsys.readouterr().out
        assert "fork-partition trusts" in out and "not projected" in out

    def test_list_marks_a_projected_name_unverified_and_an_unnamed_one_too(self, monkeypatch):
        roster = (
            cred("partition", REPO, subject=CLAIM_SUBJECT),
            cred("schema", REPO, subject="repository_id:7:environment:spi-stack"),
        )
        monkeypatch.setattr(onboard, "run_command", no_gh)
        monkeypatch.setattr(onboard, "read_roster", lambda target: roster)
        monkeypatch.setattr(onboard, "read_projection", lambda: {"partition": REPO})

        rows = {row.item: (row.state, row.detail) for row in onboard.list_trust(TARGET)}

        assert rows["partition"] == ("unverified", f"{REPO} per the projection; GitHub unreadable")
        assert rows["fork-schema"][0] == "unverified"
        assert "repository_id:7" in rows["fork-schema"][1]
        assert rows["fork-partition on spi-stack-dev1-member"] == ("correct", REPO)

    def test_list_marks_an_owner_mismatch_as_drift(self, monkeypatch):
        roster = (cred("partition", REPO, subject=CLAIM_SUBJECT),)
        monkeypatch.setattr(onboard, "run_command", github(owner_id="1"))
        monkeypatch.setattr(onboard, "read_roster", lambda target: roster)
        monkeypatch.setattr(onboard, "read_projection", dict)

        rows = {row.item: (row.state, row.detail) for row in onboard.list_trust(TARGET)}

        assert rows["fork-partition"][0] == "drifted"
        assert "owner id" in rows["fork-partition"][1]

    def test_list_compares_mirrors_by_subject_not_by_projected_name(self, monkeypatch):
        deployer = (cred("partition", REPO, subject=CLAIM_SUBJECT),)
        mirror = (cred("partition", REPO, subject="repository_id:999:environment:spi-stack"),)
        monkeypatch.setattr(onboard, "run_command", no_gh)
        monkeypatch.setattr(
            onboard,
            "read_roster",
            lambda target: deployer if target.identity_name == TARGET.identity_name else mirror,
        )
        monkeypatch.setattr(onboard, "read_projection", lambda: {"partition": REPO})

        rows = {row.item: (row.state, row.detail) for row in onboard.list_trust(TARGET)}

        assert rows["fork-partition on spi-stack-dev1-member"][0] == "drifted"
        assert "repository_id:999" in rows["fork-partition on spi-stack-dev1-member"][1]


class TestLookupFailures:
    """GitHub failing between plan and apply, or during --list, must not change trust."""

    def test_apply_projects_the_planned_name_over_a_stale_projection(self, live):
        live.protection = PROTECTED
        live.projection = {"partition": "Acme/old"}
        plan = Plan(TARGET, "partition", REPO, subject=CLAIM_SUBJECT, skip_repo=True)
        plan.state = live.observed(values=False)
        plan.steps = plan_steps(plan)
        plan.rows = plan_rows(plan)

        apply_plan(plan)

        assert live.projection == {"partition": REPO}

    def test_project_roster_prefers_a_planned_name_to_the_lock(self, monkeypatch):
        lock = {
            "data": {},
            "metadata": {"annotations": {TRUSTED_REPOS_ANNOTATION: '{"partition": "Acme/old"}'}},
        }
        written = {}
        roster = (cred("partition", REPO, subject=CLAIM_SUBJECT),)
        monkeypatch.setattr(onboard, "run_command", no_gh)
        monkeypatch.setattr(onboard, "read_roster", lambda target: roster)
        monkeypatch.setattr(
            onboard, "mutate_lock", lambda compute, description: written.update(compute(lock))
        )

        assert onboard.project_roster(TARGET, named={"partition": REPO}) == {"partition": REPO}
        assert onboard.project_roster(TARGET) == {"partition": "Acme/old"}

    def test_refuse_matches_repository_ids_when_github_is_unreadable(self, monkeypatch):
        monkeypatch.setattr(onboard, "run_command", no_gh)
        other_order = (
            f"repository_id:{REPO_ID}:repository_owner_id:{OWNER_ID}:environment:spi-stack"
        )
        plan = claim_plan(State((cred("schema", REPO, subject=other_order),), {}, PROTECTED))

        with pytest.raises(OnboardError, match="already backs schema"):
            refuse(plan)

    def test_list_compares_mirrors_to_an_unnamed_deployer_credential(self, monkeypatch):
        roster = (cred("partition", REPO, subject=CLAIM_SUBJECT),)
        monkeypatch.setattr(onboard, "run_command", no_gh)
        monkeypatch.setattr(onboard, "read_roster", lambda target: roster)
        monkeypatch.setattr(onboard, "read_projection", dict)

        rows = {row.item: (row.state, row.detail) for row in onboard.list_trust(TARGET)}

        assert rows["fork-partition on spi-stack-dev1-member"] == ("correct", CLAIM_SUBJECT)
        assert rows["fork-partition on spi-stack-dev1-noaccess"] == ("correct", CLAIM_SUBJECT)
        assert not any("does not" in detail for _, detail in rows.values())

    def test_list_does_not_call_a_hand_made_credential_a_transfer(self, monkeypatch):
        roster = (
            Credential("by-hand", GITHUB_ISSUER, credential_subject("Acme/x"), (GITHUB_AUDIENCE,)),
        )
        monkeypatch.setattr(onboard, "run_command", no_gh)
        monkeypatch.setattr(onboard, "read_roster", lambda target: roster)
        monkeypatch.setattr(onboard, "read_projection", dict)

        rows = {row.item: (row.state, row.detail) for row in onboard.list_trust(TARGET)}

        assert rows["by-hand"][0] == "unverified"
        assert "owner id" not in rows["by-hand"][1]
