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

"""Trust a fork repository against the connected environment.

A repository earns deploy access when the environment's deploy identity
carries a federated credential for the repository's protected ``spi-stack``
GitHub environment. Onboarding observes the repository, the identity, and
the image lock once, derives the commands that move each from observed to
desired, prints them, and runs them only with ``--write``.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from rich.syntax import Syntax
from rich.table import Table

from .console import console
from .images import IMAGE_REGISTRY, SCHEMA_LOAD_SERVICE_NAME
from .pins import TRUSTED_REPOS_ANNOTATION, PinError, decode_trusted_repos, mutate_lock, read_lock
from .shell import run_command

GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_AUDIENCE = "api://AzureADTokenExchange"
DEPLOY_ENVIRONMENT = "spi-stack"
CREDENTIAL_PREFIX = "fork-"
# Azure allows twenty federated credentials per user-assigned identity.
MAX_CREDENTIALS = 20
CLIENT_ID_SECRET = "AZURE_CLIENT_ID"
VARIABLE_NAMES = (
    "AZURE_TENANT_ID",
    "AZURE_SUBSCRIPTION_ID",
    "SPI_STACK_RESOURCE_GROUP",
    "SPI_STACK_CLUSTER",
)
REQUIRED_PROFILE = "core"
CONFLICT_BACKOFF_SECONDS = (5, 15, 30)
PHASES = ("repository", "azure", "cluster")

_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/(?!\.\.?$)[A-Za-z0-9_.-]+$")
# GitHub signs the subject as repo:<owner>/<name> or, by default since the
# immutable-subject change, repo:<owner>@<id>/<name>@<id>; both name one repository.
_SUBJECT_RE = re.compile(
    rf"^repo:([^:/@]+)(?:@\d+)?/([^:/@]+)(?:@\d+)?:environment:{DEPLOY_ENVIRONMENT}$"
)


class OnboardError(RuntimeError):
    """Onboarding refused or stopped; the message names what to change."""


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """The connected environment as ``spi info --json`` publishes it."""

    env: str
    profile: str
    identity_name: str
    resource_group: str
    values: dict[str, str]
    # The no-access identity carries the same federated credentials and no
    # role or group; fork CI mints its 403 caller from it.
    no_access_identity_name: str = ""

    def no_access(self) -> Target:
        return replace(self, identity_name=self.no_access_identity_name)

    def az_scope(self) -> list[str]:
        scope = ["--identity-name", self.identity_name, "--resource-group", self.resource_group]
        subscription = self.values.get("AZURE_SUBSCRIPTION_ID")
        return scope + (["--subscription", subscription] if subscription else [])


@dataclass(frozen=True)
class Credential:
    name: str
    issuer: str
    subject: str
    audiences: tuple[str, ...]

    @property
    def service(self) -> str:
        return (
            self.name[len(CREDENTIAL_PREFIX) :] if self.name.startswith(CREDENTIAL_PREFIX) else ""
        )

    @property
    def repo(self) -> str:
        match = _SUBJECT_RE.match(self.subject)
        if not match:
            return ""
        repo = f"{match.group(1)}/{match.group(2)}"
        return repo if _REPO_RE.match(repo) else ""

    @property
    def well_formed(self) -> bool:
        """A GitHub credential shaped the way this CLI writes them, whatever the subject form."""

        return (
            self.issuer == GITHUB_ISSUER
            and bool(self.repo)
            and set(self.audiences) == {GITHUB_AUDIENCE}
        )

    def trusts(self, subject: str) -> bool:
        """Exact match: Entra compares the subject string, so a form change is drift."""

        return self.well_formed and self.subject == subject


@dataclass(frozen=True)
class Protection:
    """What GitHub reports about the repository's ``spi-stack`` environment.

    The environment admits every branch. Write access is the boundary: a
    pull request from another repository runs without an OIDC token, so it
    cannot mint the deploy identity whatever the branch policy says. A
    restriction only keeps the lane off same-repo pull requests, which is
    drift.
    """

    exists: bool
    # A human-readable description of any branch restriction in force.
    restriction: str = ""

    @property
    def satisfied(self) -> bool:
        return self.exists and not self.restriction


@dataclass(frozen=True)
class State:
    """One observation of everything onboarding compares against."""

    roster: tuple[Credential, ...]
    projection: dict[str, str]
    protection: Optional[Protection] = None
    # Variables carry their value; a present secret reads "" because GitHub
    # never returns it; an absent name is None.
    values: dict[str, Optional[str]] = field(default_factory=dict)
    no_access_roster: tuple[Credential, ...] = ()

    def credential(self, service: str) -> Optional[Credential]:
        return find_credential(self.roster, service)

    def no_access_credential(self, service: str) -> Optional[Credential]:
        return find_credential(self.no_access_roster, service)


@dataclass(frozen=True)
class Step:
    phase: str
    argv: list[str]
    description: str


@dataclass(frozen=True)
class Row:
    phase: str
    item: str
    state: str
    detail: str = ""


@dataclass
class Plan:
    target: Target
    service: str
    repo: str
    subject: str = ""
    org: str = ""
    skip_repo: bool = False
    remove: bool = False
    state: State = field(default_factory=lambda: State((), {}))
    steps: list[Step] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """Trust is withheld until someone else establishes the rules."""

        return (
            self.skip_repo
            and self.state.protection is not None
            and not self.state.protection.satisfied
        )


def credential_name(service: str) -> str:
    return f"{CREDENTIAL_PREFIX}{service}"


def credential_subject(repo: str, prefix: str = "") -> str:
    """The federated subject for a repository's deploy environment.

    ``prefix`` is what GitHub reports it will sign for the repository
    (``sub_claim_prefix``); without it the classic ``repo:<owner>/<name>``.
    """

    return f"{prefix or f'repo:{repo}'}:environment:{DEPLOY_ENVIRONMENT}"


def find_credential(roster: tuple[Credential, ...], service: str) -> Optional[Credential]:
    name = credential_name(service)
    return next((cred for cred in roster if cred.name == name), None)


def roster_repos(roster: tuple[Credential, ...]) -> dict[str, str]:
    """Service to repository for the credentials shaped the way this CLI writes them."""

    return {c.service: c.repo for c in roster if c.service and c.well_formed}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _run_command(argv: list[str], **kwargs):
    try:
        return run_command(argv, check=False, **kwargs)
    except FileNotFoundError:
        raise OnboardError(f"{argv[0]} is not installed or not on PATH.") from None


def _read_json(argv: list[str], what: str, *, missing_ok: bool = False):
    """Run a read-only command and parse its JSON; a 404 is ``None`` when tolerated."""

    result = _run_command(argv, display=False)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        if missing_ok and ("404" in stderr or "Not Found" in stderr):
            return None
        raise OnboardError(f"Could not read {what}: {stderr or 'command failed'}")
    text = (result.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise OnboardError(f"Could not parse {what}: {exc}") from exc


def load_target() -> Target:
    from .info import collect_info

    info = collect_info()
    identity = info.get("deploy_identity") or {}
    environment = info.get("environment") or {}
    cluster = identity.get("cluster", "")
    return Target(
        env=environment.get("name", ""),
        profile=environment.get("profile", ""),
        identity_name=f"{cluster}-deployer" if cluster else "",
        resource_group=identity.get("resource_group", ""),
        no_access_identity_name=f"{cluster}-noaccess" if cluster else "",
        values={
            CLIENT_ID_SECRET: identity.get("client_id", ""),
            "AZURE_TENANT_ID": identity.get("tenant_id", ""),
            "AZURE_SUBSCRIPTION_ID": identity.get("subscription_id", ""),
            "SPI_STACK_RESOURCE_GROUP": identity.get("resource_group", ""),
            "SPI_STACK_CLUSTER": cluster,
        },
    )


def require_target(target: Target) -> None:
    if target.profile != REQUIRED_PROFILE:
        raise OnboardError(
            f"Onboarding needs a {REQUIRED_PROFILE} environment; the connected cluster reports "
            f"profile {target.profile or 'unknown'!r}, which deploys no OSDU services."
        )
    missing = sorted(name for name, value in target.values.items() if not value)
    if missing:
        raise OnboardError(
            f"The connected cluster does not publish the deploy identity values "
            f"({', '.join(missing)}); run 'spi up' on a release that provisions it first."
        )


def read_roster(target: Target) -> tuple[Credential, ...]:
    payload = _read_json(
        ["az", "identity", "federated-credential", "list", *target.az_scope(), "-o", "json"],
        f"federated credentials on {target.identity_name}",
    )
    roster = [
        Credential(
            name=str(item.get("name", "")),
            issuer=str(item.get("issuer", "")),
            subject=str(item.get("subject", "")),
            audiences=tuple(str(a) for a in item.get("audiences") or ()),
        )
        for item in payload or []
        if isinstance(item, dict)
    ]
    return tuple(sorted(roster, key=lambda c: c.name))


def read_no_access_roster(target: Target) -> tuple[Credential, ...]:
    """The no-access identity's roster; a missing identity names the fix."""

    if not target.no_access_identity_name:
        return ()
    try:
        return read_roster(target.no_access())
    except OnboardError as exc:
        if "not found" in str(exc).lower():
            raise OnboardError(
                f"No-access identity {target.no_access_identity_name} not found; run "
                "'spi up' on a release that provisions it first."
            ) from None
        raise


def resolve_repository(spec: str) -> str:
    """Return the repository's stored casing; Entra matches the subject exactly."""

    if not _REPO_RE.match(spec):
        raise OnboardError(f"--repo must be <owner>/<name>, got {spec!r}.")
    payload = _read_json(["gh", "api", f"repos/{spec}"], f"repository {spec}", missing_ok=True)
    if not isinstance(payload, dict) or not payload.get("full_name"):
        raise OnboardError(f"Repository {spec} was not found on GitHub, or gh cannot read it.")
    return str(payload["full_name"])


def read_subject(repo: str) -> str:
    """The subject GitHub signs for the repository's deploy environment.

    GitHub reports the prefix it uses for the repository; a custom template
    (``use_default`` false) is refused because the lane's login cannot
    predict what it would carry.
    """

    payload = _read_json(
        ["gh", "api", f"repos/{repo}/actions/oidc/customization/sub"],
        f"OIDC subject customization on {repo}",
        missing_ok=True,
    )
    if not isinstance(payload, dict):
        return credential_subject(repo)
    if payload.get("use_default") is False:
        raise OnboardError(
            f"{repo} customizes its OIDC subject claim; onboard trusts the default "
            "template only. Reset it with: gh api -X PUT "
            f"repos/{repo}/actions/oidc/customization/sub -F use_default=true"
        )
    prefix = str(payload.get("sub_claim_prefix") or "")
    return credential_subject(repo, prefix)


def read_protection(repo: str) -> Protection:
    env = _read_json(
        ["gh", "api", f"repos/{repo}/environments/{DEPLOY_ENVIRONMENT}"],
        f"environment {DEPLOY_ENVIRONMENT} on {repo}",
        missing_ok=True,
    )
    if not isinstance(env, dict):
        return Protection(exists=False)
    policy = env.get("deployment_branch_policy") or {}
    if policy.get("protected_branches"):
        return Protection(True, "protected branches only")
    if not policy.get("custom_branch_policies"):
        return Protection(True)
    # --paginate --slurp returns every page as one JSON array of page objects.
    pages = _read_json(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repo}/environments/{DEPLOY_ENVIRONMENT}/deployment-branch-policies",
        ],
        f"branch policies of {DEPLOY_ENVIRONMENT} on {repo}",
    )
    names = []
    for page in pages if isinstance(pages, list) else [pages]:
        for entry in (page or {}).get("branch_policies") or []:
            name, kind = str(entry.get("name", "")), str(entry.get("type") or "branch")
            if name:
                names.append(f"{name} ({kind})")
    return Protection(True, "only " + ", ".join(names) if names else "an empty policy list")


def read_values(repo: str, org: str) -> dict[str, Optional[str]]:
    scope = ["--org", org] if org else ["--repo", repo]
    variables = _read_json(
        ["gh", "variable", "list", *scope, "--json", "name,value,visibility"],
        f"variables on {org or repo}",
    )
    secrets = _read_json(
        ["gh", "secret", "list", *scope, "--json", "name"], f"secrets on {org or repo}"
    )
    observed: dict[str, Optional[str]] = dict.fromkeys((CLIENT_ID_SECRET, *VARIABLE_NAMES))
    for entry in variables or []:
        if entry.get("name") in VARIABLE_NAMES:
            value = str(entry.get("value", ""))
            visibility = str(entry.get("visibility") or "all").lower()
            # An organization value the fork cannot read is drift, not a match.
            observed[entry["name"]] = value if visibility == "all" else f"{value} ({visibility})"
    if any(entry.get("name") == CLIENT_ID_SECRET for entry in secrets or []):
        observed[CLIENT_ID_SECRET] = ""
    return observed


def read_projection() -> dict[str, str]:
    lock = read_lock(required=False)
    if lock is None:
        raise OnboardError(
            "The osdu-image-lock ConfigMap is missing; the cluster has not finished its "
            "first spi up, so there is nothing to project the roster into."
        )
    try:
        return decode_trusted_repos(lock)
    except PinError as exc:
        # The identity is authoritative; a corrupt projection is drift to overwrite.
        console.print(f"  [warning]{exc}[/warning]")
        return {}


def observe(
    target: Target,
    repo: str,
    org: str = "",
    *,
    values: bool = True,
    roster: Optional[tuple[Credential, ...]] = None,
) -> State:
    return State(
        roster=read_roster(target) if roster is None else roster,
        no_access_roster=read_no_access_roster(target),
        projection=read_projection(),
        protection=read_protection(repo) if repo else None,
        values=read_values(repo, org) if repo and values else {},
    )


# ---------------------------------------------------------------------------
# Planning: pure functions from observed state to steps and rows
# ---------------------------------------------------------------------------


def _env_api(repo: str, suffix: str = "") -> str:
    return f"repos/{repo}/environments/{DEPLOY_ENVIRONMENT}{suffix}"


def protection_steps(repo: str, protection: Protection) -> list[Step]:
    if protection.satisfied:
        return []
    verb = "Create" if not protection.exists else "Open"
    return [
        Step(
            "repository",
            [
                "gh",
                "api",
                "--method",
                "PUT",
                _env_api(repo),
                "-F",
                "deployment_branch_policy=null",
            ],
            f"{verb} the {DEPLOY_ENVIRONMENT} environment on {repo} to every branch",
        )
    ]


def value_steps(
    target: Target, repo: str, org: str, values: dict[str, Optional[str]]
) -> list[Step]:
    scope = ["--org", org, "--visibility", "all"] if org else ["--repo", repo]
    where = org or repo
    # A secret's value is unreadable, so it is stamped on every write.
    steps = [
        Step(
            "repository",
            [
                "gh",
                "secret",
                "set",
                CLIENT_ID_SECRET,
                *scope,
                "--body",
                target.values[CLIENT_ID_SECRET],
            ],
            f"Stamp {CLIENT_ID_SECRET} on {where}",
        )
    ]
    for name in VARIABLE_NAMES:
        if values.get(name) != target.values[name]:
            steps.append(
                Step(
                    "repository",
                    ["gh", "variable", "set", name, *scope, "--body", target.values[name]],
                    f"Set {name} on {where}",
                )
            )
    return steps


def credential_step(
    target: Target, service: str, repo: str, subject: str, roster: tuple[Credential, ...]
) -> Optional[Step]:
    existing = find_credential(roster, service)
    if existing is not None and existing.trusts(subject):
        return None
    verb = "create" if existing is None else "update"
    return Step(
        "azure",
        [
            "az",
            "identity",
            "federated-credential",
            verb,
            "--name",
            credential_name(service),
            *target.az_scope(),
            "--issuer",
            GITHUB_ISSUER,
            "--subject",
            subject,
            "--audiences",
            GITHUB_AUDIENCE,
        ],
        f"Trust {repo} for {service}",
    )


def revoke_step(target: Target, service: str, roster: tuple[Credential, ...]) -> Optional[Step]:
    existing = find_credential(roster, service)
    if existing is None:
        return None
    return Step(
        "azure",
        [
            "az",
            "identity",
            "federated-credential",
            "delete",
            "--name",
            credential_name(service),
            *target.az_scope(),
            "--yes",
        ],
        f"Revoke {existing.repo or existing.subject} for {service}",
    )


def projection_step(desired: dict[str, str], observed: dict[str, str]) -> Optional[Step]:
    if desired == observed:
        return None
    return Step(
        "cluster",
        [
            "kubectl",
            "annotate",
            "configmap",
            "osdu-image-lock",
            "-n",
            "osdu-flux",
            "--overwrite",
            f"{TRUSTED_REPOS_ANNOTATION}={json.dumps(desired, sort_keys=True)}",
        ],
        "Project the trusted-repository roster into the image lock "
        "(--write applies it through the lock's compare-and-retry patch)",
    )


def desired_projection(plan: Plan) -> dict[str, str]:
    desired = roster_repos(plan.state.roster)
    if plan.remove:
        desired.pop(plan.service, None)
    else:
        desired[plan.service] = plan.repo
    return desired


def protection_row(repo: str, protection: Protection) -> Row:
    item = f"{DEPLOY_ENVIRONMENT} environment on {repo}"
    if not protection.exists:
        return Row("repository", item, "missing")
    if protection.restriction:
        return Row("repository", item, "drifted", f"admits {protection.restriction}")
    return Row("repository", item, "correct", "every branch")


def value_rows(
    target: Target, where: str, values: dict[str, Optional[str]], *, stamped: bool = False
) -> list[Row]:
    secret = values.get(CLIENT_ID_SECRET)
    if stamped:
        rows = [Row("repository", f"{CLIENT_ID_SECRET} on {where}", "correct", "stamped")]
    elif secret is None:
        rows = [Row("repository", f"{CLIENT_ID_SECRET} on {where}", "missing")]
    else:
        rows = [
            Row(
                "repository",
                f"{CLIENT_ID_SECRET} on {where}",
                "unverified",
                "GitHub does not return secret values",
            )
        ]
    for name in VARIABLE_NAMES:
        observed = values.get(name)
        if observed is None:
            rows.append(Row("repository", f"{name} on {where}", "missing"))
        elif observed == target.values[name]:
            rows.append(Row("repository", f"{name} on {where}", "correct"))
        else:
            rows.append(Row("repository", f"{name} on {where}", "drifted", f"is {observed!r}"))
    return rows


def credential_row(plan: Plan, target: Target, roster: tuple[Credential, ...]) -> Row:
    existing = find_credential(roster, plan.service)
    item = f"{credential_name(plan.service)} on {target.identity_name}"
    if plan.remove:
        if existing is None:
            return Row("azure", item, "correct", "absent")
        return Row("azure", item, "drifted", f"trusts {existing.repo or existing.subject}")
    if existing is None:
        return Row("azure", item, "missing")
    if existing.trusts(plan.subject):
        return Row("azure", item, "correct", plan.subject)
    return Row("azure", item, "drifted", existing.subject)


def projection_row(desired: dict[str, str], observed: dict[str, str]) -> Row:
    item = f"{TRUSTED_REPOS_ANNOTATION} on osdu-image-lock"
    if desired == observed:
        return Row("cluster", item, "correct", json.dumps(desired, sort_keys=True))
    return Row("cluster", item, "drifted" if observed else "missing", json.dumps(observed))


def plan_rows(plan: Plan, *, stamped: bool = False) -> list[Row]:
    rows: list[Row] = []
    if plan.state.protection is not None:
        rows.append(protection_row(plan.repo, plan.state.protection))
    if plan.state.values:
        rows.extend(
            value_rows(plan.target, plan.org or plan.repo, plan.state.values, stamped=stamped)
        )
    rows.extend(credential_row(plan, target, roster) for target, roster in _identity_rosters(plan))
    rows.append(projection_row(desired_projection(plan), plan.state.projection))
    return rows


def plan_steps(plan: Plan) -> list[Step]:
    steps: list[Step] = []
    if plan.remove:
        for target, roster in _identity_rosters(plan):
            revoke = revoke_step(target, plan.service, roster)
            steps.extend([revoke] if revoke else [])
    else:
        assert plan.state.protection is not None
        if not plan.skip_repo:
            steps.extend(protection_steps(plan.repo, plan.state.protection))
            steps.extend(value_steps(plan.target, plan.repo, plan.org, plan.state.values))
        # Trust never precedes the rules; without --skip-repo the steps above establish them.
        if plan.blocked:
            return steps
        for target, roster in _identity_rosters(plan):
            trust = credential_step(target, plan.service, plan.repo, plan.subject, roster)
            steps.extend([trust] if trust else [])
    projection = projection_step(desired_projection(plan), plan.state.projection)
    steps.extend([projection] if projection else [])
    return steps


def _identity_rosters(plan: Plan) -> list[tuple[Target, tuple[Credential, ...]]]:
    """The deployer first, then the no-access identity when the target names one."""

    pairs = [(plan.target, plan.state.roster)]
    if plan.target.no_access_identity_name:
        pairs.append((plan.target.no_access(), plan.state.no_access_roster))
    return pairs


def refuse(plan: Plan) -> None:
    """Refusals that must land before any phase writes."""

    owner = plan.repo.split("/", 1)[0]
    if plan.org and plan.org.lower() != owner.lower():
        raise OnboardError(
            f"--org {plan.org} does not own {plan.repo}; organization values are only "
            "visible to that organization's repositories."
        )
    mine = credential_name(plan.service)
    for cred in plan.state.roster:
        if cred.name != mine and cred.repo.lower() == plan.repo.lower():
            raise OnboardError(
                f"{plan.repo} already backs {cred.service or cred.name}; one repository "
                "backs one service. Remove that credential first."
            )
    for target, roster in _identity_rosters(plan):
        if find_credential(roster, plan.service) is None and len(roster) >= MAX_CREDENTIALS:
            raise OnboardError(
                f"{target.identity_name} already holds {MAX_CREDENTIALS} federated "
                "credentials, the Azure maximum; a larger roster is a new decision."
            )


def require_known_service(service: str) -> None:
    if service not in IMAGE_REGISTRY or service == SCHEMA_LOAD_SERVICE_NAME:
        known = ", ".join(sorted(n for n in IMAGE_REGISTRY if n != SCHEMA_LOAD_SERVICE_NAME))
        raise OnboardError(f"Unknown service {service!r}. Known services: {known}")


def plan_onboard(
    target: Target, service: str, repo_spec: str = "", *, org: str = "", skip_repo: bool = False
) -> Plan:
    require_target(target)
    require_known_service(service)
    roster = read_roster(target)
    if not repo_spec:
        existing = find_credential(roster, service)
        if existing is None or not existing.repo:
            raise OnboardError(f"{service} is not trusted yet; pass --repo <owner>/<name>.")
        repo_spec = existing.repo
    repo = resolve_repository(repo_spec)
    plan = Plan(target, service, repo, subject=read_subject(repo), org=org, skip_repo=skip_repo)
    plan.state = observe(target, repo, org, values=not skip_repo, roster=roster)
    refuse(plan)
    plan.steps = plan_steps(plan)
    plan.rows = plan_rows(plan)
    return plan


def plan_remove(target: Target, service: str) -> Plan:
    require_target(target)
    require_known_service(service)
    plan = Plan(target, service, repo="", skip_repo=True, remove=True)
    plan.state = observe(target, repo="")
    plan.steps = plan_steps(plan)
    plan.rows = plan_rows(plan)
    return plan


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_STATE_STYLES = {"correct": "green", "drifted": "yellow", "missing": "red", "unverified": "dim"}
_PHASE_TITLES = {
    "repository": "Phase 1: repository (gh)",
    "azure": "Phase 2: trust (az)",
    "cluster": "Phase 3: cluster projection (kubectl)",
}


def render_rows(rows: list[Row], title: str) -> None:
    table = Table(title=title, show_lines=False)
    for column in ("Phase", "Item", "State", "Detail"):
        table.add_column(column, overflow="fold")
    for row in rows:
        style = _STATE_STYLES.get(row.state, "white")
        table.add_row(row.phase, row.item, f"[{style}]{row.state}[/{style}]", row.detail)
    console.print(table)


def render_plan(plan: Plan) -> None:
    env = plan.target.env or "the environment"
    action = (
        f"Remove trust for {plan.service} on {env}"
        if plan.remove
        else f"Onboard {plan.service} from {plan.repo} to {env}"
    )
    console.print(f"\n[bold]{action}[/bold]  (plan; pass --write to apply)")
    render_rows(plan.rows, "Observed state")
    if plan.blocked:
        console.print(
            "\n[warning]Trust steps are withheld until the repository owner establishes the "
            f"{DEPLOY_ENVIRONMENT} environment rules; re-run afterwards.[/warning]"
        )
        return
    if not plan.steps:
        console.print("[success]Nothing to change; every row is correct or unverified.[/success]")
        return
    for phase in PHASES:
        steps = [s for s in plan.steps if s.phase == phase]
        if not steps:
            continue
        console.print(f"\n[bold]{_PHASE_TITLES[phase]}[/bold]")
        script = "\n".join(f"# {s.description}\n{_quote(s.argv)}" for s in steps)
        console.print(Syntax(script, "bash", theme="monokai", word_wrap=True))
    if plan.skip_repo and not plan.remove:
        console.print(
            "\n[dim]--skip-repo: GitHub values are left to the repository's owner; the "
            f"{DEPLOY_ENVIRONMENT} environment rules are still required before trust.[/dim]"
        )


def _quote(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _run(step: Step) -> None:
    result = _run_command(step.argv, description=step.description)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise OnboardError(f"{step.description} failed: {stderr or 'command failed'}")


def _write_credential(
    target: Target, build: Callable[[tuple[Credential, ...]], Optional[Step]]
) -> None:
    """Serial credential write with bounded backoff on a busy identity.

    The Managed Identity RP rejects concurrent writes on one identity, so a
    conflict waits and rebuilds the step from a fresh roster, which may show
    nothing left to do.
    """

    for attempt, delay in enumerate((*CONFLICT_BACKOFF_SECONDS, None)):
        step = build(read_roster(target))
        if step is None:
            return
        result = _run_command(step.argv, description=step.description)
        if result.returncode == 0:
            return
        stderr = (result.stderr or result.stdout or "").strip()
        if delay is None or not _is_conflict(stderr):
            raise OnboardError(f"{step.description} failed: {stderr or 'command failed'}")
        console.print(
            f"  [warning]{step.description}: identity busy, retrying in {delay}s "
            f"(retry {attempt + 1}/{len(CONFLICT_BACKOFF_SECONDS)})[/warning]"
        )
        time.sleep(delay)


def _unprotected(repo: str) -> OnboardError:
    return OnboardError(
        f"{repo} does not protect {DEPLOY_ENVIRONMENT} as an environment open to every "
        "branch; trust stays disabled until it does."
    )


def _is_conflict(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in ("conflict", "409", "concurrent"))


def project_roster(
    target: Target, description: str = "Project the trusted-repository roster"
) -> dict[str, str]:
    """Write the identity's roster onto the lock, leaving data and pins alone.

    The roster is read inside the mutator, so a retry after a concurrent lock
    write projects the current roster rather than a stale snapshot.
    """

    written: dict[str, str] = {}

    def compute(lock: dict | None) -> dict:
        nonlocal written
        if lock is None:
            raise PinError("osdu-image-lock is missing; nothing to project the roster into.")
        written = roster_repos(read_roster(target))
        annotations = dict((lock.get("metadata") or {}).get("annotations") or {})
        annotations[TRUSTED_REPOS_ANNOTATION] = json.dumps(written, sort_keys=True)
        return {"data": dict(lock.get("data") or {}), "metadata": {"annotations": annotations}}

    try:
        mutate_lock(compute, description)
    except PinError as exc:
        raise OnboardError(str(exc)) from exc
    return written


def apply_plan(plan: Plan) -> list[Row]:
    """Run the phases in order and return the re-observed rows.

    A failing phase raises with the completed and pending phases named;
    nothing is rolled back, and a re-run repairs from observed state.
    """

    if plan.blocked:
        raise _unprotected(plan.repo)
    if not plan.steps:
        return plan.rows

    phases = [p for p in PHASES if p != "repository" or not plan.skip_repo]
    completed: list[str] = []

    def phase(name: str, work: Callable[[], None]) -> None:
        try:
            work()
        except (OnboardError, PinError) as exc:
            pending = ", ".join(phases[phases.index(name) :])
            raise OnboardError(
                f"{exc}\nCompleted: {', '.join(completed) or 'nothing'}. Pending: {pending}. "
                "Re-run to resume from observed state."
            ) from None
        completed.append(name)

    def repository() -> None:
        for step in plan.steps:
            if step.phase == "repository":
                _run(step)

    def azure() -> None:
        service, repo = plan.service, plan.repo
        if not plan.remove and not read_protection(repo).satisfied:
            raise _unprotected(repo)
        for target, _ in _identity_rosters(plan):
            if plan.remove:
                _write_credential(target, lambda roster, t=target: revoke_step(t, service, roster))
            else:
                _write_credential(
                    target,
                    lambda roster, t=target: credential_step(
                        t, service, repo, plan.subject, roster
                    ),
                )

    def cluster() -> None:
        if any(step.phase == "cluster" for step in plan.steps):
            project_roster(plan.target)

    if "repository" in phases:
        phase("repository", repository)
    phase("azure", azure)
    phase("cluster", cluster)

    stamped = any(step.argv[:3] == ["gh", "secret", "set"] for step in plan.steps)
    plan.state = observe(plan.target, plan.repo, plan.org, values=not plan.skip_repo)
    plan.steps = []
    plan.rows = plan_rows(plan, stamped=stamped)
    return plan.rows


# ---------------------------------------------------------------------------
# Listing and bootstrap
# ---------------------------------------------------------------------------


def list_trust(target: Target) -> list[Row]:
    """Trust per service, with the lock projection compared alongside."""

    require_target(target)
    roster = read_roster(target)
    trusted = roster_repos(roster)
    projection = read_projection()
    rows = []
    for service in sorted(set(trusted) | set(projection)):
        repo, projected = trusted.get(service), projection.get(service)
        if repo is None:
            rows.append(
                Row("cluster", service, "drifted", f"projected {projected} but not trusted")
            )
        elif projected != repo:
            rows.append(
                Row("azure", service, "drifted", f"{repo}; projection {projected or 'missing'}")
            )
        else:
            rows.append(Row("azure", service, "correct", repo))
    rows.extend(
        Row(
            "azure",
            cred.name,
            "unverified",
            f"not a {DEPLOY_ENVIRONMENT} credential this CLI wrote; {cred.subject}",
        )
        for cred in roster
        if cred.service not in trusted
    )
    if target.no_access_identity_name:
        try:
            no_access = roster_repos(read_no_access_roster(target))
        except OnboardError as exc:
            rows.append(Row("azure", target.no_access_identity_name, "missing", str(exc)))
            return rows
        for service, repo in sorted(trusted.items()):
            item = f"{credential_name(service)} on {target.no_access_identity_name}"
            mirrored = no_access.get(service)
            if mirrored == repo:
                rows.append(Row("azure", item, "correct", repo))
            elif mirrored is None:
                rows.append(Row("azure", item, "missing", f"{target.identity_name} trusts {repo}"))
            else:
                rows.append(Row("azure", item, "drifted", f"trusts {mirrored}, not {repo}"))
        for service, repo in sorted(no_access.items()):
            if service not in trusted:
                item = f"{credential_name(service)} on {target.no_access_identity_name}"
                rows.append(
                    Row("azure", item, "drifted", f"trusts {repo}; {target.identity_name} does not")
                )
    return rows


def sync_projection_from_identity(identity_name: str, resource_group: str) -> dict[str, str]:
    """Rebuild the lock's roster projection from the identity, for ``spi up``.

    Credentials outlive the cluster, the lock does not; a rebuilt
    cluster must carry the roster before any fork job pins against it.
    """

    target = Target("", REQUIRED_PROFILE, identity_name, resource_group, values={})
    desired = roster_repos(read_roster(target))
    if read_projection() != desired:
        desired = project_roster(target, "Project the trusted-repository roster after bootstrap")
    return desired
