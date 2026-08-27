# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Typed schema for the live backing-environment declaration.

`ops/environments/<name>.yaml` is the reviewed, git-tracked pin that names
the exact stack release, profile, and Azure placement a lifecycle workflow
(`env-upgrade`, `env-refresh`) must deploy. This module owns parsing and
strictly validating that file. Workflows read it through
`scripts/export_environment.py`, never by shell-evaluating YAML directly.

The declaration is intentionally flat: one file, seven keys, no nesting.
Anything richer belongs in the CLI's own `Config`, not in the reviewed pin.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
import yaml.constructor
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import ErrorDetails

from .config import IngressMode, Profile

# The one declaration this repository manages today. A future onboarding
# phase may add more named environments; nothing here assumes there is only
# ever one file.
DEFAULT_DECLARATION_PATH = Path("ops/environments/shared.yaml")

_ENV_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
_LOCATION_RE = re.compile(r"^[a-z][a-z0-9]*$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_SUFFIX_RE = re.compile(r"^[a-z0-9]{5}$")


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


class EnvironmentDeclaration(BaseModel):
    """The flat, reviewed contract lifecycle workflows deploy from.

    Field names are `snake_case` for Python use; the YAML on disk (and the
    values re-exported to `GITHUB_OUTPUT`) use the camelCase aliases below.
    `extra="forbid"` enforces the strict key set: an unexpected key is a
    validation error, not a silently ignored field. Population is by alias
    only (no `populate_by_name`): the on-disk schema is camelCase, exactly,
    and accepting the snake_case field name too would let a declaration
    written as `stack_version:` validate here yet be missed by the
    `^stackVersion:` bump automation in `.github/workflows/release.yml`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    env: str
    stack_version: str = Field(alias="stackVersion")
    profile: Profile
    location: str
    ingress_mode: IngressMode = Field(alias="ingressMode")
    image_branch: str = Field(alias="imageBranch")
    name_suffix: str = Field(alias="nameSuffix")

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


# Field name -> on-disk alias, for fields where they differ. Lets a rejected
# snake_case key (e.g. from populate_by_name muscle memory) be pointed at the
# camelCase key the schema actually accepts, instead of a bare "extra field".
_ALIAS_BY_FIELD_NAME: dict[str, str] = {
    name: field.alias
    for name, field in EnvironmentDeclaration.model_fields.items()
    if field.alias and field.alias != name
}


def _describe_error(error: ErrorDetails) -> str:
    loc = ".".join(str(part) for part in error["loc"])
    if error["type"] == "extra_forbidden" and loc in _ALIAS_BY_FIELD_NAME:
        alias = _ALIAS_BY_FIELD_NAME[loc]
        return f"{loc}: unexpected key; the on-disk schema uses {alias!r}, not {loc!r}"
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
        return EnvironmentDeclaration.model_validate(data)
    except ValidationError as exc:
        messages = "; ".join(_describe_error(error) for error in exc.errors())
        raise EnvironmentDeclarationError(f"schema validation failed: {messages}") from exc


def load_declaration(path: Path | str | None = None) -> EnvironmentDeclaration | None:
    """Load and validate the declaration at `path`, or `None` when absent.

    An absent file is the expected steady state before activation and after
    any future teardown; callers (the lifecycle workflows) must treat `None`
    as a clean skip, never as an error. A present-but-invalid file still
    raises `EnvironmentDeclarationError`.
    """
    resolved = Path(path) if path is not None else DEFAULT_DECLARATION_PATH
    if not resolved.exists():
        return None
    return parse_declaration(resolved.read_text(encoding="utf-8"))
