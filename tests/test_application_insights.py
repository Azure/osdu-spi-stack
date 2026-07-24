# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Tests for optional Application Insights deployment behavior."""

import hashlib
import json
import subprocess
from unittest import mock

import pytest

from spi import azure_infra, cli, deploy
from spi.config import RG_APPLICATION_INSIGHTS_TAG, Config
from spi.templates import osdu_config_configmap


def _result(
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["az"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_config_defaults_application_insights_off():
    assert Config.from_env("dev1").application_insights is False


def test_resolve_application_insights_defaults_off_for_new_environment():
    with (
        mock.patch.object(azure_infra, "read_rg_application_insights_tag", return_value=None),
        mock.patch.object(azure_infra, "detect_existing_application_insights") as detect,
        mock.patch.object(azure_infra, "write_rg_application_insights_tag") as write,
        mock.patch.object(cli, "run_command", return_value=_result("false")),
    ):
        enabled = cli._resolve_application_insights("dev1", requested=None, for_up=True)

    assert enabled is False
    detect.assert_not_called()
    write.assert_not_called()


def test_resolve_application_insights_honors_explicit_new_environment_enable():
    with (
        mock.patch.object(azure_infra, "read_rg_application_insights_tag", return_value=None),
        mock.patch.object(azure_infra, "detect_existing_application_insights") as detect,
        mock.patch.object(azure_infra, "write_rg_application_insights_tag") as write,
        mock.patch.object(cli, "run_command", return_value=_result("false")),
    ):
        enabled = cli._resolve_application_insights("dev1", requested=True, for_up=True)

    assert enabled is True
    detect.assert_not_called()
    write.assert_not_called()


def test_resolve_application_insights_preserves_persisted_mode():
    with mock.patch.object(
        azure_infra,
        "read_rg_application_insights_tag",
        return_value=True,
    ):
        enabled = cli._resolve_application_insights("dev1", requested=None, for_up=True)

    assert enabled is True


def test_resolve_application_insights_preserves_base_environment_mode():
    with mock.patch.object(
        azure_infra,
        "read_rg_application_insights_tag",
        return_value=True,
    ) as read:
        enabled = cli._resolve_application_insights("", requested=None, for_up=True)

    assert enabled is True
    read.assert_called_once_with("spi-stack")


def test_resolve_application_insights_rejects_existing_mode_change():
    with mock.patch.object(
        azure_infra,
        "read_rg_application_insights_tag",
        return_value=True,
    ):
        with pytest.raises(RuntimeError, match="cannot be changed in place"):
            cli._resolve_application_insights("dev1", requested=False, for_up=True)


def test_resolve_application_insights_infers_and_tags_legacy_environment():
    with (
        mock.patch.object(azure_infra, "read_rg_application_insights_tag", return_value=None),
        mock.patch.object(azure_infra, "resource_group_has_resources", return_value=True),
        mock.patch.object(
            azure_infra,
            "detect_existing_application_insights",
            return_value=True,
        ),
        mock.patch.object(azure_infra, "write_rg_application_insights_tag") as write,
        mock.patch.object(cli, "run_command", return_value=_result("true")),
    ):
        enabled = cli._resolve_application_insights("dev1", requested=None, for_up=True)

    assert enabled is True
    write.assert_called_once_with("spi-stack-dev1", True)


def test_resolve_application_insights_allows_choice_after_empty_dry_run():
    with (
        mock.patch.object(azure_infra, "read_rg_application_insights_tag", return_value=None),
        mock.patch.object(azure_infra, "resource_group_has_resources", return_value=False),
        mock.patch.object(azure_infra, "detect_existing_application_insights") as detect,
        mock.patch.object(azure_infra, "write_rg_application_insights_tag") as write,
        mock.patch.object(cli, "run_command", return_value=_result("true")),
    ):
        enabled = cli._resolve_application_insights("dev1", requested=True, for_up=True)

    assert enabled is True
    write.assert_called_once_with("spi-stack-dev1", True)
    detect.assert_not_called()


def test_resolve_application_insights_rejects_change_for_populated_legacy_environment():
    with (
        mock.patch.object(azure_infra, "read_rg_application_insights_tag", return_value=None),
        mock.patch.object(
            azure_infra,
            "detect_existing_application_insights",
            return_value=False,
        ),
        mock.patch.object(azure_infra, "detect_existing_log_analytics", return_value=False),
        mock.patch.object(
            azure_infra,
            "read_deployed_application_insights_mode",
            return_value=None,
        ),
        mock.patch.object(azure_infra, "resource_group_has_resources", return_value=True),
        mock.patch.object(azure_infra, "write_rg_application_insights_tag") as write,
        mock.patch.object(cli, "run_command", return_value=_result("true")),
    ):
        with pytest.raises(RuntimeError, match="cannot be changed in place"):
            cli._resolve_application_insights("dev1", requested=True, for_up=True)

    write.assert_not_called()


def test_resolve_application_insights_recovers_partial_enabled_deployment():
    with (
        mock.patch.object(azure_infra, "read_rg_application_insights_tag", return_value=None),
        mock.patch.object(azure_infra, "resource_group_has_resources", return_value=True),
        mock.patch.object(
            azure_infra,
            "detect_existing_application_insights",
            return_value=False,
        ),
        mock.patch.object(azure_infra, "detect_existing_log_analytics", return_value=False),
        mock.patch.object(
            azure_infra,
            "read_deployed_application_insights_mode",
            return_value=True,
        ),
        mock.patch.object(azure_infra, "write_rg_application_insights_tag") as write,
        mock.patch.object(cli, "run_command", return_value=_result("true")),
    ):
        enabled = cli._resolve_application_insights("dev1", requested=None, for_up=True)

    assert enabled is True
    write.assert_called_once_with("spi-stack-dev1", True)


def test_resolve_application_insights_surfaces_resource_group_probe_errors():
    with (
        mock.patch.object(azure_infra, "read_rg_application_insights_tag", return_value=None),
        mock.patch.object(azure_infra, "detect_existing_application_insights") as detect,
        mock.patch.object(cli, "run_command", return_value=_result(returncode=1, stderr="timeout")),
    ):
        with pytest.raises(RuntimeError, match="Unable to determine"):
            cli._resolve_application_insights("dev1", requested=None, for_up=True)

    detect.assert_not_called()


def test_create_resource_group_persists_observability_tag():
    cfg = Config.from_env("dev1", name_suffix="abc12", application_insights=True)
    with mock.patch.object(
        azure_infra,
        "run_command",
        side_effect=[_result("false"), _result("{}")],
    ) as run:
        azure_infra.create_resource_group(cfg)

    create_command = run.call_args_list[1].args[0]
    assert "--tags" in create_command
    assert f"{RG_APPLICATION_INSIGHTS_TAG}=true" in create_command
    assert "spi-name-suffix=abc12" in create_command


def test_dry_run_resource_group_does_not_persist_observability_tag():
    cfg = Config.from_env("dev1", name_suffix="abc12", application_insights=True)
    with mock.patch.object(
        azure_infra,
        "run_command",
        side_effect=[_result("false"), _result("{}")],
    ) as run:
        azure_infra.create_resource_group(cfg, persist_application_insights=False)

    create_command = run.call_args_list[1].args[0]
    assert f"{RG_APPLICATION_INSIGHTS_TAG}=true" not in create_command
    assert "spi-name-suffix=abc12" in create_command


def test_detect_existing_application_insights_distinguishes_not_found():
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result(
            returncode=3,
            stderr="(ResourceNotFound) The Resource was not found.",
        ),
    ):
        assert azure_infra.detect_existing_application_insights("spi-stack-dev1", "dev1") is False


def test_resource_group_has_resources_treats_null_tsv_as_empty():
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result(stdout="None"),
    ):
        assert azure_infra.resource_group_has_resources("spi-stack-dev1") is False


def test_read_application_insights_tag_distinguishes_missing_resource_group():
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result(
            returncode=3,
            stderr="(ResourceGroupNotFound) Resource group was not found.",
        ),
    ):
        assert azure_infra.read_rg_application_insights_tag("spi-stack-dev1") is None


def test_read_application_insights_tag_surfaces_probe_errors():
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result(returncode=1, stderr="TooManyRequests"),
    ):
        with pytest.raises(RuntimeError, match="Unable to read"):
            azure_infra.read_rg_application_insights_tag("spi-stack-dev1")


def test_detect_existing_application_insights_surfaces_probe_errors():
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result(returncode=1, stderr="AuthorizationFailed"),
    ):
        with pytest.raises(RuntimeError, match="Unable to determine"):
            azure_infra.detect_existing_application_insights("spi-stack-dev1", "dev1")


def test_read_deployed_application_insights_mode_supports_legacy_parameters():
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result(
            stdout=json.dumps(
                {
                    "appInsightsName": {
                        "value": "osdu-dev1-insights",
                    }
                }
            )
        ),
    ):
        assert (
            azure_infra.read_deployed_application_insights_mode(
                "spi-stack-dev1",
                "dev1",
            )
            is True
        )


def test_read_deployed_application_insights_mode_supports_explicit_flag():
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result(
            stdout=json.dumps(
                {
                    "enableApplicationInsights": {
                        "value": False,
                    }
                }
            )
        ),
    ):
        assert (
            azure_infra.read_deployed_application_insights_mode(
                "spi-stack-dev1",
                "dev1",
            )
            is False
        )


def test_read_deployed_application_insights_mode_handles_missing_deployment():
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result(
            returncode=3,
            stderr="(DeploymentNotFound) Deployment was not found.",
        ),
    ):
        assert (
            azure_infra.read_deployed_application_insights_mode(
                "spi-stack-dev1",
                "dev1",
            )
            is None
        )


def test_read_deployed_application_insights_mode_surfaces_probe_errors():
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result(returncode=1, stderr="AuthorizationFailed"),
    ):
        with pytest.raises(RuntimeError, match="Unable to read deployment"):
            azure_infra.read_deployed_application_insights_mode(
                "spi-stack-dev1",
                "dev1",
            )


def test_bicep_params_disable_application_insights_by_default():
    cfg = Config.from_env("dev1")
    with mock.patch.object(
        azure_infra,
        "_resolve_deployer_principal",
        return_value=("oid", "User"),
    ):
        params = azure_infra._build_bicep_params(cfg, "https://oidc")

    assert params["enableApplicationInsights"] is False
    assert params["appInsightsName"] == ""
    assert params["logAnalyticsName"] == ""


def test_bicep_params_enable_application_insights():
    cfg = Config.from_env("dev1", application_insights=True)
    with mock.patch.object(
        azure_infra,
        "_resolve_deployer_principal",
        return_value=("oid", "User"),
    ):
        params = azure_infra._build_bicep_params(cfg, "https://oidc")

    assert params["enableApplicationInsights"] is True
    assert params["appInsightsName"] == "osdu-dev1-insights"
    assert params["logAnalyticsName"] == "osdu-dev1-logs"


def test_osdu_config_uses_consistent_dummy_application_insights_values():
    yaml = osdu_config_configmap(
        domain="example.test",
        primary_partition="opendes",
        tenant_id="tenant",
        identity_client_id="identity",
        aad_client_id="audience",
        keyvault_uri="https://kv.vault.azure.net/",
        keyvault_name="kv",
        primary_cosmosdb_endpoint="https://cosmos/",
        primary_storage_account_name="storage",
        primary_servicebus_namespace="bus",
    )

    dummy = "00000000-0000-0000-0000-000000000000"
    assert f'APPINSIGHTS_KEY: "{dummy}"' in yaml
    assert f'APPINSIGHTS_INSTRUMENTATIONKEY: "{dummy}"' in yaml
    assert "IngestionEndpoint=https://localhost/" in yaml
    assert '"percentage":0' in yaml
    assert '"liveMetrics":{"enabled":false}' in yaml
    assert '"profiler":{"enabled":false}' in yaml
    assert '"diskPersistenceMaxSizeMb":0' in yaml
    assert '"disabledAll":true' in yaml
    assert '"preAggregatedStandardMetrics":{"enabled":false}' in yaml


def test_osdu_config_omits_disabled_agent_config_with_real_application_insights():
    yaml = osdu_config_configmap(
        domain="example.test",
        primary_partition="opendes",
        tenant_id="tenant",
        identity_client_id="identity",
        aad_client_id="audience",
        keyvault_uri="https://kv.vault.azure.net/",
        keyvault_name="kv",
        primary_cosmosdb_endpoint="https://cosmos/",
        primary_storage_account_name="storage",
        primary_servicebus_namespace="bus",
        appinsights_key="real-key",
        app_insights_connection_string="InstrumentationKey=real-key",
    )

    assert "APPLICATIONINSIGHTS_CONFIGURATION_CONTENT" not in yaml


def test_changed_osdu_config_restarts_existing_deployments():
    yaml_content = "kind: ConfigMap\ndata:\n  VALUE: changed\n"
    with mock.patch.object(
        deploy,
        "run_command",
        side_effect=[
            _result(stdout=json.dumps({"metadata": {"annotations": {}}})),
            _result(stdout="deployment.apps/storage"),
            _result(stdout="deployment.apps/storage restarted"),
            _result(stdout="configmap/osdu-config annotated"),
        ],
    ) as run:
        deploy._restart_osdu_deployments_if_config_changed(yaml_content)

    assert run.call_args_list[2].args[0] == [
        "kubectl",
        "rollout",
        "restart",
        "deployment",
        "--namespace",
        "osdu",
    ]
    assert run.call_args_list[3].args[0][:4] == [
        "kubectl",
        "annotate",
        "configmap",
        "osdu-config",
    ]


def test_unchanged_osdu_config_does_not_restart_deployments():
    yaml_content = "kind: ConfigMap\ndata:\n  VALUE: unchanged\n"
    config_hash = hashlib.sha256(yaml_content.encode("utf-8")).hexdigest()
    with mock.patch.object(
        deploy,
        "run_command",
        return_value=_result(
            stdout=json.dumps(
                {
                    "metadata": {
                        "annotations": {
                            deploy.OSDU_CONFIG_ROLLOUT_ANNOTATION: config_hash,
                        }
                    }
                }
            )
        ),
    ) as run:
        deploy._restart_osdu_deployments_if_config_changed(yaml_content)

    run.assert_called_once()


def test_failed_osdu_config_restart_remains_pending_for_retry():
    yaml_content = "kind: ConfigMap\ndata:\n  VALUE: retry\n"
    no_rollout_state = _result(stdout=json.dumps({"metadata": {"annotations": {}}}))
    with mock.patch.object(
        deploy,
        "run_command",
        side_effect=[
            no_rollout_state,
            _result(stdout="deployment.apps/storage"),
            RuntimeError("restart failed"),
        ],
    ) as run:
        with pytest.raises(RuntimeError, match="restart failed"):
            deploy._restart_osdu_deployments_if_config_changed(yaml_content)

    assert all(call.args[0][1] != "annotate" for call in run.call_args_list)
