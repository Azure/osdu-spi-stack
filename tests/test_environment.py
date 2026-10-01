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

from spi import environment
from spi.config import IngressMode, Profile
from spi.environment import (
    MAX_FORKS,
    DeclarationLocator,
    EnvironmentDeclaration,
    EnvironmentDeclarationError,
    fetch_declared,
    load_declaration,
    parse_declaration,
    parse_locator,
)
from spi.images import ImageResolutionError

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
        "declares_forks": "false",
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


def test_rejects_the_hidden_ip_ingress_mode():
    yaml_text = VALID_YAML.replace("ingressMode: azure", "ingressMode: ip")

    with pytest.raises(EnvironmentDeclarationError, match="ingressMode"):
        parse_declaration(yaml_text)


@pytest.mark.parametrize("ingress_mode", ["azure", "dns"])
def test_accepts_declarable_ingress_modes(ingress_mode):
    yaml_text = VALID_YAML.replace("ingressMode: azure", f"ingressMode: {ingress_mode}")

    declaration = parse_declaration(yaml_text)

    assert declaration.ingress_mode.value == ingress_mode


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


def test_rejects_a_duplicated_required_key():
    # yaml.safe_load would silently keep the second value here; a reviewer
    # approving the PR sees the first stackVersion line.
    yaml_text = VALID_YAML + "stackVersion: v0.7.0\n"

    with pytest.raises(EnvironmentDeclarationError, match="stackVersion"):
        parse_declaration(yaml_text)


def test_rejects_a_duplicated_non_required_key():
    yaml_text = VALID_YAML + "location: eastus2\n"

    with pytest.raises(EnvironmentDeclarationError, match="location"):
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


def test_load_declaration_raises_when_env_does_not_match_filename(tmp_path: Path):
    # A typo either side must fail closed, not deploy a second environment.
    path = tmp_path / "shared.yaml"
    path.write_text(VALID_YAML.replace("env: shared", "env: shraed"), encoding="utf-8")

    with pytest.raises(EnvironmentDeclarationError, match="shraed"):
        load_declaration(path)


def test_load_declaration_accepts_a_matching_env_and_filename(tmp_path: Path):
    path = tmp_path / "other.yaml"
    path.write_text(VALID_YAML.replace("env: shared", "env: other"), encoding="utf-8")

    declaration = load_declaration(path)

    assert isinstance(declaration, EnvironmentDeclaration)
    assert declaration.env == "other"


FORKS_YAML = (
    VALID_YAML
    + """\
forks:
  - service: partition
    repo: Acme/osdu-spi-partition
  - service: legal
    repo: Acme/osdu-spi-legal
    canonicalSource: fork
"""
)


def test_forks_default_to_the_community_source():
    declaration = parse_declaration(FORKS_YAML)

    assert [(f.service, f.repo, f.canonical_source) for f in declaration.forks] == [
        ("partition", "Acme/osdu-spi-partition", "community"),
        ("legal", "Acme/osdu-spi-legal", "fork"),
    ]
    assert declaration.to_github_output()["declares_forks"] == "true"


def _with_forks(*entries: str, profile: str = "core") -> str:
    return (
        VALID_YAML.replace("profile: core", f"profile: {profile}") + "forks:\n" + "".join(entries)
    )


def _entry(service: str, repo: str, extra: str = "") -> str:
    return f"  - service: {service}\n    repo: {repo}\n{extra}"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (_with_forks(_entry("partition", "Acme/p"), profile="minimal"), "requires profile 'core'"),
        (
            _with_forks(_entry("partition", "Acme/fork"), _entry("legal", "acme/FORK")),
            "acme/FORK is listed more than once",
        ),
        (
            _with_forks(_entry("partition", "Acme/a"), _entry("partition", "Acme/b")),
            "partition is listed more than once",
        ),
        (_with_forks(_entry("nonesuch", "Acme/p")), "unknown service 'nonesuch'"),
        (_with_forks(_entry("schema-load", "Acme/p")), "unknown service 'schema-load'"),
        (_with_forks(_entry("partition", "not-a-repo")), "must be <owner>/<name>"),
        (
            _with_forks(_entry("partition", "Acme/p", "    canonicalSource: gitlab\n")),
            "forks.0.canonicalSource",
        ),
        (
            _with_forks(_entry("partition", "Acme/p", "    canonical_source: fork\n")),
            "the on-disk schema uses 'canonicalSource'",
        ),
        (
            _with_forks(*(_entry("partition", f"Acme/r{n}") for n in range(MAX_FORKS + 1))),
            "holds at most 19",
        ),
    ],
)
def test_rejects_forks_the_environment_cannot_hold(raw, message):
    with pytest.raises(EnvironmentDeclarationError, match=message):
        parse_declaration(raw)


def test_parses_a_locator_into_repository_and_path():
    locator = parse_locator("Azure/osdu-spi-stack:ops/environments/shared.yaml")

    assert locator == DeclarationLocator("Azure/osdu-spi-stack", "ops/environments/shared.yaml")
    assert str(locator) == "Azure/osdu-spi-stack:ops/environments/shared.yaml"
    assert locator.same_file(DeclarationLocator("azure/OSDU-spi-stack", locator.path))
    assert not locator.same_file(DeclarationLocator(locator.repo, "ops/environments/other.yaml"))


@pytest.mark.parametrize(
    "value",
    [
        "ops/environments/shared.yaml",
        "Azure:shared.yaml",
        "Azure/stack:/etc/shared.yaml",
        "Azure/stack:ops/../shared.yaml",
        "Azure/stack:ops/./shared.yaml",
        "Azure/..:shared.yaml",
        "Azure/stack:shared.json",
        "Azure/stack:shared.yaml?ref=evil",
    ],
)
def test_rejects_a_locator_that_is_not_a_repository_file(value):
    with pytest.raises(EnvironmentDeclarationError, match="must be <owner>/<repo>:<path>"):
        parse_locator(value)


class TestFetchDeclared:
    LOCATOR = DeclarationLocator("Azure/osdu-spi-stack", "ops/environments/shared.yaml")

    def test_reads_the_file_on_main(self, monkeypatch):
        asked = []

        def github_file(repo, path, ref):
            asked.append((repo, path, ref))
            return FORKS_YAML.encode()

        monkeypatch.setattr(environment, "github_file", github_file)

        declared = fetch_declared(self.LOCATOR)

        assert asked == [("Azure/osdu-spi-stack", "ops/environments/shared.yaml", "main")]
        assert declared.locator == self.LOCATOR
        assert [f.canonical_source for f in declared.declaration.forks] == ["community", "fork"]

    def test_refuses_a_file_whose_env_is_not_its_name(self, monkeypatch):
        monkeypatch.setattr(environment, "github_file", lambda *a: FORKS_YAML.encode())

        with pytest.raises(EnvironmentDeclarationError, match="expected dev1.yaml"):
            fetch_declared(DeclarationLocator("Azure/osdu-spi-stack", "ops/dev1.yaml"))

    def test_an_unreadable_file_is_an_error_not_an_absent_declaration(self, monkeypatch):
        def github_file(*args):
            raise ImageResolutionError("GitHub API repos/x: HTTP 404")

        monkeypatch.setattr(environment, "github_file", github_file)

        with pytest.raises(EnvironmentDeclarationError, match="could not read .*HTTP 404"):
            fetch_declared(self.LOCATOR)
