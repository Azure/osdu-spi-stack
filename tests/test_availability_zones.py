# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for subscription-resolved system pool availability zones (ADR-027)."""

import json
import subprocess
from unittest import mock

import pytest

from spi import azure_infra
from spi.config import Config


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


def test_complete_zone_set_is_passed_through():
    cfg = Config.from_env("dev1")
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result([_sku(["1", "2", "3"])]),
    ):
        assert azure_infra._resolve_system_pool_zones(cfg) == ["1", "2", "3"]


def test_restricted_zone_is_a_preflight_error():
    cfg = Config.from_env("dev1")
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result([_sku(["1", "2", "3"], ["2"])]),
    ):
        with pytest.raises(RuntimeError, match="requires every availability zone"):
            azure_infra._resolve_system_pool_zones(cfg)


def test_missing_size_in_region_is_reported():
    cfg = Config.from_env("dev1")
    with mock.patch.object(azure_infra, "run_command", return_value=_result([])):
        with pytest.raises(RuntimeError, match="is not offered in"):
            azure_infra._resolve_system_pool_zones(cfg)


def test_fully_restricted_size_is_reported():
    cfg = Config.from_env("dev1")
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result([_sku(["1", "2"], ["1", "2"])]),
    ):
        with pytest.raises(RuntimeError, match="no usable availability zone"):
            azure_infra._resolve_system_pool_zones(cfg)


def test_other_sizes_in_the_catalogue_are_ignored():
    cfg = Config.from_env("dev1")
    other = {
        "name": "Standard_D2s_v5",
        "locationInfo": [{"zones": ["1"]}],
        "restrictions": [{"type": "Zone", "restrictionInfo": {"zones": ["1"]}}],
    }
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result([other, _sku(["1", "2", "3"])]),
    ):
        assert azure_infra._resolve_system_pool_zones(cfg) == ["1", "2", "3"]


def test_failed_sku_query_falls_back_to_template_default():
    cfg = Config.from_env("dev1")
    failed = subprocess.CompletedProcess(args=["az"], returncode=1, stdout="", stderr="throttled")
    with mock.patch.object(azure_infra, "run_command", return_value=failed):
        assert azure_infra._resolve_system_pool_zones(cfg) is None


def test_create_aks_automatic_passes_resolved_zones_to_bicep():
    cfg = Config.from_env("dev1")
    with (
        mock.patch.object(azure_infra, "_resolve_system_pool_zones", return_value=["1", "2", "3"]),
        mock.patch.object(azure_infra, "run_bicep_deployment", return_value={}) as run_bicep,
    ):
        azure_infra.create_aks_automatic(cfg, "deployer-id", "ServicePrincipal", dry_run=True)

    parameters = run_bicep.call_args.kwargs["parameters"]
    assert parameters["availabilityZones"] == ["1", "2", "3"]
    assert parameters["systemPoolVmSize"] == azure_infra.SYSTEM_POOL_VM_SIZE


def test_create_aks_automatic_omits_zones_when_resolver_falls_back():
    cfg = Config.from_env("dev1")
    with (
        mock.patch.object(azure_infra, "_resolve_system_pool_zones", return_value=None),
        mock.patch.object(azure_infra, "run_bicep_deployment", return_value={}) as run_bicep,
    ):
        azure_infra.create_aks_automatic(cfg, "deployer-id", "ServicePrincipal", dry_run=True)

    parameters = run_bicep.call_args.kwargs["parameters"]
    assert "availabilityZones" not in parameters
    assert parameters["systemPoolVmSize"] == azure_infra.SYSTEM_POOL_VM_SIZE
