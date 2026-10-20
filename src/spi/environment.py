# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Typed schema for the live backing-environment declaration.

`ops/environments/<name>.yaml` is the reviewed, git-tracked pin that names
the exact stack release, profile, and Azure placement a lifecycle workflow
(`env-upgrade`, `env-refresh`) must deploy. This module owns parsing and
strictly validating that file. Workflows read it through
`scripts/export_environment.py`, never by shell-evaluating YAML directly.

The declaration is six flat keys plus two optional sections: `forks:`, the
repositories the environment trusts and the mirrors its services follow, and
`resources:`, the sizing `spi up` writes to the `spi-sizing` ConfigMap. A stack
file passed to `spi up -f` carries only `resources:` and always builds on core.

A stack names its declaration by locator, `<owner>/<repo>:<path>`, read from
`main` and retained on the resource group as the `spi-environment-declaration`
tag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Optional

import yaml
import yaml.constructor
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import ErrorDetails

from .config import IngressMode, Profile
from .images import IMAGE_REGISTRY, SCHEMA_LOAD_SERVICE_NAME, ImageResolutionError, github_file
from .sizing import SizingError, overrides

DEFAULT_DECLARATION_PATH = Path("ops/environments/shared.yaml")
DECLARATION_REF = "main"
# Twenty federated credentials per identity, one of them the cluster's.
MAX_FORKS = 19
AZURE_SOURCE = "azure"
# Read as azure: the value environments recorded before community images were dropped.
LEGACY_COMMUNITY_SOURCE = "community"
FORK_SOURCE = "fork"

_ENV_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
_LOCATION_RE = re.compile(r"^[a-z][a-z0-9]*$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_SUFFIX_RE = re.compile(r"^[a-z0-9]{5}$")
_REPO = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/(?!\.\.?$)[A-Za-z0-9_.-]+"
_REPO_RE = re.compile(rf"^{_REPO}$")
_LOCATOR_RE = re.compile(r"^([^:]+):([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.ya?ml)$")

# IngressMode.IP is a debug fallback and not part of the declaration schema.
_DECLARABLE_INGRESS_MODES = {IngressMode.AZURE.value, IngressMode.DNS.value}


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects a mapping with a repeated key, at any depth.

    Plain `yaml.safe_load` keeps the last value for a duplicate mapping key
    with no error, so two `stackVersion:` entries would silently validate
    against whichever one came last: a reviewer approving the PR sees the
    first line while a lifecycle workflow deploys the second.
    """

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        mapping: dict = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


class EnvironmentDeclarationError(ValueError):
    """Raised when a present declaration file fails schema validation.

    A missing file is a clean skip handled by `load_declaration` returning
    `None`; this error is reserved for a file that exists but cannot be
    trusted, which must fail a lifecycle workflow closed rather than run
    with a guessed value.
    """


class ForkDeclaration(BaseModel):
    """One trusted repository and the canonical image source its service follows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str
    repo: str
    canonical_source: Literal["azure", "community", "fork"] = Field(
        AZURE_SOURCE, alias="canonicalSource"
    )

    _legacy_source = field_validator("canonical_source", mode="after")(
        lambda value: AZURE_SOURCE if value == LEGACY_COMMUNITY_SOURCE else value
    )


class EnvironmentDeclaration(BaseModel):
    """The reviewed contract lifecycle workflows deploy from.

    The on-disk schema is camelCase, exactly, and population is by alias only:
    accepting `stack_version:` too would let a declaration validate here yet
    be missed by the `^stackVersion:` bump in `.github/workflows/release.yml`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    env: str
    stack_version: str = Field(alias="stackVersion")
    profile: Profile
    location: str
    ingress_mode: IngressMode = Field(alias="ingressMode")
    # Accepted and ignored: a lifecycle workflow still hands it to a CLI that predates
    # Azure images, and the declaration drops it once its stackVersion does not.
    image_branch: str = Field("", alias="imageBranch")
    name_suffix: str = Field(alias="nameSuffix")
    forks: tuple[ForkDeclaration, ...] = ()
    resources: dict[str, Any] = Field(default_factory=dict)

    _empty_resources = field_validator("resources", mode="before")(
        lambda value: {} if value is None else value
    )

    def sizing(self) -> dict[str, str]:
        """The `spi-sizing` keys `resources:` sets; empty leaves every default."""
        return overrides(self.resources)

    def fork(self, service: str) -> Optional[ForkDeclaration]:
        return next((entry for entry in self.forks if entry.service == service), None)

    def to_github_output(self) -> dict[str, str]:
        """Values safe to append to `$GITHUB_OUTPUT` without shell eval.

        Every field has already passed strict-schema validation, so each
        value is one of a small enum, a `vX.Y.Z` tag, an Azure region, a git
        ref, or a five-character suffix: none can contain a newline or shell
        metacharacter that would let a step's `run:` block misinterpret it.
        """
        return {
            "env": self.env,
            "stack_version": self.stack_version,
            "profile": self.profile.value,
            "location": self.location,
            "ingress_mode": self.ingress_mode.value,
            "image_branch": self.image_branch,
            "name_suffix": self.name_suffix,
            "declares_forks": "true" if self.forks else "false",
        }


def _validate_shape(data: dict) -> None:
    """Raise before pydantic construction so shape errors read consistently."""
    env = data.get("env")
    if isinstance(env, str) and not _ENV_RE.fullmatch(env):
        raise EnvironmentDeclarationError(
            f"env {env!r} must be lowercase alphanumeric, optionally hyphenated, "
            "starting with a letter"
        )

    stack_version = data.get("stackVersion")
    if isinstance(stack_version, str) and not _TAG_RE.fullmatch(stack_version):
        raise EnvironmentDeclarationError(f"stackVersion {stack_version!r} must match vX.Y.Z")

    location = data.get("location")
    if isinstance(location, str) and not _LOCATION_RE.fullmatch(location):
        raise EnvironmentDeclarationError(
            f"location {location!r} must be a lowercase Azure region name (e.g. westus3)"
        )

    image_branch = data.get("imageBranch")
    if isinstance(image_branch, str) and not _BRANCH_RE.fullmatch(image_branch):
        raise EnvironmentDeclarationError(
            f"imageBranch {image_branch!r} must be a valid git branch name"
        )

    name_suffix = data.get("nameSuffix")
    if isinstance(name_suffix, str) and not _SUFFIX_RE.fullmatch(name_suffix):
        raise EnvironmentDeclarationError(
            f"nameSuffix {name_suffix!r} must be exactly five lowercase alphanumeric characters"
        )

    ingress_mode = data.get("ingressMode")
    if isinstance(ingress_mode, str) and ingress_mode not in _DECLARABLE_INGRESS_MODES:
        raise EnvironmentDeclarationError(
            f"ingressMode {ingress_mode!r} must be one of "
            f"{sorted(_DECLARABLE_INGRESS_MODES)}; 'ip' is a debug-only fallback with no "
            "HTTPS listener and cannot be declared here"
        )


def _validate_forks(declaration: EnvironmentDeclaration) -> None:
    forks = declaration.forks
    if forks and declaration.profile is not Profile.CORE:
        raise EnvironmentDeclarationError(
            f"forks requires profile 'core'; profile {declaration.profile.value!r} deploys "
            "no OSDU services for a fork to back"
        )
    if len(forks) > MAX_FORKS:
        raise EnvironmentDeclarationError(
            f"forks lists {len(forks)} repositories; the deploy identity holds at most {MAX_FORKS}"
        )
    services: set[str] = set()
    repos: set[str] = set()
    for entry in forks:
        if entry.service not in IMAGE_REGISTRY or entry.service == SCHEMA_LOAD_SERVICE_NAME:
            known = ", ".join(sorted(n for n in IMAGE_REGISTRY if n != SCHEMA_LOAD_SERVICE_NAME))
            raise EnvironmentDeclarationError(
                f"forks: unknown service {entry.service!r}; known services: {known}"
            )
        if not _REPO_RE.fullmatch(entry.repo):
            raise EnvironmentDeclarationError(f"forks: repo {entry.repo!r} must be <owner>/<name>")
        if entry.service in services:
            raise EnvironmentDeclarationError(f"forks: {entry.service} is listed more than once")
        # GitHub resolves a repository name case-insensitively.
        if entry.repo.lower() in repos:
            raise EnvironmentDeclarationError(
                f"forks: {entry.repo} is listed more than once; one repository backs one service"
            )
        services.add(entry.service)
        repos.add(entry.repo.lower())


def _validate_resources(declaration: EnvironmentDeclaration) -> None:
    if declaration.resources and declaration.profile is not Profile.CORE:
        raise EnvironmentDeclarationError(
            f"resources requires profile 'core'; sizing builds on core, and profile "
            f"{declaration.profile.value!r} is below it"
        )
    # env-upgrade hands a declaration the environment only when it lists forks.
    if declaration.resources and not declaration.forks:
        raise EnvironmentDeclarationError(
            "resources requires forks; a declaration owns the environment, and its sizing, "
            "only once it lists the forks it trusts"
        )
    try:
        declaration.sizing()
    except SizingError as exc:
        raise EnvironmentDeclarationError(str(exc)) from exc


# Lets a rejected snake_case key be pointed at the camelCase key the schema
# accepts, instead of a bare "extra field" error.
_ALIAS_BY_FIELD_NAME: dict[str, str] = {
    name: field.alias
    for model in (EnvironmentDeclaration, ForkDeclaration)
    for name, field in model.model_fields.items()
    if field.alias and field.alias != name
}


def _describe_error(error: ErrorDetails) -> str:
    loc = ".".join(str(part) for part in error["loc"])
    leaf = str(error["loc"][-1]) if error["loc"] else ""
    if error["type"] == "extra_forbidden" and leaf in _ALIAS_BY_FIELD_NAME:
        alias = _ALIAS_BY_FIELD_NAME[leaf]
        return f"{loc}: unexpected key; the on-disk schema uses {alias!r}, not {leaf!r}"
    return f"{loc}: {error['msg']}"


def parse_declaration(raw: str) -> EnvironmentDeclaration:
    """Parse and strictly validate declaration YAML text.

    Raises `EnvironmentDeclarationError` for anything from malformed YAML to
    an extra key to an out-of-shape value. There is no partial-success path:
    a declaration is either a fully trustworthy typed object or an error.
    """
    try:
        data = yaml.load(raw, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise EnvironmentDeclarationError(f"invalid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise EnvironmentDeclarationError("declaration must be a YAML mapping")

    _validate_shape(data)

    try:
        declaration = EnvironmentDeclaration.model_validate(data)
    except ValidationError as exc:
        messages = "; ".join(_describe_error(error) for error in exc.errors())
        raise EnvironmentDeclarationError(f"schema validation failed: {messages}") from exc
    _validate_forks(declaration)
    _validate_resources(declaration)
    return declaration


class StackFile(BaseModel):
    """A local file `spi up -f` deploys on top of core."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resources: dict[str, Any] = Field(default_factory=dict)

    _empty_resources = field_validator("resources", mode="before")(
        lambda value: {} if value is None else value
    )


def load_stack_file(path: Path) -> dict[str, str]:
    """The `spi-sizing` keys a stack file sets, after every check that needs no cluster."""
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except OSError as exc:
        raise EnvironmentDeclarationError(f"cannot read {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise EnvironmentDeclarationError(f"{path}: invalid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise EnvironmentDeclarationError(f"{path}: a stack file must be a YAML mapping")
    if "profile" in data:
        raise EnvironmentDeclarationError(
            f"{path}: a stack file always builds on core and takes no profile"
        )
    try:
        stack = StackFile.model_validate(data)
        return overrides(stack.resources)
    except ValidationError as exc:
        messages = "; ".join(_describe_error(error) for error in exc.errors())
        raise EnvironmentDeclarationError(f"{path}: {messages}") from exc
    except SizingError as exc:
        raise EnvironmentDeclarationError(f"{path}: {exc}") from exc


def _require_stem(declaration: EnvironmentDeclaration, path: PurePosixPath | Path) -> None:
    """A typo in the filename or in `env` must not deploy the wrong environment."""
    if declaration.env != path.stem:
        raise EnvironmentDeclarationError(
            f"env {declaration.env!r} does not match declaration filename "
            f"{path.name!r}; expected {path.stem}.yaml"
        )


def load_declaration(path: Path | str | None = None) -> EnvironmentDeclaration | None:
    """Load and validate the declaration at `path`, or `None` when absent.

    An absent file is a clean skip for the lifecycle workflows, never an
    error. A present but invalid file raises `EnvironmentDeclarationError`,
    including when `env` and the filename stem disagree: a typo in either
    must not deploy the wrong environment.
    """
    resolved = Path(path) if path is not None else DEFAULT_DECLARATION_PATH
    if not resolved.exists():
        return None
    declaration = parse_declaration(resolved.read_text(encoding="utf-8"))
    _require_stem(declaration, resolved)
    return declaration


@dataclass(frozen=True)
class DeclarationLocator:
    """Where a stack's reviewed declaration lives: `<owner>/<repo>:<path>` on `main`."""

    repo: str
    path: str

    def __str__(self) -> str:
        return f"{self.repo}:{self.path}"

    def same_file(self, other: DeclarationLocator) -> bool:
        return self.repo.lower() == other.repo.lower() and self.path == other.path


@dataclass(frozen=True)
class Declared:
    """A declaration together with the locator it was read from."""

    locator: DeclarationLocator
    declaration: EnvironmentDeclaration


def parse_locator(value: str) -> DeclarationLocator:
    match = _LOCATOR_RE.fullmatch(value.strip())
    if (
        not match
        or not _REPO_RE.fullmatch(match.group(1))
        or {".", ".."} & set(match.group(2).split("/"))
    ):
        raise EnvironmentDeclarationError(
            f"declaration locator {value!r} must be <owner>/<repo>:<path>.yaml, for example "
            "Azure/osdu-spi-stack:ops/environments/shared.yaml"
        )
    return DeclarationLocator(match.group(1), match.group(2))


def fetch_declared(locator: DeclarationLocator) -> Declared:
    """Load the reviewed file from the locator's `main`; anything short of valid is an error."""
    try:
        raw = github_file(locator.repo, locator.path, DECLARATION_REF)
    except ImageResolutionError as exc:
        raise EnvironmentDeclarationError(f"could not read {locator}: {exc}") from exc
    try:
        declaration = parse_declaration(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise EnvironmentDeclarationError(f"{locator} is not UTF-8 text: {exc}") from exc
    _require_stem(declaration, PurePosixPath(locator.path))
    return Declared(locator, declaration)
