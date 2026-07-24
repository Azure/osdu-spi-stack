# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Tests for subscription-aware system pool availability zone resolution."""

import json
import subprocess
from unittest import mock

import pytest

from spi import azure_infra
from spi.config import AksMode, Config


def _sku(zones, restricted_zones=None):
    sku = {
        "name": azure_infra.SYSTEM_POOL_VM_SIZE,
        "locationInfo": [{"zones": zones}],
        "restrictions": [],
    }
    if restricted_zones:
        sku["restrictions"].append({"type": "Zone", "restrictionInfo": {"zones": restricted_zones}})
    return sku


def _result(payload) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["az"],
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )


def test_zones_exclude_subscription_restrictions_for_base():
    cfg = Config.from_env("dev1", aks_mode=AksMode.BASE)
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result([_sku(["1", "2", "3"], ["2"])]),
    ):
        assert azure_infra._resolve_system_pool_zones(cfg) == ["1", "3"]


def test_automatic_rejects_a_reduced_zone_set():
    cfg = Config.from_env("dev1", aks_mode=AksMode.AUTOMATIC)
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result([_sku(["1", "2", "3"], ["2"])]),
    ):
        with pytest.raises(RuntimeError, match="requires every availability zone"):
            azure_infra._resolve_system_pool_zones(cfg)


def test_automatic_accepts_a_complete_zone_set():
    cfg = Config.from_env("dev1", aks_mode=AksMode.AUTOMATIC)
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result([_sku(["1", "2", "3"])]),
    ):
        assert azure_infra._resolve_system_pool_zones(cfg) == ["1", "2", "3"]


def test_missing_size_in_region_is_reported():
    cfg = Config.from_env("dev1", aks_mode=AksMode.AUTOMATIC)
    with mock.patch.object(azure_infra, "run_command", return_value=_result([])):
        with pytest.raises(RuntimeError, match="is not offered in"):
            azure_infra._resolve_system_pool_zones(cfg)


def test_fully_restricted_size_is_reported():
    cfg = Config.from_env("dev1", aks_mode=AksMode.BASE)
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result([_sku(["1", "2"], ["1", "2"])]),
    ):
        with pytest.raises(RuntimeError, match="no usable availability zone"):
            azure_infra._resolve_system_pool_zones(cfg)
