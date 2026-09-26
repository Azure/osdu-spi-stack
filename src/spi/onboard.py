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
GitHub environment. Separately, the resource group's ``spi-source-<service>``
tag records whether the service's canonical image follows that fork or the
community registry. Onboarding observes the repository, the identity, the
tags, and the image lock once, derives the commands that move each from
observed to desired, prints them, and runs them only with ``--write``.
"""

from __future__ import annotations

import functools
import json
import re
import shlex
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from rich.syntax import Syntax
from rich.table import Table

from .console import console
from .images import (
    IMAGE_REGISTRY,
    SCHEMA_LOAD_SERVICE_NAME,
    SCHEMA_SERVICE_NAME,
    ImageResolutionError,
    github_get,
    resolve_fork_image,
    resolve_fork_loader,
)
from .pins import (
    CANONICAL_SOURCES_ANNOTATION,
    TRUSTED_REPOS_ANNOTATION,
    PinError,
    decode_canonical_sources,
    decode_trusted_repos,
    mutate_lock,
    read_lock,
    untrusted_sources,
)
from .shell import run_command
from .templates import TESTER_NAMESPACE

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
SOURCE_TAG_PREFIX = "spi-source-"
COMMUNITY_SOURCE = "community"
FORK_SOURCE = "fork"
# The projection copies both durable records, so it runs after either is written.
PHASES = ("repository", "azure", "source", "cluster")
# Removal records community before revoking, so no rebuild can promote a revoked fork.
REMOVE_PHASES = ("source", "azure", "cluster")

_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/(?!\.\.?$)[A-Za-z0-9_.-]+$")
# GitHub signs the subject as repo:<owner>/<name> or, by default since the
# immutable-subject change, repo:<owner>@<id>/<name>@<id>; the ids come as a pair.
_SUBJECT_RE = re.compile(
    rf"^repo:(?:([^:/@]+)/([^:/@]+)|([^:/@]+)@\d+/([^:/@]+)@\d+)"
    rf":environment:{DEPLOY_ENVIRONMENT}$"
)
# A custom template onboard can render: ids pin the repository, context pins the environment.
ID_CLAIMS = frozenset({"repository_owner_id", "repository_id"})
_ID_RE = re.compile(r"[0-9]+")
TEMPLATE_CLAIMS = ID_CLAIMS | {"context"}


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
    # The member and no-access identities carry the same federated credentials
    # and no role; fork CI mints its non-admin and 401 callers from them.
    no_access_identity_name: str = ""
    member_identity_name: str = ""

    def no_access(self) -> Target:
        return replace(self, identity_name=self.no_access_identity_name)

    def member(self) -> Target:
        return replace(self, identity_name=self.member_identity_name)

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
        owner, name = (group for group in match.groups() if group)
        repo = f"{owner}/{name}"
        return repo if _REPO_RE.match(repo) else ""

    @property
    def ids(self) -> dict[str, str]:
        """The repository ids of a subject a custom template rendered; empty for other forms."""

        fields = self.subject.split(":")
        claims = dict(zip(fields[::2], fields[1::2]))
        if len(fields) % 2 or len(claims) * 2 != len(fields):
            return {}
        if claims.pop("environment", None) != DEPLOY_ENVIRONMENT:
            return {}
        if "repository_id" not in claims or not set(claims) <= ID_CLAIMS:
            return {}
        return claims if all(_ID_RE.fullmatch(value) for value in claims.values()) else {}

    @property
    def well_formed(self) -> bool:
        """A GitHub credential shaped the way this CLI writes them, whatever the subject form."""

        return (
            self.issuer == GITHUB_ISSUER
            and bool(self.repo or self.ids)
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
    member_roster: tuple[Credential, ...] = ()
    # Service to its spi-source-<service> tag value, community or <owner>/<name>.
    sources: dict[str, str] = field(default_factory=dict)
    source_projection: dict[str, str] = field(default_factory=dict)

    def credential(self, service: str) -> Optional[Credential]:
        return find_credential(self.roster, service)

    def no_access_credential(self, service: str) -> Optional[Credential]:
        return find_credential(self.no_access_roster, service)

    def member_credential(self, service: str) -> Optional[Credential]:
        return find_credential(self.member_roster, service)


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
    # fork, community, or "" to keep the recorded source.
    canonical_source: str = ""
    # The image a promotion resolves to now, shown in the plan.
    promotion: str = ""
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


def source_tag(service: str) -> str:
    return f"{SOURCE_TAG_PREFIX}{service}"


def fork_sources(sources: dict[str, str]) -> dict[str, str]:
    """Service to fork repository for the services that do not follow community."""

    return {service: value for service, value in sources.items() if value != COMMUNITY_SOURCE}


def parse_source_tags(tags: dict) -> dict[str, str]:
    """Service to source from a resource group's tags; an invalid value is an error."""

    sources: dict[str, str] = {}
    for key, value in (tags or {}).items():
        if not str(key).lower().startswith(SOURCE_TAG_PREFIX):
            continue
        service, value = str(key)[len(SOURCE_TAG_PREFIX) :].lower(), str(value)
        if value != COMMUNITY_SOURCE and not _REPO_RE.match(value):
            raise OnboardError(
                f"Resource-group tag {key}={value!r} is neither {COMMUNITY_SOURCE} nor "
                f"<owner>/<name>; correct the tag before changing images."
            )
        sources[service] = value
    return sources


def credential_subject(repo: str, prefix: str = "") -> str:
    """The federated subject for a repository's deploy environment.

    ``prefix`` is what GitHub reports it will sign for the repository
    (``sub_claim_prefix``); without it the classic ``repo:<owner>/<name>``.
    """

    return f"{prefix or f'repo:{repo}'}:environment:{DEPLOY_ENVIRONMENT}"


def claim_subject(keys: list[str], ids: dict[str, str]) -> str:
    """The subject a custom template signs, rendered in the template's key order."""

    return ":".join(
        f"environment:{DEPLOY_ENVIRONMENT}" if key == "context" else f"{key}:{ids[key]}"
        for key in keys
    )


def subject_ids(subject: str) -> dict[str, str]:
    return Credential("", GITHUB_ISSUER, subject, (GITHUB_AUDIENCE,)).ids


def find_credential(roster: tuple[Credential, ...], service: str) -> Optional[Credential]:
    name = credential_name(service)
    return next((cred for cred in roster if cred.name == name), None)


def credential_repo(cred: Credential) -> Optional[str]:
    """The repository a credential trusts; None when its ids cannot be resolved right now.

    An id subject names no repository, so GitHub resolves it. A repository
    whose owner id no longer matches is no longer the one trusted, which is "".
    """

    if cred.repo or not cred.ids:
        return cred.repo
    try:
        name, owner_id = github_repository(cred.ids["repository_id"])
    except OnboardError:
        return None
    expected = cred.ids.get("repository_owner_id")
    return name if expected in (None, owner_id) else ""


@dataclass(frozen=True)
class Resolved:
    """The roster's repository names, and the id credentials GitHub did not name."""

    repos: dict[str, str]
    # Named from the lock's last projection because GitHub could not be read.
    projected: dict[str, Credential]
    # Neither GitHub nor the projection named them; they carry no trust until one does.
    unnamed: tuple[Credential, ...]


def resolve_roster(
    roster: tuple[Credential, ...], known: Optional[dict[str, str]] = None
) -> Resolved:
    """Name the credentials shaped the way this CLI writes them.

    ``known`` is the lock's last projection; it names an id credential only
    while GitHub cannot be read, as under ``spi up`` without ``gh``.
    """

    repos: dict[str, str] = {}
    projected: dict[str, Credential] = {}
    unnamed: list[Credential] = []
    for cred in roster:
        if not (cred.service and cred.well_formed):
            continue
        repo = credential_repo(cred)
        if repo is None:
            repo = (known or {}).get(cred.service, "")
            if repo:
                projected[cred.service] = cred
            else:
                unnamed.append(cred)
        if repo:
            repos[cred.service] = repo
    return Resolved(repos, projected, tuple(unnamed))


def roster_repos(
    roster: tuple[Credential, ...], known: Optional[dict[str, str]] = None
) -> dict[str, str]:
    """Service to repository for the credentials shaped the way this CLI writes them."""

    return resolve_roster(roster, known).repos


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
        member_identity_name=f"{cluster}-member" if cluster else "",
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


def _read_mirror_roster(mirror: Target, label: str) -> tuple[Credential, ...]:
    """A mirror identity's roster; a missing identity names the fix."""

    if not mirror.identity_name:
        return ()
    try:
        return read_roster(mirror)
    except OnboardError as exc:
        if "not found" in str(exc).lower():
            raise OnboardError(
                f"{label} identity {mirror.identity_name} not found; run "
                "'spi up' on a release that provisions it first."
            ) from None
        raise


def read_no_access_roster(target: Target) -> tuple[Credential, ...]:
    return _read_mirror_roster(target.no_access(), "No-access")


def read_member_roster(target: Target) -> tuple[Credential, ...]:
    return _read_mirror_roster(target.member(), "Member")


def resolve_repository(spec: str) -> str:
    """Return the repository's stored casing; Entra matches the subject exactly."""

    if not _REPO_RE.match(spec):
        raise OnboardError(f"--repo must be <owner>/<name>, got {spec!r}.")
    payload = _read_json(["gh", "api", f"repos/{spec}"], f"repository {spec}", missing_ok=True)
    if not isinstance(payload, dict) or not payload.get("full_name"):
        raise OnboardError(f"Repository {spec} was not found on GitHub, or gh cannot read it.")
    return str(payload["full_name"])


@functools.cache
def github_repository(repository_id: str) -> tuple[str, str]:
    """The stored ``full_name`` and owner id GitHub keeps under a repository id."""

    payload = _read_json(
        ["gh", "api", f"repositories/{repository_id}"], f"repository id {repository_id}"
    )
    owner = payload.get("owner") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or not payload.get("full_name") or not isinstance(owner, dict):
        raise OnboardError(f"GitHub returned no repository for id {repository_id}.")
    return str(payload["full_name"]), str(owner.get("id", ""))


def read_repository_ids(repo: str) -> dict[str, str]:
    payload = _read_json(["gh", "api", f"repos/{repo}"], f"repository {repo}")
    owner = payload.get("owner") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or not payload.get("id") or not isinstance(owner, dict):
        raise OnboardError(f"GitHub returned no ids for {repo}.")
    return {"repository_id": str(payload["id"]), "repository_owner_id": str(owner.get("id", ""))}


def read_subject(repo: str) -> str:
    """The subject GitHub signs for the repository's deploy environment.

    Under the default template GitHub reports the prefix it signs. A custom
    template (``use_default`` false) is rendered from its claim keys when
    they are repository ids plus the context; ``sub_claim_prefix`` does not
    describe that form. Any other template is refused because onboard cannot
    tell what it would carry.
    """

    payload = _read_json(
        ["gh", "api", f"repos/{repo}/actions/oidc/customization/sub"],
        f"OIDC subject customization on {repo}",
    )
    if not isinstance(payload, dict):
        raise OnboardError(f"Unexpected OIDC subject customization on {repo}: {payload!r}")
    if payload.get("use_default") is False:
        keys = [str(key) for key in payload.get("include_claim_keys") or []]
        renderable = (
            len(set(keys)) == len(keys)
            and set(keys) <= TEMPLATE_CLAIMS
            and {"repository_id", "context"} <= set(keys)
        )
        if not renderable:
            raise OnboardError(
                f"{repo} customizes its OIDC subject claim with {keys or 'no claim keys'}; "
                f"onboard renders only {', '.join(sorted(TEMPLATE_CLAIMS))} with "
                "repository_id and context present. Reset it with: gh api -X PUT "
                f"repos/{repo}/actions/oidc/customization/sub -F use_default=true"
            )
        return claim_subject(keys, read_repository_ids(repo))
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


def _read_lock_projection(decode: Callable[[dict], dict[str, str]]) -> dict[str, str]:
    lock = read_lock(required=False)
    if lock is None:
        raise OnboardError(
            "The osdu-image-lock ConfigMap is missing; the cluster has not finished its "
            "first spi up, so there is nothing to project the roster into."
        )
    try:
        return decode(lock)
    except PinError as exc:
        # The durable record is authoritative; a corrupt projection is drift to overwrite.
        console.print(f"  [warning]{exc}[/warning]")
        return {}


def read_projection() -> dict[str, str]:
    return _read_lock_projection(decode_trusted_repos)


def read_source_projection() -> dict[str, str]:
    return _read_lock_projection(decode_canonical_sources)


def read_source_tags(
    resource_group: str, subscription: str = "", *, missing_ok: bool = False
) -> dict[str, str]:
    """Service to canonical source from the resource group's ``spi-source-*`` tags.

    ``missing_ok`` reads a group that does not exist yet as no tags, for a
    first provision; any other failure raises, because an unread policy must
    not resolve as community.
    """

    argv = ["az", "group", "show", "--name", resource_group, "--query", "tags", "-o", "json"]
    result = _run_command(
        argv + (["--subscription", subscription] if subscription else []), display=False
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        if missing_ok and "ResourceGroupNotFound" in stderr:
            return {}
        raise OnboardError(f"Could not read source tags on {resource_group}: {stderr}")
    try:
        tags = json.loads((result.stdout or "").strip() or "null")
    except json.JSONDecodeError as exc:
        raise OnboardError(f"Could not parse tags on {resource_group}: {exc}") from exc
    return parse_source_tags(tags if isinstance(tags, dict) else {})


def _id_credential_names(cred: Credential, repo: str) -> bool:
    """Whether an id credential ``gh`` could not name trusts ``repo``, read by name over REST."""

    try:
        payload = github_get(f"repos/{repo}")
    except ImageResolutionError:
        return False
    owner = payload.get("owner") if isinstance(payload, dict) else None
    if not isinstance(owner, dict):
        return False
    expected = cred.ids.get("repository_owner_id")
    return cred.ids.get("repository_id") == str(payload.get("id")) and expected in (
        None,
        str(owner.get("id")),
    )


def read_source_policy(resource_group: str, identity_name: str) -> dict[str, str]:
    """The fork each service follows, for resolution; nothing before the group exists.

    A fork source must be the repository the deploy identity trusts for that
    service, so an edited or stale tag cannot deploy an untrusted image.
    """

    forks = fork_sources(read_source_tags(resource_group, missing_ok=True))
    if not forks:
        return forks
    target = Target("", REQUIRED_PROFILE, identity_name, resource_group, values={})
    resolved = resolve_roster(read_roster(target))
    trusted = dict(resolved.repos)
    unconfirmed = []
    for cred in resolved.unnamed:
        repo = forks.get(cred.service)
        if repo and _id_credential_names(cred, repo):
            trusted[cred.service] = repo
        elif repo:
            unconfirmed.append(cred.service)
    mismatched = untrusted_sources(forks, trusted)
    if mismatched:
        hint = (
            f" GitHub could not confirm the id credentials for {', '.join(unconfirmed)}; "
            "sign in with gh or set GH_TOKEN."
            if unconfirmed
            else ""
        )
        raise OnboardError(
            f"Untrusted canonical source on {resource_group}: {'; '.join(mismatched)}. "
            "Re-onboard the fork, or set --canonical-source community, before deploying." + hint
        )
    return forks


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
        member_roster=read_member_roster(target),
        projection=read_projection(),
        protection=read_protection(repo) if repo else None,
        values=read_values(repo, org) if repo and values else {},
        sources=read_source_tags(
            target.resource_group, target.values.get("AZURE_SUBSCRIPTION_ID", "")
        ),
        source_projection=read_source_projection(),
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


_PROJECTED = {
    TRUSTED_REPOS_ANNOTATION: "trusted-repository roster",
    CANONICAL_SOURCES_ANNOTATION: "canonical-source policy",
}


def projection_step(
    desired: dict[str, str],
    observed: dict[str, str],
    annotation: str = TRUSTED_REPOS_ANNOTATION,
) -> Optional[Step]:
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
            f"{annotation}={json.dumps(desired, sort_keys=True)}",
        ],
        f"Project the {_PROJECTED[annotation]} into the image lock "
        "(--write applies it through the lock's compare-and-retry patch)",
    )


def desired_projection(plan: Plan) -> dict[str, str]:
    desired = roster_repos(plan.state.roster, plan.state.projection)
    if plan.remove:
        desired.pop(plan.service, None)
    else:
        desired[plan.service] = plan.repo
    return desired


def desired_source(plan: Plan) -> str:
    """The tag value the plan records: community or the onboarded repository."""

    if plan.remove or plan.canonical_source == COMMUNITY_SOURCE:
        return COMMUNITY_SOURCE
    if plan.canonical_source == FORK_SOURCE:
        return plan.repo
    recorded = plan.state.sources.get(plan.service, COMMUNITY_SOURCE)
    # refuse() stops a recorded fork other than this repository; this repairs casing only.
    return COMMUNITY_SOURCE if recorded == COMMUNITY_SOURCE else plan.repo


def observed_source(plan: Plan) -> Optional[str]:
    """The recorded tag value; removal reads an absent tag as the community it means."""

    recorded = plan.state.sources.get(plan.service)
    return COMMUNITY_SOURCE if recorded is None and plan.remove else recorded


def desired_source_projection(plan: Plan) -> dict[str, str]:
    return fork_sources({**plan.state.sources, plan.service: desired_source(plan)})


def source_step(plan: Plan) -> Optional[Step]:
    value = desired_source(plan)
    if observed_source(plan) == value:
        return None
    subscription = plan.target.values.get("AZURE_SUBSCRIPTION_ID")
    return Step(
        "source",
        [
            "az",
            "group",
            "update",
            "--name",
            plan.target.resource_group,
            "--set",
            f"tags.{source_tag(plan.service)}={value}",
            "--output",
            "none",
            *(["--subscription", subscription] if subscription else []),
        ],
        f"Record {value} as the canonical source of {plan.service}",
    )


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


def projection_row(
    desired: dict[str, str],
    observed: dict[str, str],
    annotation: str = TRUSTED_REPOS_ANNOTATION,
) -> Row:
    item = f"{annotation} on osdu-image-lock"
    if desired == observed:
        return Row("cluster", item, "correct", json.dumps(desired, sort_keys=True))
    return Row("cluster", item, "drifted" if observed else "missing", json.dumps(observed))


def source_row(plan: Plan) -> Row:
    item = f"{source_tag(plan.service)} on {plan.target.resource_group}"
    desired, observed = desired_source(plan), observed_source(plan)
    if observed is None:
        return Row("source", item, "missing", f"records {desired}")
    if observed != desired:
        return Row("source", item, "drifted", f"is {observed}, records {desired}")
    detail = observed
    if plan.promotion:
        detail += f"; next refresh resolves {plan.promotion}"
    return Row("source", item, "correct", detail)


def plan_rows(plan: Plan, *, stamped: bool = False) -> list[Row]:
    rows: list[Row] = []
    if plan.state.protection is not None:
        rows.append(protection_row(plan.repo, plan.state.protection))
    if plan.state.values:
        rows.extend(
            value_rows(plan.target, plan.org or plan.repo, plan.state.values, stamped=stamped)
        )
    rows.extend(credential_row(plan, target, roster) for target, roster in _identity_rosters(plan))
    rows.append(source_row(plan))
    rows.append(projection_row(desired_projection(plan), plan.state.projection))
    rows.append(
        projection_row(
            desired_source_projection(plan),
            plan.state.source_projection,
            CANONICAL_SOURCES_ANNOTATION,
        )
    )
    return rows


def plan_steps(plan: Plan) -> list[Step]:
    steps: list[Step] = []
    if plan.remove:
        record = source_step(plan)
        steps.extend([record] if record else [])
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
        record = source_step(plan)
        steps.extend([record] if record else [])
    for desired, observed, annotation in (
        (desired_projection(plan), plan.state.projection, TRUSTED_REPOS_ANNOTATION),
        (
            desired_source_projection(plan),
            plan.state.source_projection,
            CANONICAL_SOURCES_ANNOTATION,
        ),
    ):
        projection = projection_step(desired, observed, annotation)
        steps.extend([projection] if projection else [])
    return steps


def _identity_rosters(plan: Plan) -> list[tuple[Target, tuple[Credential, ...]]]:
    """The deployer first, then the member and no-access identities the target names."""

    pairs = [(plan.target, plan.state.roster)]
    if plan.target.member_identity_name:
        pairs.append((plan.target.member(), plan.state.member_roster))
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
    planned = subject_ids(plan.subject).get("repository_id")
    for cred in plan.state.roster:
        if cred.name == mine:
            continue
        # Ids settle it without GitHub; a template reordering its keys changes the subject only.
        same_id = planned is not None and cred.ids.get("repository_id") == planned
        same_repo = (credential_repo(cred) or "").lower() == plan.repo.lower()
        if cred.subject == plan.subject or same_id or same_repo:
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
    recorded = plan.state.sources.get(plan.service, COMMUNITY_SOURCE)
    if (
        not plan.canonical_source
        and recorded != COMMUNITY_SOURCE
        and recorded.lower() != plan.repo.lower()
    ):
        raise OnboardError(
            f"{plan.service} follows {recorded}, not {plan.repo}; pass --canonical-source "
            f"{FORK_SOURCE} to follow {plan.repo} or --canonical-source {COMMUNITY_SOURCE}."
        )


def check_promotion(service: str, repo: str) -> str:
    """The image a promotion to ``repo`` would resolve now; refuse what cannot resolve."""

    try:
        image, commit = resolve_fork_image(service, repo)
        if service == SCHEMA_SERVICE_NAME and resolve_fork_loader(image.repository, commit) is None:
            raise OnboardError(
                f"schema cannot follow {repo}: it published no loader "
                f"{image.repository}-load:{image.tag} beside the service image. Onboard "
                "without --canonical-source to trust the fork while schema stays on community."
            )
    except ImageResolutionError as exc:
        raise OnboardError(f"{service} cannot follow {repo}: {exc}") from None
    return f"{image.repository}:{image.tag}"


def require_known_service(service: str) -> None:
    if service not in IMAGE_REGISTRY or service == SCHEMA_LOAD_SERVICE_NAME:
        known = ", ".join(sorted(n for n in IMAGE_REGISTRY if n != SCHEMA_LOAD_SERVICE_NAME))
        raise OnboardError(f"Unknown service {service!r}. Known services: {known}")


def plan_onboard(
    target: Target,
    service: str,
    repo_spec: str = "",
    *,
    org: str = "",
    skip_repo: bool = False,
    canonical_source: str = "",
) -> Plan:
    require_target(target)
    require_known_service(service)
    roster = read_roster(target)
    if not repo_spec:
        existing = find_credential(roster, service)
        repo_spec = (credential_repo(existing) or "") if existing else ""
        if not repo_spec:
            raise OnboardError(f"{service} is not trusted yet; pass --repo <owner>/<name>.")
    repo = resolve_repository(repo_spec)
    plan = Plan(
        target,
        service,
        repo,
        subject=read_subject(repo),
        org=org,
        skip_repo=skip_repo,
        canonical_source=canonical_source,
    )
    plan.state = observe(target, repo, org, values=not skip_repo, roster=roster)
    refuse(plan)
    desired = desired_source(plan)
    recorded = plan.state.sources.get(service, COMMUNITY_SOURCE)
    # Only a change of source is a promotion; an existing one is proved by each refresh.
    if desired != COMMUNITY_SOURCE and desired.lower() != recorded.lower():
        plan.promotion = check_promotion(service, repo)
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
    "repository": "repository (gh)",
    "azure": "trust (az)",
    "source": "canonical source (az)",
    "cluster": "cluster projection (kubectl)",
}


def phases(plan: Plan) -> tuple[str, ...]:
    """The phases this plan runs, in order."""

    if plan.remove:
        return REMOVE_PHASES
    return PHASES[1:] if plan.skip_repo else PHASES


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
    for number, phase in enumerate(phases(plan), start=1):
        steps = [s for s in plan.steps if s.phase == phase]
        if not steps:
            continue
        console.print(f"\n[bold]Phase {number}: {_PHASE_TITLES[phase]}[/bold]")
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
    target: Target,
    description: str = "Project the trusted-repository roster",
    named: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Write the identity's roster onto the lock, leaving data and pins alone.

    The roster is read inside the mutator, so a retry after a concurrent lock
    write projects the current roster rather than a stale snapshot. ``named``
    carries names planning already resolved, ahead of the lock's older ones.
    """

    written: dict[str, str] = {}

    def compute(lock: dict | None) -> dict:
        nonlocal written
        if lock is None:
            raise PinError("osdu-image-lock is missing; nothing to project the roster into.")
        try:
            known = decode_trusted_repos(lock)
        except PinError:
            known = {}
        written = roster_repos(read_roster(target), {**known, **(named or {})})
        annotations = dict((lock.get("metadata") or {}).get("annotations") or {})
        annotations[TRUSTED_REPOS_ANNOTATION] = json.dumps(written, sort_keys=True)
        return {"data": dict(lock.get("data") or {}), "metadata": {"annotations": annotations}}

    try:
        mutate_lock(compute, description)
    except PinError as exc:
        raise OnboardError(str(exc)) from exc
    return written


def project_sources(
    target: Target, description: str = "Project the canonical-source policy"
) -> dict[str, str]:
    """Write the resource group's source tags onto the lock, leaving data and pins alone.

    A resolved image changes only on the next refresh, which reads this projection.
    """

    written: dict[str, str] = {}

    def compute(lock: dict | None) -> dict:
        nonlocal written
        if lock is None:
            raise PinError("osdu-image-lock is missing; nothing to project the source policy into.")
        written = fork_sources(
            read_source_tags(target.resource_group, target.values.get("AZURE_SUBSCRIPTION_ID", ""))
        )
        annotations = dict((lock.get("metadata") or {}).get("annotations") or {})
        annotations[CANONICAL_SOURCES_ANNOTATION] = json.dumps(written, sort_keys=True)
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

    order = phases(plan)
    completed: list[str] = []

    def phase(name: str, work: Callable[[], None]) -> None:
        try:
            work()
        except (OnboardError, PinError) as exc:
            pending = ", ".join(order[order.index(name) :])
            raise OnboardError(
                f"{exc}\nCompleted: {', '.join(completed) or 'nothing'}. Pending: {pending}. "
                "Re-run to resume from observed state."
            ) from None
        completed.append(name)

    def run_phase(name: str) -> Callable[[], None]:
        def work() -> None:
            for step in plan.steps:
                if step.phase == name:
                    _run(step)

        return work

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
        if desired_projection(plan) != plan.state.projection:
            named = {} if plan.remove else {plan.service: plan.repo}
            project_roster(plan.target, named=named)
        if desired_source_projection(plan) != plan.state.source_projection:
            project_sources(plan.target)

    work = {
        "repository": run_phase("repository"),
        "azure": azure,
        "source": run_phase("source"),
        "cluster": cluster,
    }
    for name in order:
        phase(name, work[name])

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
    projection = read_projection()
    resolved = resolve_roster(roster, projection)
    trusted = resolved.repos
    rows = []
    for service in sorted(set(trusted) | set(projection)):
        repo, projected = trusted.get(service), projection.get(service)
        if repo is None:
            rows.append(
                Row("cluster", service, "drifted", f"projected {projected} but not trusted")
            )
        elif service in resolved.projected:
            rows.append(
                Row("azure", service, "unverified", f"{repo} per the projection; GitHub unreadable")
            )
        elif projected != repo:
            rows.append(
                Row("azure", service, "drifted", f"{repo}; projection {projected or 'missing'}")
            )
        else:
            rows.append(Row("azure", service, "correct", repo))
    for cred in roster:
        if cred.service in trusted:
            continue
        if cred.subject.startswith(f"system:serviceaccount:{TESTER_NAMESPACE}:"):
            rows.append(Row("azure", cred.name, "correct", f"cluster issuer; {cred.subject}"))
        elif cred in resolved.unnamed:
            rows.append(
                Row("azure", cred.name, "unverified", f"GitHub did not name {cred.subject}")
            )
        elif cred.service and cred.well_formed:
            rows.append(
                Row("azure", cred.name, "drifted", f"owner id no longer matches; {cred.subject}")
            )
        else:
            rows.append(
                Row(
                    "azure",
                    cred.name,
                    "unverified",
                    f"not a {DEPLOY_ENVIRONMENT} credential this CLI wrote; {cred.subject}",
                )
            )
    mirrors = [
        (target.member_identity_name, read_member_roster),
        (target.no_access_identity_name, read_no_access_roster),
    ]
    # A mirror carries the deployer's subject verbatim, so the comparison needs no GitHub.
    deployers = {c.service: c for c in roster if c.service and c.well_formed}
    for mirror_name, read_mirror in mirrors:
        if not mirror_name:
            continue
        try:
            mirror_roster = read_mirror(target)
        except OnboardError as exc:
            rows.append(Row("azure", mirror_name, "missing", str(exc)))
            continue
        for service, deployer in sorted(deployers.items()):
            item = f"{credential_name(service)} on {mirror_name}"
            repo = trusted.get(service) or deployer.subject
            mirrored = find_credential(mirror_roster, service)
            if mirrored is None:
                rows.append(Row("azure", item, "missing", f"{target.identity_name} trusts {repo}"))
            elif mirrored.trusts(deployer.subject):
                rows.append(Row("azure", item, "correct", repo))
            else:
                rows.append(Row("azure", item, "drifted", f"trusts {_name(mirrored)}, not {repo}"))
        for mirrored in mirror_roster:
            if mirrored.service and mirrored.well_formed and mirrored.service not in deployers:
                item = f"{credential_name(mirrored.service)} on {mirror_name}"
                rows.append(
                    Row(
                        "azure",
                        item,
                        "drifted",
                        f"trusts {_name(mirrored)}; {target.identity_name} does not",
                    )
                )
    rows.extend(
        source_rows(
            trusted,
            read_source_tags(target.resource_group, target.values.get("AZURE_SUBSCRIPTION_ID", "")),
            read_source_projection(),
        )
    )
    return rows


def source_rows(
    trusted: dict[str, str], sources: dict[str, str], projection: dict[str, str]
) -> list[Row]:
    """Canonical source per trusted or tagged service, checked against trust and the lock."""

    rows = []
    for service in sorted(set(trusted) | set(sources) | set(projection)):
        item = source_tag(service)
        value = sources.get(service, COMMUNITY_SOURCE)
        projected = projection.get(service, COMMUNITY_SOURCE)
        if value != COMMUNITY_SOURCE and value.lower() != trusted.get(service, "").lower():
            rows.append(
                Row("source", item, "drifted", f"follows {value}, which is not trusted for it")
            )
        elif value != projected:
            rows.append(Row("source", item, "drifted", f"{value}; projection {projected}"))
        elif service not in sources:
            rows.append(Row("source", item, "missing", f"{COMMUNITY_SOURCE} by default"))
        else:
            rows.append(Row("source", item, "correct", value))
    return rows


def _name(cred: Credential) -> str:
    return cred.repo or cred.subject


def sync_projection_from_identity(identity_name: str, resource_group: str) -> dict[str, str]:
    """Rebuild the lock's roster projection from the identity, for ``spi up``.

    Credentials outlive the cluster, the lock does not; a rebuilt
    cluster must carry the roster before any fork job pins against it.
    """

    target = Target("", REQUIRED_PROFILE, identity_name, resource_group, values={})
    observed = read_projection()
    resolved = resolve_roster(read_roster(target), observed)
    for cred in resolved.unnamed:
        console.print(
            f"  [warning]{cred.name} trusts {cred.subject} but GitHub could not name the "
            "repository, so it is not projected; run 'spi up' again with gh signed in, or "
            "'spi onboard <service> --repo <owner>/<name> --write'.[/warning]"
        )
    desired = resolved.repos
    if observed != desired:
        desired = project_roster(target, "Project the trusted-repository roster after bootstrap")
    return desired


def sync_sources_from_tags(resource_group: str) -> dict[str, str]:
    """Rebuild the lock's source projection from the resource-group tags, for ``spi up``."""

    target = Target("", REQUIRED_PROFILE, "", resource_group, values={})
    desired = fork_sources(read_source_tags(resource_group))
    if read_source_projection() != desired:
        desired = project_sources(target, "Project the canonical-source policy after bootstrap")
    return desired
