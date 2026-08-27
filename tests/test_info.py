# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Machine-readable environment information contract."""

from spi import info


def test_collect_info_includes_versioned_identity_fields(monkeypatch):
    monkeypatch.setattr(
        info,
        "_read_ingress_config",
        lambda: {"INGRESS_MODE": "azure", "INGRESS_FQDN": "shared.example.test"},
    )
    monkeypatch.setattr(
        info,
        "_read_osdu_config",
        lambda: {
            "AZURE_TENANT_ID": "tenant-id",
            "AAD_CLIENT_ID": "application-id",
            "KEYVAULT_NAME": "shared-vault",
        },
    )
    monkeypatch.setattr(
        info,
        "_read_flux_extension_values",
        lambda: {
            "AZURE_RESOURCE_GROUP": "spi-stack-shared",
            "AZURE_REGION": "westus3",
        },
    )
    monkeypatch.setattr(info, "_read_partitions_list", lambda: ["opendes"])
    monkeypatch.setattr("spi.guard.get_suspend_status", lambda: True)

    result = info.collect_info()

    assert result["apiVersion"] == "spi.osdu.dev/v1"
    assert result["base_url"] == "https://shared.example.test"
    assert result["azure"]["tenant_id"] == "tenant-id"
    assert result["azure"]["data_plane_application_id"] == "application-id"
    assert result["suspended"] is True
