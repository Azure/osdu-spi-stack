# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Machine-readable environment information contract."""

from pathlib import Path

from spi import info
from spi.templates import LEGAL_TAG_BASE, spi_init_values_configmap


def _wire(monkeypatch, osdu_config=None, partitions=None, legal_tag_base=None):
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
    monkeypatch.setattr(info, "_read_partitions_list", lambda: partitions or ["opendes"])
    monkeypatch.setattr(info, "_read_legal_tag_base", lambda: legal_tag_base or LEGAL_TAG_BASE)
    monkeypatch.setattr("spi.guard.get_suspend_status", lambda: True)


def test_collect_info_includes_versioned_identity_fields(monkeypatch):
    _wire(monkeypatch)

    result = info.collect_info()

    assert result["apiVersion"] == "spi.osdu.dev/v1"
    assert result["base_url"] == "https://shared.example.test"
    assert result["azure"]["tenant_id"] == "tenant-id"
    assert result["azure"]["data_plane_application_id"] == "application-id"
    assert result["suspended"] is True


def test_openid_issuer_is_published_explicitly(monkeypatch):
    _wire(monkeypatch)

    result = info.collect_info()

    assert result["azure"]["openid_issuer"] == ("https://login.microsoftonline.com/tenant-id/v2.0")


def test_openid_issuer_empty_until_tenant_known(monkeypatch):
    _wire(monkeypatch, osdu_config={})

    result = info.collect_info()

    assert result["azure"]["openid_issuer"] == ""


def test_partitions_report_their_default_legal_tag(monkeypatch):
    _wire(monkeypatch, partitions=["opendes", "second"])

    result = info.collect_info()

    assert [p["legal_tag"] for p in result["partitions"]] == [
        f"opendes-{LEGAL_TAG_BASE}",
        f"second-{LEGAL_TAG_BASE}",
    ]
    assert result["partitions"][0]["primary"] is True


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
    assert info._read_legal_tag_base() == LEGAL_TAG_BASE

    monkeypatch.setattr(
        info,
        "kubectl_json",
        lambda args: {"data": {"values.yaml": "partitions:\n  - opendes\nlegalTag: custom-tag\n"}},
    )
    assert info._read_legal_tag_base() == "custom-tag"


def test_legal_tag_base_falls_back_for_older_configmaps(monkeypatch):
    """A spi-init-values written before the legalTag key existed means the
    Jobs used the chart default; the fallback constant must match it."""
    monkeypatch.setattr(
        info,
        "kubectl_json",
        lambda args: {"data": {"values.yaml": "partitions:\n  - opendes\n"}},
    )
    assert info._read_legal_tag_base() == LEGAL_TAG_BASE

    monkeypatch.setattr(info, "kubectl_json", lambda args: None)
    assert info._read_legal_tag_base() == LEGAL_TAG_BASE


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
