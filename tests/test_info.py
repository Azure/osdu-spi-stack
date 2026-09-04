# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Machine-readable environment information contract."""

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from spi import cli, info
from spi.deploy_record import DeployRecord
from spi.templates import LEGAL_TAG_BASE, spi_init_values_configmap

_RECORD = DeployRecord(
    ref="v0.9.1",
    resolved_commit="b11c6748ca05",
    deployed_at="2026-09-04T14:36:46Z",
    cli_version="0.9.1",
    profile="core",
    maintenance=False,
    env="shared",
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI styling so assertions survive Rich colorizing under CI."""
    return _ANSI.sub("", text)


def _wire(
    monkeypatch,
    osdu_config=None,
    partitions=None,
    legal_tag_base=None,
    seeded=True,
    record=_RECORD,
):
    monkeypatch.setattr(
        info,
        "_read_ingress_config",
        lambda: {"INGRESS_MODE": "azure", "INGRESS_FQDN": "shared.example.test"},
    )
    monkeypatch.setattr(
        info,
        "_read_osdu_config",
        lambda: (
            osdu_config
            if osdu_config is not None
            else {
                "AZURE_TENANT_ID": "tenant-id",
                "AAD_CLIENT_ID": "application-id",
                "KEYVAULT_NAME": "shared-vault",
            }
        ),
    )
    monkeypatch.setattr(
        info,
        "_read_flux_extension_values",
        lambda: {
            "AZURE_RESOURCE_GROUP": "spi-stack-shared",
            "AZURE_REGION": "westus3",
        },
    )
    names = partitions or ["opendes"]
    values_yaml = "partitions:\n" + "".join(f"  - {name}\n" for name in names)
    values_yaml += f"legalTag: {legal_tag_base or LEGAL_TAG_BASE}\n"
    monkeypatch.setattr(info, "_read_init_values_yaml", lambda: values_yaml)
    monkeypatch.setattr(info, "_legal_tag_seeded", lambda partition: seeded)
    monkeypatch.setattr(info, "_read_deploy_record", lambda: record)
    monkeypatch.setattr("spi.guard.get_suspend_status", lambda: True)


def test_collect_info_includes_versioned_identity_fields(monkeypatch):
    _wire(monkeypatch)

    result = info.collect_info()

    assert result["apiVersion"] == "spi.osdu.dev/v1"
    assert result["base_url"] == "https://shared.example.test"
    assert result["azure"]["tenant_id"] == "tenant-id"
    assert result["azure"]["data_plane_application_id"] == "application-id"
    assert result["suspended"] is True


def test_render_info_json_never_reads_or_emits_live_credentials(monkeypatch, capsys):
    _wire(monkeypatch)
    sentinel = "sentinel-secret-value"
    calls = []

    def fake_get_live_credentials():
        calls.append(True)
        return [("PostgreSQL", sentinel, sentinel, "platform/secret#password")]

    monkeypatch.setattr(info, "_get_live_credentials", fake_get_live_credentials)

    info.render_info(show_secrets=True, output_json=True)

    output = capsys.readouterr().out
    assert calls == []
    assert sentinel not in output
    assert "credentials" not in json.loads(output)


def test_info_rejects_show_secrets_with_json():
    result = CliRunner().invoke(cli.app, ["info", "--show-secrets", "--json"])

    assert result.exit_code == 2
    assert "--show-secrets cannot be combined with --json" in _plain(result.output)


def test_openid_issuer_is_published_explicitly(monkeypatch):
    _wire(monkeypatch)

    result = info.collect_info()

    assert result["azure"]["openid_issuer"] == ("https://login.microsoftonline.com/tenant-id/v2.0")


def test_openid_issuer_empty_until_tenant_known(monkeypatch):
    _wire(monkeypatch, osdu_config={})

    result = info.collect_info()

    assert result["azure"]["openid_issuer"] == ""


def test_partitions_report_their_seeded_legal_tag(monkeypatch):
    _wire(monkeypatch, partitions=["opendes", "second"])

    result = info.collect_info()

    assert [p["legal_tag"] for p in result["partitions"]] == [
        f"opendes-{LEGAL_TAG_BASE}",
        f"second-{LEGAL_TAG_BASE}",
    ]
    assert result["partitions"][0]["primary"] is True


def test_legal_tag_empty_until_seeding_is_observed(monkeypatch):
    """Legal seeding is non-gating, so the environment can be ready and
    deployable with no tag. Publishing the derived name anyway would tell an
    acceptance suite to reference a tag that was never created, including on
    environments deployed before legal-init existed."""
    _wire(monkeypatch, partitions=["opendes"], seeded=False)

    partition = info.collect_info()["partitions"][0]

    assert partition["legal_tag"] == ""
    assert partition["legal_tag_desired"] == f"opendes-{LEGAL_TAG_BASE}"


def test_legal_tag_seeded_reads_the_job_outcome(monkeypatch):
    """Only the Job's own success proves the tag exists; a Job that is absent,
    still running, or failed must not read as seeded."""
    seen = []

    def fake_kubectl_json(args):
        seen.append(args)
        return {"status": {"succeeded": 1}}

    monkeypatch.setattr(info, "kubectl_json", fake_kubectl_json)
    assert info._legal_tag_seeded("opendes") is True
    assert seen[0] == ["get", "job", "legal-init-opendes", "-n", "osdu"]

    monkeypatch.setattr(info, "kubectl_json", lambda args: {"status": {"failed": 2}})
    assert info._legal_tag_seeded("opendes") is False

    monkeypatch.setattr(info, "kubectl_json", lambda args: None)
    assert info._legal_tag_seeded("opendes") is False


def test_legal_tag_base_read_from_init_values(monkeypatch):
    """The reported tag comes from the same ConfigMap the init chart consumed,
    not from a CLI-side constant, so a pinned override is reported truthfully."""
    rendered = spi_init_values_configmap(["opendes"])
    values_yaml = rendered.split("values.yaml: |\n", 1)[1]
    values_yaml = "\n".join(line[4:] for line in values_yaml.splitlines())

    def fake_kubectl_json(args):
        assert args[:2] == ["get", "configmap"]
        return {"data": {"values.yaml": values_yaml}}

    monkeypatch.setattr(info, "kubectl_json", fake_kubectl_json)
    assert info._legal_tag_base_from_values_yaml(info._read_init_values_yaml()) == LEGAL_TAG_BASE

    assert (
        info._legal_tag_base_from_values_yaml("partitions:\n  - opendes\nlegalTag: custom-tag\n")
        == "custom-tag"
    )


def test_legal_tag_base_falls_back_for_older_configmaps(monkeypatch):
    """A spi-init-values written before the legalTag key existed means the
    Jobs used the chart default; the fallback constant must match it."""
    assert info._legal_tag_base_from_values_yaml("partitions:\n  - opendes\n") == LEGAL_TAG_BASE

    monkeypatch.setattr(info, "kubectl_json", lambda args: None)
    assert info._read_init_values_yaml() == ""
    assert info._legal_tag_base_from_values_yaml(info._read_init_values_yaml()) == LEGAL_TAG_BASE


def test_cli_constant_matches_chart_values_default():
    """templates.LEGAL_TAG_BASE and the chart's `legalTag` default must agree:
    the fallback path reports what a default-rendered chart actually created."""
    values = (
        Path(__file__).resolve().parent.parent
        / "software"
        / "charts"
        / "osdu-spi-init"
        / "values.yaml"
    ).read_text()
    chart_default = next(
        line.split(":", 1)[1].strip()
        for line in values.splitlines()
        if line.startswith("legalTag:")
    )
    assert chart_default == LEGAL_TAG_BASE


def test_info_json_publishes_the_same_environment_block_as_status(monkeypatch):
    """One builder feeds both commands (ADR-030): a fork job binding facts
    from info and gating on status must be reading the same environment."""
    from spi import status

    _wire(monkeypatch)

    facts = info.collect_info()["environment"]

    assert facts == status.environment_facts(_RECORD)
    assert facts["name"] == "shared"
    assert facts["stackVersion"] == "v0.9.1"
    assert facts["profile"] == "core"


def test_info_fails_when_the_record_is_unreadable(monkeypatch):
    """An empty identity block means no record. A read failure is not that,
    so info exits 1 like status rather than publishing a false fact."""
    from spi.deploy_record import DeployRecordError

    real_read = info._read_deploy_record
    _wire(monkeypatch)
    monkeypatch.setattr(info, "_read_deploy_record", real_read)
    monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-stack-shared")

    def unreadable(required=False):
        raise DeployRecordError("forbidden")

    monkeypatch.setattr(info, "read_deploy_record", unreadable)

    result = CliRunner().invoke(cli.app, ["info", "--json"])

    assert result.exit_code == 1
    assert "forbidden" in result.output
    assert "{" not in result.output


def test_info_human_header_names_environment_and_profile(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-stack-shared")

    result = CliRunner().invoke(cli.app, ["info"])
    output = _plain(result.output)

    assert "Environment:   shared  v0.9.1  profile core" in output
    assert output.index("Environment:") < output.index("Ingress mode:")


def test_info_human_header_marks_a_missing_record(monkeypatch):
    _wire(monkeypatch, record=None)
    monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-stack-shared")

    result = CliRunner().invoke(cli.app, ["info"])

    assert "Environment:   unknown (no deploy record)" in _plain(result.output)
