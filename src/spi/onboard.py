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
GitHub environment. This module plans and applies that activation in three
phases: repository protection and the five values on GitHub, the credential
on the identity, and the roster projection fork CI reads from the image
lock. Without ``--write`` nothing changes; the plan prints the exact
commands so an operator without the CLI on one side can run them by hand.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from rich.syntax import Syntax
from rich.table import Table

from .console import console
from .images import IMAGE_REGISTRY, SCHEMA_LOAD_SERVICE_NAME
from .pins import (
    TRUSTED_REPOS_ANNOTATION,
    PinError,
    decode_trusted_repos,
    mutate_lock,
    read_lock,
)
from .shell import run_command

GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_AUDIENCE = "api://AzureADTokenExchange"
DEPLOY_ENVIRONMENT = "spi-stack"
REQUIRED_BRANCHES = ("main", "fork_integration")
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
_CONFLICT_MARKERS = ("conflict", "409", "concurrent", "retryable")

_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/(?!\.\.?$)[A-Za-z0-9_.-]+$")
_SUBJECT_RE = re.compile(rf"^repo:([^:]+/[^:]+):environment:{DEPLOY_ENVIRONMENT}$")


class OnboardError(RuntimeError):
    """Onboarding refused or stopped; the message names what to change."""


@dataclass(frozen=True)
class Target:
    """The connected environment as ``spi info --json`` publishes it."""

    env: str
    profile: str
    identity_name: str
    resource_group: str
    values: dict[str, str]

    @property
    def client_id(self) -> str:
        return self.values[CLIENT_ID_SECRET]


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
        return match.group(1) if match else ""

    def matches(self, repo: str) -> bool:
        return (
            self.issuer == GITHUB_ISSUER
            and self.subject == credential_subject(repo)
            and set(self.audiences) == {GITHUB_AUDIENCE}
        )


@dataclass(frozen=True)
class Protection:
    """What GitHub reports about the repository's ``spi-stack`` environment."""

    exists: bool
    custom_branch_policies: bool
    branches: tuple[str, ...]
    # Policies beyond the required branches, as (id, name); a tag policy or a
    # wildcard here admits runs ADR-032 keeps out, so trust waits until they go.
    extra_policies: tuple[tuple[int, str], ...] = ()

    @property
    def missing_branches(self) -> tuple[str, ...]:
        return tuple(name for name in REQUIRED_BRANCHES if name not in self.branches)

    @property
    def satisfied(self) -> bool:
        return (
            self.exists
            and self.custom_branch_policies
            and not self.missing_branches
            and not self.extra_policies
        )


@dataclass(frozen=True)
class Row:
    phase: str
    item: str
    state: str
    detail: str = ""


@dataclass
class Step:
    """One command the plan would run, with the row it repairs."""

    phase: str
    argv: list[str]
    description: str


@dataclass
class Plan:
    target: Target
    service: str
    repo: str
    org: str
    skip_repo: bool
    roster: list[Credential]
    protection: Optional[Protection]
    values: dict[str, Optional[str]]
    projection: dict[str, str]
    steps: list[Step] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)

    @property
    def credential_name(self) -> str:
        return f"{CREDENTIAL_PREFIX}{self.service}"

    def existing_credential(self) -> Optional[Credential]:
        return next((cred for cred in self.roster if cred.name == self.credential_name), None)


def credential_subject(repo: str) -> str:
    return f"repo:{repo}:environment:{DEPLOY_ENVIRONMENT}"


def _quote(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _read_json(argv: list[str], what: str, *, missing_ok: bool = False):
    """Run a read-only command and parse its JSON output.

    ``missing_ok`` turns a GitHub 404 into ``None`` so callers can distinguish
    an absent object from an unreadable one; every other failure raises,
    since an unverified precondition must block rather than pass.
    """

    result = run_command(argv, display=False, check=False)
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
    """Read the connected environment's identity facts through ``spi info``."""

    from .info import collect_info

    info = collect_info()
    identity = info.get("deploy_identity") or {}
    cluster = identity.get("cluster", "")
    values = {
        CLIENT_ID_SECRET: identity.get("client_id", ""),
        "AZURE_TENANT_ID": identity.get("tenant_id", ""),
        "AZURE_SUBSCRIPTION_ID": identity.get("subscription_id", ""),
        "SPI_STACK_RESOURCE_GROUP": identity.get("resource_group", ""),
        "SPI_STACK_CLUSTER": cluster,
    }
    return Target(
        env=(info.get("environment") or {}).get("name", ""),
        profile=(info.get("environment") or {}).get("profile", ""),
        identity_name=f"{cluster}-deployer" if cluster else "",
        resource_group=identity.get("resource_group", ""),
        values=values,
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
            "The connected cluster does not publish the deploy identity values "
            f"({', '.join(missing)}); run 'spi up' on a release that provisions the "
            "deploy identity first."
        )


def read_roster(target: Target) -> list[Credential]:
    payload = _read_json(
        [
            "az",
            "identity",
            "federated-credential",
            "list",
            "--identity-name",
            target.identity_name,
            "--resource-group",
            target.resource_group,
            "-o",
            "json",
        ],
        f"federated credentials on {target.identity_name}",
    )
    roster = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        roster.append(
            Credential(
                name=str(item.get("name", "")),
                issuer=str(item.get("issuer", "")),
                subject=str(item.get("subject", "")),
                audiences=tuple(str(a) for a in item.get("audiences") or ()),
            )
        )
    return sorted(roster, key=lambda cred: cred.name)


def roster_repos(roster: list[Credential]) -> dict[str, str]:
    """Map service to trusted repository for the credentials this CLI owns."""

    return {
        cred.service: cred.repo
        for cred in roster
        if cred.service and cred.repo and cred.matches(cred.repo)
    }


def resolve_repository(spec: str) -> str:
    """Return the repository's stored casing; Entra matches the subject exactly."""

    if not _REPO_RE.match(spec):
        raise OnboardError(f"--repo must be <owner>/<name>, got {spec!r}.")
    payload = _read_json(["gh", "api", f"repos/{spec}"], f"repository {spec}", missing_ok=True)
    if not isinstance(payload, dict) or not payload.get("full_name"):
        raise OnboardError(f"Repository {spec} was not found on GitHub, or gh cannot read it.")
    return str(payload["full_name"])


def read_protection(repo: str) -> Protection:
    env = _read_json(
        ["gh", "api", f"repos/{repo}/environments/{DEPLOY_ENVIRONMENT}"],
        f"environment {DEPLOY_ENVIRONMENT} on {repo}",
        missing_ok=True,
    )
    if not isinstance(env, dict):
        return Protection(exists=False, custom_branch_policies=False, branches=())
    policy = env.get("deployment_branch_policy") or {}
    custom = bool(policy.get("custom_branch_policies"))
    branches: list[str] = []
    extras: list[tuple[int, str]] = []
    if custom:
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
        entries = [
            entry
            for page in (pages if isinstance(pages, list) else [pages])
            if isinstance(page, dict)
            for entry in page.get("branch_policies") or []
        ]
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            name = str(entry["name"])
            kind = str(entry.get("type") or "branch")
            if kind == "branch" and name in REQUIRED_BRANCHES and name not in branches:
                branches.append(name)
            else:
                extras.append((int(entry.get("id") or 0), f"{name} ({kind})"))
    return Protection(
        exists=True,
        custom_branch_policies=custom,
        branches=tuple(branches),
        extra_policies=tuple(extras),
    )


def read_values(repo: str, org: str) -> dict[str, Optional[str]]:
    """Observed values at the level the plan writes to.

    Variables come back with their values; a secret comes back as ``""``
    when present, since GitHub never returns its value, and ``None`` when
    absent. Variables that are absent are ``None`` too.
    """

    scope = ["--org", org] if org else ["--repo", repo]
    fields = "name,value,visibility" if org else "name,value"
    variables = _read_json(
        ["gh", "variable", "list", *scope, "--json", fields],
        f"variables on {org or repo}",
    )
    secrets = _read_json(
        ["gh", "secret", "list", *scope, "--json", "name"], f"secrets on {org or repo}"
    )
    observed: dict[str, Optional[str]] = {
        name: None for name in (CLIENT_ID_SECRET, *VARIABLE_NAMES)
    }
    for entry in variables or []:
        if isinstance(entry, dict) and entry.get("name") in VARIABLE_NAMES:
            value = str(entry.get("value", ""))
            # An organization value the fork cannot read is drift, not a match.
            if org and str(entry.get("visibility", "all")).lower() != "all":
                value = f"{value} (visibility {entry.get('visibility')})"
            observed[str(entry["name"])] = value
    for entry in secrets or []:
        if isinstance(entry, dict) and entry.get("name") == CLIENT_ID_SECRET:
            observed[CLIENT_ID_SECRET] = ""
    return observed


def read_projection() -> dict[str, str]:
    lock = read_lock(required=False)
    if lock is None:
        raise OnboardError(
            "The osdu-image-lock ConfigMap is missing; the cluster has not finished "
            "its first spi up, so there is nothing to project the roster into."
        )
    try:
        return decode_trusted_repos(lock)
    except PinError as exc:
        # The identity is authoritative; a corrupt projection is drift to overwrite.
        console.print(f"  [warning]{exc}[/warning]")
        return {}


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _gh_scope(plan: Plan) -> list[str]:
    return ["--org", plan.org] if plan.org else ["--repo", plan.repo]


def _value_commands(plan: Plan) -> list[Step]:
    steps = []
    visibility = ["--visibility", "all"] if plan.org else []
    where = plan.org or plan.repo
    steps.append(
        Step(
            "repository",
            [
                "gh",
                "secret",
                "set",
                CLIENT_ID_SECRET,
                *_gh_scope(plan),
                *visibility,
                "--body",
                plan.target.client_id,
            ],
            f"Stamp {CLIENT_ID_SECRET} on {where}",
        )
    )
    for name in VARIABLE_NAMES:
        if plan.values.get(name) == plan.target.values[name]:
            continue
        steps.append(
            Step(
                "repository",
                [
                    "gh",
                    "variable",
                    "set",
                    name,
                    *_gh_scope(plan),
                    *visibility,
                    "--body",
                    plan.target.values[name],
                ],
                f"Set {name} on {where}",
            )
        )
    return steps


def _protection_commands(plan: Plan) -> list[Step]:
    protection = plan.protection
    assert protection is not None
    steps = []
    if not protection.exists or not protection.custom_branch_policies:
        steps.append(
            Step(
                "repository",
                [
                    "gh",
                    "api",
                    "--method",
                    "PUT",
                    f"repos/{plan.repo}/environments/{DEPLOY_ENVIRONMENT}",
                    "-F",
                    "deployment_branch_policy[protected_branches]=false",
                    "-F",
                    "deployment_branch_policy[custom_branch_policies]=true",
                ],
                f"Create the protected {DEPLOY_ENVIRONMENT} environment on {plan.repo}",
            )
        )
    for policy_id, label in protection.extra_policies:
        steps.append(
            Step(
                "repository",
                [
                    "gh",
                    "api",
                    "--method",
                    "DELETE",
                    f"repos/{plan.repo}/environments/{DEPLOY_ENVIRONMENT}"
                    f"/deployment-branch-policies/{policy_id}",
                ],
                f"Remove the {label} policy from {DEPLOY_ENVIRONMENT} on {plan.repo}",
            )
        )
    for branch in protection.missing_branches:
        steps.append(
            Step(
                "repository",
                [
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    f"repos/{plan.repo}/environments/{DEPLOY_ENVIRONMENT}/deployment-branch-policies",
                    "-f",
                    f"name={branch}",
                    "-f",
                    "type=branch",
                ],
                f"Admit {branch} to {DEPLOY_ENVIRONMENT} on {plan.repo}",
            )
        )
    return steps


def _credential_command(plan: Plan) -> Optional[Step]:
    existing = plan.existing_credential()
    if existing is not None and existing.matches(plan.repo):
        return None
    verb = "update" if existing is not None else "create"
    return Step(
        "azure",
        [
            "az",
            "identity",
            "federated-credential",
            verb,
            "--name",
            plan.credential_name,
            "--identity-name",
            plan.target.identity_name,
            "--resource-group",
            plan.target.resource_group,
            "--issuer",
            GITHUB_ISSUER,
            "--subject",
            credential_subject(plan.repo),
            "--audiences",
            GITHUB_AUDIENCE,
        ],
        f"Trust {plan.repo} for {plan.service}",
    )


def _projection_step(desired: dict[str, str], observed: dict[str, str]) -> Optional[Step]:
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
        "Project the trusted-repository roster into the image lock",
    )


def _value_rows(plan: Plan, stamped: frozenset[str] = frozenset()) -> list[Row]:
    rows = []
    where = plan.org or plan.repo
    observed = plan.values.get(CLIENT_ID_SECRET)
    if CLIENT_ID_SECRET in stamped:
        rows.append(Row("repository", f"{CLIENT_ID_SECRET} on {where}", "correct", "stamped"))
    elif observed is None:
        rows.append(Row("repository", f"{CLIENT_ID_SECRET} on {where}", "missing"))
    else:
        rows.append(
            Row(
                "repository",
                f"{CLIENT_ID_SECRET} on {where}",
                "unverified",
                "GitHub does not return secret values",
            )
        )
    for name in VARIABLE_NAMES:
        observed = plan.values.get(name)
        if observed is None:
            rows.append(Row("repository", f"{name} on {where}", "missing"))
        elif observed == plan.target.values[name]:
            rows.append(Row("repository", f"{name} on {where}", "correct"))
        else:
            rows.append(Row("repository", f"{name} on {where}", "drifted", f"is {observed!r}"))
    return rows


def _protection_rows(plan: Plan) -> list[Row]:
    protection = plan.protection
    assert protection is not None
    item = f"{DEPLOY_ENVIRONMENT} environment on {plan.repo}"
    if not protection.exists:
        return [Row("repository", item, "missing")]
    if not protection.custom_branch_policies:
        return [Row("repository", item, "drifted", "no custom branch policies")]
    problems = []
    if protection.missing_branches:
        problems.append("missing " + ", ".join(protection.missing_branches))
    if protection.extra_policies:
        problems.append("admits " + ", ".join(label for _id, label in protection.extra_policies))
    if problems:
        return [Row("repository", item, "drifted", "; ".join(problems))]
    return [Row("repository", item, "correct", ", ".join(protection.branches))]


def _credential_row(plan: Plan) -> Row:
    existing = plan.existing_credential()
    item = f"{plan.credential_name} on {plan.target.identity_name}"
    if existing is None:
        return Row("azure", item, "missing")
    if existing.matches(plan.repo):
        return Row("azure", item, "correct", credential_subject(plan.repo))
    return Row("azure", item, "drifted", existing.subject)


def _projection_row(desired: dict[str, str], observed: dict[str, str]) -> Row:
    item = f"{TRUSTED_REPOS_ANNOTATION} on osdu-image-lock"
    if desired == observed:
        return Row("cluster", item, "correct", json.dumps(desired, sort_keys=True))
    return Row("cluster", item, "drifted" if observed else "missing", json.dumps(observed))


def _desired_projection(plan: Plan) -> dict[str, str]:
    desired = roster_repos(plan.roster)
    desired[plan.service] = plan.repo
    return desired


def _refuse_before_writes(plan: Plan) -> None:
    owner = plan.repo.split("/", 1)[0]
    if plan.org and plan.org.lower() != owner.lower():
        raise OnboardError(
            f"--org {plan.org} does not own {plan.repo}; organization values are only "
            f"visible to that organization's repositories."
        )
    for cred in plan.roster:
        if cred.repo.lower() == plan.repo.lower() and cred.name != plan.credential_name:
            raise OnboardError(
                f"{plan.repo} already backs {cred.service or cred.name}; one repository "
                "backs one service. Remove that credential first."
            )
    if plan.existing_credential() is None and len(plan.roster) >= MAX_CREDENTIALS:
        raise OnboardError(
            f"{plan.target.identity_name} already holds {MAX_CREDENTIALS} federated "
            "credentials, the Azure maximum; a larger roster is a new decision."
        )


def _require_known_service(service: str) -> None:
    if service not in IMAGE_REGISTRY or service == SCHEMA_LOAD_SERVICE_NAME:
        known = ", ".join(
            sorted(name for name in IMAGE_REGISTRY if name != SCHEMA_LOAD_SERVICE_NAME)
        )
        raise OnboardError(f"Unknown service {service!r}. Known services: {known}")


def plan_onboard(
    target: Target,
    service: str,
    repo_spec: str,
    *,
    org: str = "",
    skip_repo: bool = False,
) -> Plan:
    """Read every side and build the plan; raises before any phase could write."""

    require_target(target)
    _require_known_service(service)
    if shutil.which("gh") is None:
        raise OnboardError("gh is required to read repository state; install the GitHub CLI.")

    roster = read_roster(target)
    existing = next((c for c in roster if c.name == f"{CREDENTIAL_PREFIX}{service}"), None)
    if repo_spec:
        repo = resolve_repository(repo_spec)
    elif existing is not None and existing.repo:
        repo = existing.repo
    else:
        raise OnboardError(f"{service} is not trusted yet; pass --repo <owner>/<fork>.")

    plan = Plan(
        target=target,
        service=service,
        repo=repo,
        org=org,
        skip_repo=skip_repo,
        roster=roster,
        protection=read_protection(repo),
        values={} if skip_repo else read_values(repo, org),
        projection=read_projection(),
    )
    _refuse_before_writes(plan)

    steps: list[Step] = []
    if not skip_repo:
        steps.extend(_protection_commands(plan))
        steps.extend(_value_commands(plan))
    credential = _credential_command(plan)
    if credential is not None:
        steps.append(credential)
    projection = _projection_step(_desired_projection(plan), plan.projection)
    if projection is not None:
        steps.append(projection)
    plan.steps = steps
    plan.rows = _status_rows(plan)
    return plan


def _status_rows(plan: Plan, stamped: frozenset[str] = frozenset()) -> list[Row]:
    rows = _protection_rows(plan)
    if not plan.skip_repo:
        rows.extend(_value_rows(plan, stamped))
    rows.append(_credential_row(plan))
    rows.append(_projection_row(_desired_projection(plan), plan.projection))
    return rows


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_PHASE_TITLES = {
    "repository": "1. Repository protection and values (GitHub)",
    "azure": "2. Azure trust (deploy identity)",
    "cluster": "3. Cluster trust (image lock projection)",
}

_STATE_STYLES = {
    "correct": "success",
    "drifted": "warning",
    "missing": "warning",
    "unverified": "dim",
}


def render_rows(rows: list[Row], title: str) -> None:
    table = Table(title=title, show_lines=False)
    table.add_column("Phase")
    table.add_column("Item")
    table.add_column("State")
    table.add_column("Detail", overflow="fold")
    for row in rows:
        style = _STATE_STYLES.get(row.state, "white")
        table.add_row(row.phase, row.item, f"[{style}]{row.state}[/{style}]", row.detail)
    console.print(table)


def render_plan(plan: Plan) -> None:
    console.print(
        f"\n[bold]Onboard {plan.service} from {plan.repo} to {plan.target.env or 'the environment'}"
        "[/bold]  (plan; pass --write to apply)"
    )
    render_rows(plan.rows, "Observed state")
    if not plan.steps:
        console.print("[success]Nothing to change; every row is correct or unverified.[/success]")
        return
    for phase, title in _PHASE_TITLES.items():
        steps = [step for step in plan.steps if step.phase == phase]
        if not steps:
            continue
        console.print(f"\n[bold]{title}[/bold]")
        script = "\n".join(f"# {step.description}\n{_quote(step.argv)}" for step in steps)
        console.print(Syntax(script, "bash", theme="monokai", word_wrap=True))
    if plan.skip_repo:
        console.print(
            "\n[dim]--skip-repo: GitHub values are left to the repository's owner; the "
            f"{DEPLOY_ENVIRONMENT} environment rules are still required before trust.[/dim]"
        )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _run_step(step: Step) -> None:
    result = run_command(step.argv, description=step.description, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise OnboardError(f"{step.description} failed: {stderr or 'command failed'}")


def _write_credential(plan: Plan, build: Callable[[Plan], Optional[Step]]) -> None:
    """Issue one credential write, backing off and re-reading the roster on a conflict.

    The Managed Identity RP rejects concurrent writes on one identity; a
    competing onboarding is answered by waiting and rebuilding the step
    from the observed roster, which may show nothing left to do.
    """

    for attempt, delay in enumerate((*CONFLICT_BACKOFF_SECONDS, None)):
        plan.roster = read_roster(plan.target)
        step = build(plan)
        if step is None:
            return
        result = run_command(step.argv, description=step.description, check=False)
        if result.returncode == 0:
            return
        stderr = (result.stderr or result.stdout or "").strip()
        if delay is None or not _is_conflict(stderr):
            raise OnboardError(f"{step.description} failed: {stderr or 'command failed'}")
        console.print(
            f"  [warning]{step.description}: identity busy, retrying in {delay}s "
            f"(attempt {attempt + 1}/{len(CONFLICT_BACKOFF_SECONDS)})[/warning]"
        )
        time.sleep(delay)


def _is_conflict(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _CONFLICT_MARKERS)


def project_roster(
    target: Target, description: str = "Project the trusted-repository roster"
) -> dict[str, str]:
    """Write the identity's roster onto the lock, leaving data and pins alone.

    The roster is re-read on every attempt, so a competing onboarding that
    landed between this call's planning read and its write is projected
    rather than overwritten with a stale snapshot. Returns what was written.
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


def _reconcile_projection(plan: Plan) -> None:
    """Cluster phase: read both sides fresh, project if they differ, read again."""

    plan.roster = read_roster(plan.target)
    plan.projection = read_projection()
    if plan.projection != roster_repos(plan.roster):
        project_roster(plan.target)
        plan.projection = read_projection()
    plan.roster = read_roster(plan.target)


def apply_plan(plan: Plan) -> list[Row]:
    """Apply the phases in order and return the re-read state.

    A phase that fails raises with the completed and pending phases named;
    nothing is rolled back, and a re-run repairs from observed state.
    """

    completed: list[str] = []
    phases = ["repository", "azure", "cluster"] if not plan.skip_repo else ["azure", "cluster"]

    def fail(exc: Exception, phase: str) -> OnboardError:
        pending = phases[phases.index(phase) :]
        return OnboardError(
            f"{exc}\nCompleted: {', '.join(completed) or 'nothing'}. "
            f"Pending: {', '.join(pending)}. Re-run to resume from observed state."
        )

    stamped: set[str] = set()
    if not plan.skip_repo:
        try:
            for step in (s for s in plan.steps if s.phase == "repository"):
                _run_step(step)
                if step.argv[:3] == ["gh", "secret", "set"]:
                    stamped.add(step.argv[3])
        except (OnboardError, PinError) as exc:
            raise fail(exc, "repository") from None
        completed.append("repository")

    try:
        plan.protection = read_protection(plan.repo)
        if not plan.protection.satisfied:
            raise OnboardError(
                f"{plan.repo} does not protect {DEPLOY_ENVIRONMENT} with custom branch "
                f"policies for {', '.join(REQUIRED_BRANCHES)}; trust stays disabled until it does."
            )
        _write_credential(plan, _credential_command)
        plan.roster = read_roster(plan.target)
    except (OnboardError, PinError) as exc:
        raise fail(exc, "azure") from None
    completed.append("azure")

    try:
        _reconcile_projection(plan)
    except (OnboardError, PinError) as exc:
        raise fail(exc, "cluster") from None
    completed.append("cluster")

    if not plan.skip_repo:
        plan.values = read_values(plan.repo, plan.org)
    plan.steps = []
    plan.rows = _status_rows(plan, frozenset(stamped))
    return plan.rows


# ---------------------------------------------------------------------------
# Listing and removal
# ---------------------------------------------------------------------------


def list_trust(target: Target) -> list[Row]:
    """Trust per service, with the lock projection compared alongside."""

    require_target(target)
    roster = read_roster(target)
    trusted = roster_repos(roster)
    projection = read_projection()
    rows = []
    for service in sorted(set(trusted) | set(projection)):
        repo = trusted.get(service)
        projected = projection.get(service)
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
    for cred in roster:
        if cred.service and cred.repo and not cred.matches(cred.repo):
            rows.append(
                Row("azure", cred.name, "unverified", f"issuer or audience drifted; {cred.subject}")
            )
        elif not cred.service or not cred.repo:
            rows.append(
                Row("azure", cred.name, "unverified", f"not a {DEPLOY_ENVIRONMENT} credential")
            )
    return rows


def _delete_command(plan: Plan) -> Optional[Step]:
    existing = plan.existing_credential()
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
            plan.credential_name,
            "--identity-name",
            plan.target.identity_name,
            "--resource-group",
            plan.target.resource_group,
            "--yes",
        ],
        f"Revoke {existing.repo or existing.subject} for {plan.service}",
    )


def plan_remove(target: Target, service: str) -> Plan:
    require_target(target)
    _require_known_service(service)
    roster = read_roster(target)
    plan = Plan(
        target=target,
        service=service,
        repo="",
        org="",
        skip_repo=True,
        roster=roster,
        protection=None,
        values={},
        projection=read_projection(),
    )
    existing = plan.existing_credential()
    steps: list[Step] = []
    delete = _delete_command(plan)
    if delete is not None:
        steps.append(delete)
    desired = {svc: repo for svc, repo in roster_repos(roster).items() if svc != service}
    projection = _projection_step(desired, plan.projection)
    if projection is not None:
        steps.append(projection)
    plan.steps = steps
    plan.rows = [
        Row(
            "azure",
            plan.credential_name,
            "correct" if existing is None else "drifted",
            "absent" if existing is None else f"trusts {existing.repo or existing.subject}",
        ),
        _projection_row(desired, plan.projection),
    ]
    return plan


def apply_remove(plan: Plan) -> list[Row]:
    """Revoke, then reproject; a failure names the phase left pending."""

    try:
        _write_credential(plan, _delete_command)
        plan.roster = read_roster(plan.target)
    except (OnboardError, PinError) as exc:
        raise OnboardError(
            f"{exc}\nCompleted: nothing. Pending: azure, cluster. Re-run to resume."
        ) from None
    try:
        _reconcile_projection(plan)
    except (OnboardError, PinError) as exc:
        raise OnboardError(
            f"{exc}\nCompleted: azure. Pending: cluster. Re-run to resume."
        ) from None
    plan.steps = []
    plan.rows = [
        Row("azure", plan.credential_name, "correct", "absent"),
        _projection_row(roster_repos(plan.roster), plan.projection),
    ]
    return plan.rows


def render_remove_plan(plan: Plan) -> None:
    console.print(
        f"\n[bold]Remove trust for {plan.service} on {plan.target.env or 'the environment'}[/bold]"
        "  (plan; pass --write to apply)"
    )
    render_rows(plan.rows, "Observed state")
    if not plan.steps:
        console.print("[success]Nothing to change.[/success]")
        return
    script = "\n".join(f"# {step.description}\n{_quote(step.argv)}" for step in plan.steps)
    console.print(Syntax(script, "bash", theme="monokai", word_wrap=True))


def sync_projection_from_identity(identity_name: str, resource_group: str) -> dict[str, str]:
    """Rebuild the lock's roster projection from the identity, for ``spi up``.

    Credentials outlive the cluster (ADR-034), the lock does not; a rebuilt
    cluster must carry the roster before any fork job pins against it.
    """

    target = Target(
        env="",
        profile=REQUIRED_PROFILE,
        identity_name=identity_name,
        resource_group=resource_group,
        values={},
    )
    desired = roster_repos(read_roster(target))
    if read_projection() != desired:
        desired = project_roster(target, "Project the trusted-repository roster after bootstrap")
    return desired
