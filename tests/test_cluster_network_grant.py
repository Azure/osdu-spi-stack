# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Tests for repairing the VNet grant node autoprovisioning depends on."""

import subprocess
from unittest import mock

import pytest

from spi import azure_infra
from spi.config import Config


def _result(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["az"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


def test_existing_grant_is_left_alone():
    cfg = Config.from_env("dev1")
    with (
        mock.patch.object(azure_infra, "_subscription_id", return_value="sub"),
        mock.patch.object(azure_infra, "run_command", return_value=_result("1")) as run,
    ):
        azure_infra._ensure_cluster_identity_network_contributor(cfg, {"clusterPrincipalId": "pid"})

    assert run.call_count == 1
    assert run.call_args.args[0][1] == "rest"


def test_missing_grant_is_reapplied():
    cfg = Config.from_env("dev1")
    with (
        mock.patch.object(azure_infra, "_subscription_id", return_value="sub"),
        mock.patch.object(azure_infra, "run_command", side_effect=[_result("0"), _result()]) as run,
    ):
        azure_infra._ensure_cluster_identity_network_contributor(cfg, {"clusterPrincipalId": "pid"})

    create_command = run.call_args_list[1].args[0]
    assert create_command[1:4] == ["role", "assignment", "create"]
    assert "Network Contributor" in create_command
    assert "pid" in create_command
    assert create_command[-3].endswith("/virtualNetworks/spi-stack-dev1-vnet")


def test_failed_lookup_is_treated_as_missing():
    cfg = Config.from_env("dev1")
    with (
        mock.patch.object(azure_infra, "_subscription_id", return_value="sub"),
        mock.patch.object(
            azure_infra,
            "run_command",
            side_effect=[_result("", returncode=1), _result()],
        ) as run,
    ):
        azure_infra._ensure_cluster_identity_network_contributor(cfg, {"clusterPrincipalId": "pid"})

    assert run.call_count == 2


def test_missing_cluster_identity_is_fatal():
    cfg = Config.from_env("dev1")
    with mock.patch.object(azure_infra, "run_command") as run:
        with pytest.raises(RuntimeError, match="control-plane identity"):
            azure_infra._ensure_cluster_identity_network_contributor(cfg, {})

    run.assert_not_called()
