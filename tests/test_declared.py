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

"""Declared forks: planning is pure over an observed State; apply runs the actions in order."""

import subprocess
from dataclasses import replace

import pytest

from spi import declared, onboard
from spi.declared import Intent, plan
from spi.environment import DeclarationLocator, Declared, parse_declaration
from spi.onboard import (
    GITHUB_AUDIENCE,
    GITHUB_ISSUER,
    MAX_CREDENTIALS,
    Credential,
    OnboardError,
    Protection,
    State,
    Target,
    credential_subject,
)
from spi.pins import CANONICAL_SOURCES_ANNOTATION, TRUSTED_REPOS_ANNOTATION

PARTITION, LEGAL = "Acme/osdu-spi-partition", "Acme/osdu-spi-legal"
DEPLOYER, MEMBER, NO_ACCESS = (
    "spi-stack-dev1-deployer",
    "spi-stack-dev1-member",
    "spi-stack-dev1-noaccess",
)
TARGET = Target(
    env="dev1",
    profile="core",
    identity_name=DEPLOYER,
    resource_group="spi-stack-dev1",
    no_access_identity_name=NO_ACCESS,
    member_identity_name=MEMBER,
    values={},
)
PROTECTED = Protection(exists=True)
LOCATOR = DeclarationLocator("Acme/ops", "environments/dev1.yaml")
BASE = """\
env: dev1
stackVersion: v0.24.0
profile: core
location: westus3
ingressMode: azure
imageBranch: master
nameSuffix: x7k2q
"""


def owner(*forks: tuple[str, str, str]) -> Declared:
    lines = "".join(
        f"  - service: {service}\n    repo: {repo}\n    canonicalSource: {source}\n"
        for service, repo, source in forks
    )
    return Declared(LOCATOR, parse_declaration(BASE + ("forks:\n" + lines if forks else "")))


def cred(service: str, repo: str) -> Credential:
    return Credential(
        f"fork-{service}", GITHUB_ISSUER, credential_subject(repo), (GITHUB_AUDIENCE,)
    )


def intent(service: str, repo: str, **overrides) -> Intent:
    return replace(
        Intent(service, repo, subject=credential_subject(repo), protection=PROTECTED), **overrides
    )


def everywhere(*creds: Credential, **overrides) -> State:
    """The same credentials on all three identities, as onboarding leaves them."""

    state = State(
        roster=creds,
        member_roster=creds,
        no_access_roster=creds,
        projection={c.service: c.repo for c in creds},
    )
    return replace(state, **overrides)


def reconcile(state: State, *intents: Intent, lock: bool = True):
    named = {c.service: c.repo for c in state.roster}
    return plan(TARGET, owner(), intents, state, named, lock=lock)


def commands(result) -> list[str]:
    """Each action as verb, name, and identity or tag: enough to read the order."""

    out = []
    for action in result.actions:
        argv = action.step.argv
        if argv[:3] == ["az", "identity", "federated-credential"]:
            out.append(f"{argv[3]} {argv[5]} on {argv[argv.index('--identity-name') + 1]}")
        elif argv[:3] == ["az", "group", "update"]:
            out.append(argv[argv.index("--set") + 1])
        else:
            out.append(argv[-1].split("=", 1)[0])
    return out


def states(result, phase: str) -> list[str]:
    return [row.state for row in result.rows if row.phase == phase]


class TestPlan:
    def test_an_environment_matching_its_declaration_plans_nothing(self):
        state = everywhere(
            cred("partition", PARTITION),
            cred("legal", LEGAL),
            sources={"partition": "community", "legal": LEGAL},
            source_projection={"legal": LEGAL},
        )

        result = reconcile(
            state, intent("partition", PARTITION), intent("legal", LEGAL, follows_fork=True)
        )

        assert result.actions == [] and result.blocked == []
        assert {row.state for row in result.rows} == {"correct"}

    def test_a_credential_removed_by_hand_is_restored_on_that_identity_only(self):
        state = replace(
            everywhere(cred("partition", PARTITION), sources={"partition": "community"}),
            member_roster=(),
        )

        result = reconcile(state, intent("partition", PARTITION))

        assert commands(result) == [f"create fork-partition on {MEMBER}"]
        assert states(result, "azure") == ["correct", "missing", "correct"]
        assert result.actions[0].subject == credential_subject(PARTITION)

    def test_a_source_tag_removed_by_hand_is_recorded_again(self):
        state = everywhere(cred("legal", LEGAL), source_projection={"legal": LEGAL})

        result = reconcile(state, intent("legal", LEGAL, follows_fork=True))

        assert commands(result) == [f"tags.spi-source-legal={LEGAL}"]
        assert states(result, "source") == ["missing"]

    def test_undeclared_trust_returns_to_community_and_is_revoked_before_any_trust(self):
        # legal's repository moved to partition: Azure keeps a subject unique on an identity.
        state = everywhere(cred("legal", PARTITION), sources={"legal": PARTITION})

        result = reconcile(state, intent("partition", PARTITION))

        assert commands(result) == [
            "tags.spi-source-legal=community",
            f"delete fork-legal on {DEPLOYER}",
            f"delete fork-legal on {MEMBER}",
            f"delete fork-legal on {NO_ACCESS}",
            f"create fork-partition on {DEPLOYER}",
            f"create fork-partition on {MEMBER}",
            f"create fork-partition on {NO_ACCESS}",
            "tags.spi-source-partition=community",
            TRUSTED_REPOS_ANNOTATION,
        ]
        assert result.trusted == {"partition": PARTITION}

    def test_repositories_swapped_between_declared_services_are_revoked_before_either_is_written(
        self,
    ):
        state = everywhere(cred("partition", LEGAL), cred("legal", PARTITION))

        result = reconcile(state, intent("partition", PARTITION), intent("legal", LEGAL))

        azure = [c for c in commands(result) if " on " in c]
        assert [c.split(" ")[0] for c in azure] == ["delete"] * 6 + ["create"] * 6
        assert result.blocked == []

    def test_a_replaced_repository_loses_trust_and_source_even_when_its_successor_is_blocked(self):
        old = "Acme/old-legal"
        state = everywhere(
            cred("legal", old), sources={"legal": old}, source_projection={"legal": old}
        )
        successor = intent("legal", LEGAL, follows_fork=True, protection=Protection(exists=False))

        result = reconcile(state, successor)

        assert commands(result) == [
            f"delete fork-legal on {DEPLOYER}",
            f"delete fork-legal on {MEMBER}",
            f"delete fork-legal on {NO_ACCESS}",
            "tags.spi-source-legal=community",
            TRUSTED_REPOS_ANNOTATION,
            CANONICAL_SOURCES_ANNOTATION,
        ]
        assert result.trusted == {} and result.sources == {}
        assert len(result.blocked) == 1

    def test_a_source_tag_naming_an_untrusted_fork_returns_to_community(self):
        untrusted = intent("legal", LEGAL, follows_fork=True, protection=Protection(exists=False))

        result = reconcile(everywhere(sources={"legal": LEGAL}), untrusted)

        assert commands(result) == ["tags.spi-source-legal=community"]
        assert result.sources == {}

    def test_an_unprotected_repository_gets_no_credential_and_no_fork_source(self):
        unprotected = intent(
            "legal", LEGAL, follows_fork=True, protection=Protection(True, "only main (branch)")
        )

        result = reconcile(everywhere(), unprotected, intent("partition", PARTITION))

        assert [c for c in commands(result) if "legal" in c] == []
        assert f"create fork-partition on {DEPLOYER}" in commands(result)
        assert result.blocked == [
            f"legal from {LEGAL}: {LEGAL} does not protect spi-stack as an environment open "
            "to every branch"
        ]
        assert result.trusted == {"partition": PARTITION} and result.sources == {}

    def test_a_standing_credential_survives_an_unreadable_github(self):
        state = everywhere(cred("partition", PARTITION), sources={"partition": "community"})
        unread = intent("partition", PARTITION, subject="", protection=None, unread="HTTP 403")

        result = reconcile(state, unread)

        assert result.actions == [] and result.blocked == []
        assert states(result, "azure") == ["unverified"] * 3

    def test_an_unreadable_github_still_revokes_a_credential_naming_another_repository(self):
        state = everywhere(cred("partition", "Acme/replaced"))
        unread = intent("partition", PARTITION, subject="", protection=None, unread="HTTP 403")

        result = reconcile(state, unread)

        assert [c.split(" ")[0] for c in commands(result) if " on " in c] == ["delete"] * 3
        assert result.blocked == [f"partition from {PARTITION}: HTTP 403"]
        assert result.trusted == {}

    def test_an_unreadable_github_still_revokes_a_credential_this_cli_would_not_write(self):
        forged = replace(cred("partition", PARTITION), issuer="https://elsewhere.example")
        unread = intent("partition", PARTITION, subject="", protection=None, unread="HTTP 403")

        result = plan(TARGET, owner(), (unread,), everywhere(forged), {})

        assert [c.split(" ")[0] for c in commands(result) if " on " in c] == ["delete"] * 3
        assert result.trusted == {}

    def test_a_subject_another_credential_holds_is_refused_before_azure_rejects_it(self):
        by_hand = replace(cred("x", PARTITION), name="by-hand")
        state = replace(everywhere(), roster=(by_hand,))

        result = reconcile(state, intent("partition", PARTITION))

        # The mirrors follow the deployer, so none of the three is written.
        assert [c for c in commands(result) if " on " in c] == []
        assert result.blocked == [
            f"partition from {PARTITION}: by-hand on {DEPLOYER} already holds its subject"
        ]
        assert result.trusted == {}

    def test_an_identity_at_the_azure_maximum_gains_no_credential(self):
        full = tuple(
            Credential(f"other-{n}", "https://aks", f"system:serviceaccount:x:{n}", ())
            for n in range(MAX_CREDENTIALS)
        )
        state = replace(everywhere(), roster=full)

        result = reconcile(state, intent("partition", PARTITION))

        assert f"create fork-partition on {DEPLOYER}" not in commands(result)
        assert "already holds 20 federated credentials" in result.blocked[0]

    def test_an_unreadable_github_blocks_only_the_write_it_would_need(self):
        state = replace(
            everywhere(cred("partition", PARTITION), sources={"partition": "community"}),
            no_access_roster=(),
        )
        unread = intent("partition", PARTITION, subject="", protection=None, unread="HTTP 403")

        result = reconcile(state, unread)

        assert result.actions == []
        assert result.blocked == [f"partition from {PARTITION}: HTTP 403"]
        # The deployer still trusts the repository, so the lock keeps naming it.
        assert result.trusted == {"partition": PARTITION}

    def test_a_fork_that_cannot_resolve_is_trusted_but_stays_on_its_recorded_source(self):
        unresolved = intent(
            "legal", LEGAL, follows_fork=True, unresolvable="publishes no main-snapshot"
        )

        result = reconcile(everywhere(sources={"legal": "community"}), unresolved)

        assert [c for c in commands(result) if c.startswith("tags.")] == []
        assert f"create fork-legal on {DEPLOYER}" in commands(result)
        assert result.blocked == [f"legal from {LEGAL}: publishes no main-snapshot"]
        assert result.sources == {}

    def test_a_declaration_with_no_forks_revokes_every_fork_credential_only(self):
        cluster = Credential("cluster-spi-test", "https://aks", "system:serviceaccount:x:y", ())
        state = replace(
            everywhere(cred("partition", PARTITION)), roster=(cluster, cred("partition", PARTITION))
        )

        result = reconcile(state)

        assert commands(result) == [
            f"delete fork-partition on {DEPLOYER}",
            f"delete fork-partition on {MEMBER}",
            f"delete fork-partition on {NO_ACCESS}",
            TRUSTED_REPOS_ANNOTATION,
        ]

    def test_bootstrap_plans_the_durable_records_and_leaves_the_lock_to_spi_up(self):
        result = reconcile(everywhere(), intent("legal", LEGAL, follows_fork=True), lock=False)

        assert {row.phase for row in result.rows} == {"azure", "source"}
        assert all(action.step.phase != "cluster" for action in result.actions)
        assert result.sources == {"legal": LEGAL}

    def test_the_lock_projections_follow_what_will_be_trusted_and_followed(self):
        result = reconcile(everywhere(), intent("legal", LEGAL, follows_fork=True))

        projections = {a.step.argv[-1] for a in result.actions if a.step.phase == "cluster"}
        assert projections == {
            f'{TRUSTED_REPOS_ANNOTATION}={{"legal": "{LEGAL}"}}',
            f'{CANONICAL_SOURCES_ANNOTATION}={{"legal": "{LEGAL}"}}',
        }


class TestReadIntent:
    FORK = owner(("legal", "acme/OSDU-spi-legal", "fork")).declaration.forks[0]

    @pytest.fixture
    def github(self, monkeypatch):
        monkeypatch.setattr(declared, "resolve_repository", lambda spec: LEGAL)
        monkeypatch.setattr(declared, "read_subject", credential_subject)
        monkeypatch.setattr(declared, "read_protection", lambda repo: PROTECTED)
        checked = []
        monkeypatch.setattr(declared, "check_promotion", lambda s, r: checked.append(r) or "img")
        return checked

    def test_takes_the_casing_github_stores_and_checks_a_promotion(self, github):
        found = declared.read_intent(self.FORK, everywhere(), {})

        assert found == Intent("legal", LEGAL, True, credential_subject(LEGAL), PROTECTED)
        assert github == [LEGAL]

    def test_a_source_already_recorded_is_not_resolved_again(self, github):
        declared.read_intent(self.FORK, everywhere(sources={"legal": LEGAL}), {})

        assert github == []

    def test_an_unreadable_repository_keeps_the_name_its_credential_carries(
        self, github, monkeypatch
    ):
        def unreadable(spec):
            raise OnboardError("gh cannot read it")

        monkeypatch.setattr(declared, "resolve_repository", unreadable)

        found = declared.read_intent(
            self.FORK, everywhere(sources={"legal": LEGAL}), {"legal": LEGAL}
        )

        assert (found.repo, found.subject, found.protection) == (LEGAL, "", None)
        assert found.unread == "gh cannot read it"

    def test_a_refused_promotion_is_carried_not_raised(self, github, monkeypatch):
        def refuse(service, repo):
            raise OnboardError("schema cannot follow it")

        monkeypatch.setattr(declared, "check_promotion", refuse)

        assert declared.read_intent(self.FORK, everywhere(), {}).unresolvable == (
            "schema cannot follow it"
        )


class TestApply:
    @pytest.fixture
    def azure(self, monkeypatch):
        """Federated credentials and tags as az would keep them, written through run_command."""

        world: dict = {DEPLOYER: (), MEMBER: (), NO_ACCESS: (), "calls": [], "projected": []}

        def run(argv, **_):
            world["calls"].append(" ".join(argv[:4]))
            if argv[:3] == ["az", "identity", "federated-credential"]:
                identity = argv[argv.index("--identity-name") + 1]
                kept = tuple(c for c in world[identity] if c.name != argv[5])
                if argv[3] != "delete":
                    subject = argv[argv.index("--subject") + 1]
                    kept += (Credential(argv[5], GITHUB_ISSUER, subject, (GITHUB_AUDIENCE,)),)
                world[identity] = kept
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(onboard, "run_command", run)
        monkeypatch.setattr(onboard, "read_roster", lambda target, **_: world[target.identity_name])
        monkeypatch.setattr(
            declared,
            "project_roster",
            lambda target, named=None: world["projected"].append(named),
        )
        monkeypatch.setattr(declared, "project_sources", lambda target: None)
        return world

    def test_writes_each_identity_then_the_tag_then_projects(self, azure):
        result = reconcile(everywhere(), intent("legal", LEGAL, follows_fork=True))

        declared.apply(result)

        assert azure[DEPLOYER] == azure[MEMBER] == azure[NO_ACCESS] == (cred("legal", LEGAL),)
        assert azure["calls"] == ["az identity federated-credential create"] * 3 + [
            "az group update --name"
        ]
        assert azure["projected"] == [{"legal": LEGAL}]

    def test_a_blocked_entry_is_raised_after_the_others_are_reconciled(self, azure):
        result = reconcile(
            everywhere(),
            intent("legal", LEGAL, protection=Protection(exists=False)),
            intent("partition", PARTITION),
        )

        with pytest.raises(OnboardError, match="not fully reconciled: legal from"):
            declared.apply(result)

        assert azure[DEPLOYER] == (cred("partition", PARTITION),)

    def test_bootstrap_apply_leaves_the_lock_alone(self, azure):
        result = reconcile(everywhere(), intent("partition", PARTITION), lock=False)

        declared.apply(result)

        assert azure["projected"] == []
