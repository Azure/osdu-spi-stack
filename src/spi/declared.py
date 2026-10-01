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

"""Reconcile a declared environment's trust and source policy to its ``forks:`` list.

The declaration is authoritative; the federated credentials on the deploy,
member, and no-access identities and the ``spi-source-<service>`` tags are
copies of it. One observation of those copies and of what GitHub reports for
each declared repository yields the commands that restore what is declared
and revoke what is not, which ``spi up`` and ``spi onboard --reconcile``
apply before the lock projections are rebuilt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rich.syntax import Syntax

from .console import console
from .environment import Declared, ForkDeclaration
from .onboard import (
    COMMUNITY_SOURCE,
    DEPLOY_ENVIRONMENT,
    FORK_SOURCE,
    REQUIRED_PROFILE,
    Credential,
    OnboardError,
    Protection,
    Row,
    State,
    Step,
    Target,
    _quote,
    check_promotion,
    credential_name,
    credential_step,
    find_credential,
    identity_rosters,
    project_roster,
    project_sources,
    projection_row,
    projection_step,
    read_projection,
    read_protection,
    read_roster,
    read_source_projection,
    read_source_tags,
    read_subject,
    record_source_step,
    render_rows,
    resolve_repository,
    revoke_step,
    roster_repos,
    run_step,
    source_tag,
    write_credential,
)
from .pins import CANONICAL_SOURCES_ANNOTATION, TRUSTED_REPOS_ANNOTATION


@dataclass(frozen=True)
class Intent:
    """One declared fork, with what GitHub and GHCR report about it."""

    service: str
    # GitHub's stored casing when it could be read, the declared spelling otherwise.
    repo: str
    follows_fork: bool = False
    # Empty when GitHub did not report what it signs for the repository.
    subject: str = ""
    protection: Optional[Protection] = None
    # Why the subject or the protection rules could not be read.
    unread: str = ""
    # Why the fork's image cannot become canonical now; set only when the tag must move to it.
    unresolvable: str = ""


@dataclass(frozen=True)
class Action:
    """One command of the plan; a credential write names its identity so apply can retry it."""

    step: Step
    identity: Optional[Target] = None
    service: str = ""
    repo: str = ""
    # Empty on a credential write revokes it.
    subject: str = ""


@dataclass
class Reconciliation:
    target: Target
    declared: Declared
    intents: tuple[Intent, ...]
    state: State
    actions: list[Action] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    # Declared entries that could not be reconciled, each with its reason.
    blocked: list[str] = field(default_factory=list)
    # Service to repository the roster trusts once the actions run.
    trusted: dict[str, str] = field(default_factory=dict)
    # Service to the fork it follows once the actions run.
    sources: dict[str, str] = field(default_factory=dict)


def bootstrap_target(
    env: str, resource_group: str, deployer: str, member: str, no_access: str
) -> Target:
    """The identities ``spi up`` reconciles, named from its config instead of the cluster."""

    return Target(
        env,
        REQUIRED_PROFILE,
        deployer,
        resource_group,
        values={},
        no_access_identity_name=no_access,
        member_identity_name=member,
    )


def observe(target: Target, *, lock: bool = True, missing_ok: bool = False) -> State:
    """The durable records, and the lock's copies when a cluster holds one.

    ``missing_ok`` reads identities and a group that do not exist yet as
    empty, for the check ``spi up`` runs before a first provision.
    """

    return State(
        roster=read_roster(target, missing_ok=missing_ok),
        member_roster=(
            read_roster(target.member(), missing_ok=missing_ok)
            if target.member_identity_name
            else ()
        ),
        no_access_roster=(
            read_roster(target.no_access(), missing_ok=missing_ok)
            if target.no_access_identity_name
            else ()
        ),
        projection=read_projection() if lock else {},
        sources=read_source_tags(
            target.resource_group,
            target.values.get("AZURE_SUBSCRIPTION_ID", ""),
            missing_ok=missing_ok,
        ),
        source_projection=read_source_projection() if lock else {},
    )


def read_intent(fork: ForkDeclaration, state: State, named: dict[str, str]) -> Intent:
    """What GitHub and GHCR report for one declared fork.

    An unreadable repository is not an error here: a credential that already
    names it stands, and only a write that needs the missing answer is blocked.
    """

    repo, subject, protection, unread = fork.repo, "", None, ""
    try:
        repo = resolve_repository(fork.repo)
        subject = read_subject(repo)
        protection = read_protection(repo)
    except OnboardError as exc:
        unread = str(exc)
        known = named.get(fork.service, "")
        repo = known if known.lower() == repo.lower() else repo
    follows_fork = fork.canonical_source == FORK_SOURCE
    unresolvable = ""
    if follows_fork and state.sources.get(fork.service, "").lower() != repo.lower():
        try:
            check_promotion(fork.service, repo)
        except OnboardError as exc:
            unresolvable = str(exc)
    return Intent(fork.service, repo, follows_fork, subject, protection, unread, unresolvable)


def _write_blocker(intent: Intent, subject: str) -> str:
    """Why a credential for this fork cannot be written now; empty when it can."""

    if not subject or intent.protection is None:
        return intent.unread or f"GitHub did not report what it signs for {intent.repo}"
    if not intent.protection.satisfied:
        return (
            f"{intent.repo} does not protect {DEPLOY_ENVIRONMENT} as an environment open to "
            "every branch"
        )
    return ""


def _named(cred: Credential) -> str:
    return cred.repo or cred.subject


def plan(
    target: Target,
    declared: Declared,
    intents: tuple[Intent, ...],
    state: State,
    named: dict[str, str],
    *,
    lock: bool = True,
) -> Reconciliation:
    """Pure: the actions and rows that move the observed copies to the declaration.

    ``named`` is service to repository for the deployer's current credentials.
    Every credential that trusts something other than its declared fork is
    revoked before any is written: Azure keeps a subject unique on an identity,
    so a repository moved to another service would otherwise collide, and a
    replaced repository must not keep its trust while its successor is blocked.
    """

    result = Reconciliation(target, declared, intents, state)
    actions, rows = result.actions, result.rows
    by_service = {intent.service: intent for intent in intents}
    identities = identity_rosters(target, state)

    # Without GitHub's answer, a credential that already names the repository is the subject.
    subjects: dict[str, str] = {}
    for intent in intents:
        deployer = find_credential(state.roster, intent.service)
        standing = (
            deployer is not None
            and deployer.well_formed
            and named.get(intent.service, "").lower() == intent.repo.lower()
        )
        subjects[intent.service] = intent.subject or (
            deployer.subject if deployer and standing else ""
        )

    def stale(cred: Credential) -> bool:
        """A declared service's credential that trusts something other than its declared fork."""

        intent = by_service[cred.service]
        subject = subjects[intent.service]
        if subject:
            return not cred.trusts(subject)
        other = named.get(intent.service, "")
        return bool(other) and other.lower() != intent.repo.lower()

    for service, value in sorted(state.sources.items()):
        if service in by_service or value == COMMUNITY_SOURCE:
            continue
        item = f"{source_tag(service)} on {target.resource_group}"
        rows.append(Row("source", item, "drifted", f"follows {value}; {service} is not declared"))
        actions.append(Action(record_source_step(target, service, COMMUNITY_SOURCE)))
    revoked: set[tuple[str, str]] = set()
    for identity, roster in identities:
        for cred in roster:
            if not cred.service:
                continue
            declared_service = cred.service in by_service
            if declared_service and not stale(cred):
                continue
            if not declared_service:
                item = f"{cred.name} on {identity.identity_name}"
                rows.append(Row("azure", item, "drifted", f"trusts {_named(cred)}; not declared"))
            revoke = revoke_step(identity, cred.service, roster)
            assert revoke is not None
            actions.append(Action(revoke, identity, cred.service))
            revoked.add((identity.identity_name, cred.service))

    for intent in intents:
        subject = subjects[intent.service]
        blocker = _write_blocker(intent, subject)
        trusted, stranded = True, False
        for identity, roster in identities:
            existing = find_credential(roster, intent.service)
            item = f"{credential_name(intent.service)} on {identity.identity_name}"
            replaced = (identity.identity_name, intent.service) in revoked
            if existing is not None and not replaced and subject:
                if intent.subject:
                    rows.append(Row("azure", item, "correct", subject))
                else:
                    rows.append(Row("azure", item, "unverified", f"{subject}; {intent.unread}"))
                continue
            if existing is None:
                rows.append(Row("azure", item, "missing", f"declares {intent.repo}"))
            else:
                rows.append(Row("azure", item, "drifted", existing.subject))
            if blocker:
                stranded = True
                trusted = trusted and identity is not target
                continue
            remaining = tuple(cred for cred in roster if cred is not existing)
            write = credential_step(identity, intent.service, intent.repo, subject, remaining)
            assert write is not None
            actions.append(Action(write, identity, intent.service, intent.repo, subject))
        if stranded:
            result.blocked.append(f"{intent.service} from {intent.repo}: {blocker}")
        if trusted:
            result.trusted[intent.service] = intent.repo

        observed = state.sources.get(intent.service)
        item = f"{source_tag(intent.service)} on {target.resource_group}"
        follows = (
            intent.follows_fork
            and trusted
            and not (intent.unresolvable and observed != intent.repo)
        )
        if intent.follows_fork and trusted and not follows:
            result.blocked.append(f"{intent.service} from {intent.repo}: {intent.unresolvable}")
        # A fork that cannot be followed leaves the tag alone unless it names a fork.
        desired = intent.repo if follows else COMMUNITY_SOURCE
        if follows:
            result.sources[intent.service] = desired
        if intent.follows_fork and not follows and observed in (None, COMMUNITY_SOURCE):
            rows.append(Row("source", item, "missing", f"records {intent.repo}"))
        elif observed == desired:
            rows.append(Row("source", item, "correct", desired))
        elif observed is None:
            rows.append(Row("source", item, "missing", f"records {desired}"))
            actions.append(Action(record_source_step(target, intent.service, desired)))
        else:
            rows.append(Row("source", item, "drifted", f"is {observed}, records {desired}"))
            actions.append(Action(record_source_step(target, intent.service, desired)))

    if lock:
        for desired_map, observed_map, annotation in (
            (result.trusted, state.projection, TRUSTED_REPOS_ANNOTATION),
            (result.sources, state.source_projection, CANONICAL_SOURCES_ANNOTATION),
        ):
            rows.append(projection_row(desired_map, observed_map, annotation))
            step = projection_step(desired_map, observed_map, annotation)
            if step is not None:
                actions.append(Action(step))
    return result


def build(
    target: Target, declared: Declared, *, lock: bool = True, missing_ok: bool = False
) -> Reconciliation:
    """Observe once, ask GitHub about each declared fork, and plan."""

    state = observe(target, lock=lock, missing_ok=missing_ok)
    named = roster_repos(state.roster, state.projection)
    intents = tuple(read_intent(fork, state, named) for fork in declared.declaration.forks)
    return plan(target, declared, intents, state, named, lock=lock)


def render(result: Reconciliation) -> None:
    env = result.target.env or "the environment"
    console.print(
        f"\n[bold]Reconcile {env} to {result.declared.locator}[/bold]  "
        "(plan; pass --write to apply)"
    )
    render_rows(result.rows, "Observed state")
    for reason in result.blocked:
        console.print(f"[warning]Not reconciled: {reason}[/warning]")
    if not result.actions:
        if not result.blocked:
            console.print("[success]Nothing to change; every row is correct.[/success]")
        return
    script = "\n".join(f"# {a.step.description}\n{_quote(a.step.argv)}" for a in result.actions)
    console.print(Syntax(script, "bash", theme="monokai", word_wrap=True))


def apply(result: Reconciliation) -> None:
    """Run the actions in order, credential writes serially per identity.

    What can be reconciled is, and the entries that could not are raised
    afterwards, so one fork's missing rules do not strand the others.
    """

    for action in result.actions:
        identity = action.identity
        if action.step.phase == "cluster":
            continue
        if identity is None:
            run_step(action.step)
        elif action.subject:
            write_credential(
                identity,
                lambda roster, a=action, i=identity: credential_step(
                    i, a.service, a.repo, a.subject, roster
                ),
            )
        else:
            write_credential(
                identity, lambda roster, a=action, i=identity: revoke_step(i, a.service, roster)
            )
    if any(action.step.phase == "cluster" for action in result.actions):
        project_roster(result.target, named=result.trusted)
        project_sources(result.target)
    if result.blocked:
        raise OnboardError(
            f"{result.declared.locator} is not fully reconciled: " + "; ".join(result.blocked)
        )
