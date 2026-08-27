# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Declaration schema contract for the shared backing environment.

`spi.environment` is the one place `ops/environments/shared.yaml` is parsed.
These tests protect the strict-key, typed-value guarantees the lifecycle
workflows (`env-upgrade`, `env-refresh`) depend on: a malformed declaration
must fail closed, and every exported value must already be safe to append to
`$GITHUB_OUTPUT` without shell evaluation.
"""

from pathlib import Path

import pytest

from spi.config import IngressMode, Profile
from spi.environment import (
    EnvironmentDeclaration,
    EnvironmentDeclarationError,
    load_declaration,
    parse_declaration,
)

VALID_YAML = """\
env: shared
stackVersion: v0.6.0
profile: core
location: westus3
ingressMode: azure
imageBranch: master
nameSuffix: x7k2q
"""


def test_parses_a_valid_declaration():
    declaration = parse_declaration(VALID_YAML)

    assert declaration.env == "shared"
    assert declaration.stack_version == "v0.6.0"
    assert declaration.profile is Profile.CORE
    assert declaration.location == "westus3"
    assert declaration.ingress_mode is IngressMode.AZURE
    assert declaration.image_branch == "master"
    assert declaration.name_suffix == "x7k2q"


def test_to_github_output_uses_camelcase_source_values_as_plain_strings():
    declaration = parse_declaration(VALID_YAML)

    assert declaration.to_github_output() == {
        "env": "shared",
        "stack_version": "v0.6.0",
        "profile": "core",
        "location": "westus3",
        "ingress_mode": "azure",
        "image_branch": "master",
        "name_suffix": "x7k2q",
    }


def test_declaration_is_frozen():
    declaration = parse_declaration(VALID_YAML)

    with pytest.raises(Exception):
        declaration.env = "other"  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda y: y.replace("stackVersion: v0.6.0", "stackVersion: 0.6.0"),
        lambda y: y.replace("stackVersion: v0.6.0", "stackVersion: v0.6"),
        lambda y: y.replace("stackVersion: v0.6.0", "stackVersion: latest"),
    ],
)
def test_rejects_a_malformed_stack_version_tag(mutation):
    with pytest.raises(EnvironmentDeclarationError, match="stackVersion"):
        parse_declaration(mutation(VALID_YAML))


@pytest.mark.parametrize(
    "bad_suffix",
    ["x7k2", "x7k2qq", "X7K2Q", "x7k-q", "x7k2_"],
)
def test_rejects_a_malformed_name_suffix(bad_suffix):
    yaml_text = VALID_YAML.replace("nameSuffix: x7k2q", f"nameSuffix: {bad_suffix}")

    with pytest.raises(EnvironmentDeclarationError, match="nameSuffix"):
        parse_declaration(yaml_text)


def test_rejects_an_unknown_profile():
    yaml_text = VALID_YAML.replace("profile: core", "profile: enterprise")

    with pytest.raises(EnvironmentDeclarationError):
        parse_declaration(yaml_text)


def test_rejects_an_unknown_ingress_mode():
    yaml_text = VALID_YAML.replace("ingressMode: azure", "ingressMode: nginx")

    with pytest.raises(EnvironmentDeclarationError):
        parse_declaration(yaml_text)


def test_rejects_an_extra_key():
    yaml_text = VALID_YAML + "extraKey: nope\n"

    with pytest.raises(EnvironmentDeclarationError, match="extraKey"):
        parse_declaration(yaml_text)


@pytest.mark.parametrize(
    ("snake_case_key", "camel_case_key", "sample_value"),
    [
        ("stack_version", "stackVersion", "v0.6.0"),
        ("ingress_mode", "ingressMode", "azure"),
        ("image_branch", "imageBranch", "master"),
        ("name_suffix", "nameSuffix", "x7k2q"),
    ],
)
def test_rejects_a_snake_case_key_and_names_the_camelcase_key(
    snake_case_key, camel_case_key, sample_value
):
    # populate_by_name is intentionally off: a snake_case key must be
    # rejected, not silently accepted alongside its camelCase alias.
    yaml_text = VALID_YAML + f"{snake_case_key}: {sample_value}\n"

    with pytest.raises(EnvironmentDeclarationError, match=f"{snake_case_key}.*{camel_case_key}"):
        parse_declaration(yaml_text)


@pytest.mark.parametrize(
    "missing_line",
    [
        "env: shared\n",
        "stackVersion: v0.6.0\n",
        "profile: core\n",
        "location: westus3\n",
        "ingressMode: azure\n",
        "imageBranch: master\n",
        "nameSuffix: x7k2q\n",
    ],
)
def test_rejects_a_missing_required_key(missing_line):
    yaml_text = VALID_YAML.replace(missing_line, "")

    with pytest.raises(EnvironmentDeclarationError):
        parse_declaration(yaml_text)


def test_rejects_non_mapping_yaml():
    with pytest.raises(EnvironmentDeclarationError, match="mapping"):
        parse_declaration("- just\n- a\n- list\n")


def test_rejects_invalid_yaml():
    with pytest.raises(EnvironmentDeclarationError, match="YAML"):
        parse_declaration("env: [unclosed\n")


def test_load_declaration_returns_none_when_file_is_absent(tmp_path: Path):
    assert load_declaration(tmp_path / "does-not-exist.yaml") is None


def test_load_declaration_parses_an_existing_file(tmp_path: Path):
    path = tmp_path / "shared.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")

    declaration = load_declaration(path)

    assert isinstance(declaration, EnvironmentDeclaration)
    assert declaration.env == "shared"


def test_load_declaration_raises_for_an_invalid_existing_file(tmp_path: Path):
    path = tmp_path / "shared.yaml"
    path.write_text(VALID_YAML.replace("nameSuffix: x7k2q", "nameSuffix: BAD"), encoding="utf-8")

    with pytest.raises(EnvironmentDeclarationError):
        load_declaration(path)
