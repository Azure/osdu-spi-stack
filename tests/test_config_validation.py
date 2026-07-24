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

"""Validation tests for Config.data_partitions.

Guards against silent Azure-resource-name collisions when partition names
contain stripped characters (hyphens/underscores) or when env+partition
exceeds the 24-char Storage account limit.
"""

import pytest
from pydantic import ValidationError

from spi.config import Config, Profile
from spi.images import ImageSource


def _validation_message(exc_info: pytest.ExceptionInfo) -> str:
    return str(exc_info.value)


class TestValidPartitions:
    def test_single_partition(self):
        cfg = Config(env="dev1", data_partitions=["opendes"])
        assert cfg.data_partitions == ["opendes"]
        assert cfg.primary_partition == "opendes"

    def test_multiple_partitions(self):
        cfg = Config(env="dev1", data_partitions=["opendes", "tenant1"])
        assert cfg.data_partitions == ["opendes", "tenant1"]
        assert cfg.primary_partition == "opendes"

    def test_default_partitions(self):
        cfg = Config(env="dev1")
        assert cfg.data_partitions == ["opendes"]

    def test_application_insights_can_be_enabled(self):
        cfg = Config(env="dev1", application_insights=True)
        assert cfg.application_insights is True

    def test_ghcr_images_are_the_default_baseline(self):
        cfg = Config(env="dev1")
        assert cfg.image_source == ImageSource.GHCR
        assert cfg.image_org == "yuchen-osdu"
        assert cfg.image_tag == "main-snapshot"
        assert cfg.image_ref == ""

    def test_full_profile_uses_the_implemented_thirteen_service_stack(self):
        cfg = Config(env="dev1", profile=Profile.FULL)
        assert cfg.gitops_profile == "core"

    def test_empty_env_still_valid(self):
        cfg = Config(env="", data_partitions=["p1"])
        assert cfg.data_partitions == ["p1"]

    def test_alphanumeric_mixed(self):
        cfg = Config(env="dev1", data_partitions=["p1", "p2", "tenant42"])
        assert cfg.data_partitions == ["p1", "p2", "tenant42"]

    def test_max_length_partition_with_env(self):
        # "osdu" (4) + env "dev1" (4) + suffix (5) = 13; partition can be up to 11.
        cfg = Config(env="dev1", data_partitions=["abcdefghijk"])
        assert cfg.data_partitions == ["abcdefghijk"]


class TestInvalidPartitionNames:
    @pytest.mark.parametrize(
        "bad_name",
        [
            "OPENDES",  # uppercase
            "Opendes",  # mixed case
            "my-tenant",  # hyphen
            "p_1",  # underscore
            "p1!",  # special char
            "p 1",  # space
            "tenant.1",  # period
            "",  # empty string element
        ],
    )
    def test_rejects_non_alphanumeric(self, bad_name):
        with pytest.raises(ValidationError) as exc_info:
            Config(env="dev1", data_partitions=[bad_name])
        msg = _validation_message(exc_info)
        assert "lowercase alphanumeric" in msg
        assert repr(bad_name) in msg


class TestInvalidLength:
    def test_long_env_plus_partition_exceeds_24(self):
        # "osdu" (4) + "longprodenv2026" (15) + "tenanteast" (10) + suffix (5) = 34
        with pytest.raises(ValidationError) as exc_info:
            Config(env="longprodenv2026", data_partitions=["tenanteast"])
        msg = _validation_message(exc_info)
        assert "24-char Azure limit" in msg
        assert "tenanteast" in msg

    def test_short_partition_with_long_env_fails(self):
        # "osdu" (4) + "productiondev" (13) + "p1" (2) + suffix (5) = 24, OK
        cfg = Config(env="productiondev", data_partitions=["p1"])
        assert cfg.data_partitions == ["p1"]

        # add one more char on partition: 25, fail
        with pytest.raises(ValidationError):
            Config(env="productiondev", data_partitions=["p11"])

    def test_env_dashes_stripped_for_length_check(self):
        # "osdu" (4) + "dev-1" stripped to "dev1" (4) + "tenant" (6) = 14, OK
        cfg = Config(env="dev-1", data_partitions=["tenant"])
        assert cfg.data_partitions == ["tenant"]


class TestDuplicates:
    def test_two_identical(self):
        with pytest.raises(ValidationError) as exc_info:
            Config(env="dev1", data_partitions=["p1", "p1"])
        msg = _validation_message(exc_info)
        assert "duplicate" in msg
        assert "p1" in msg

    def test_three_partitions_one_duplicated(self):
        with pytest.raises(ValidationError) as exc_info:
            Config(env="dev1", data_partitions=["p1", "p2", "p1"])
        msg = _validation_message(exc_info)
        assert "duplicate" in msg


class TestEmpty:
    def test_empty_list_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Config(env="dev1", data_partitions=[])
        msg = _validation_message(exc_info)
        assert "at least one partition" in msg


class TestFromEnv:
    """Config.from_env should pass partition validation through."""

    def test_from_env_default_partition(self):
        cfg = Config.from_env(env="dev1")
        assert cfg.data_partitions == ["opendes"]

    def test_from_env_custom_partitions(self):
        cfg = Config.from_env(env="dev1", data_partitions=["opendes", "tenant1"])
        assert cfg.data_partitions == ["opendes", "tenant1"]

    def test_from_env_invalid_partition(self):
        with pytest.raises(ValidationError):
            Config.from_env(env="dev1", data_partitions=["BAD"])


def test_deployer_principal_type_env_override(monkeypatch):
    """SPI_DEPLOYER_TYPE bypasses the az account lookup entirely."""
    from spi import azure_infra

    monkeypatch.setenv("SPI_DEPLOYER_TYPE", "User")
    assert azure_infra._deployer_principal_type() == "User"

    monkeypatch.setenv("SPI_DEPLOYER_TYPE", "ServicePrincipal")
    assert azure_infra._deployer_principal_type() == "ServicePrincipal"
