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

"""`spi up` writes the deploy identity into spi-init-values for the members Job."""

from unittest.mock import patch

import yaml

from spi import deploy
from spi.config import Config
from spi.templates import PARTITIONS_CONFIGMAP, PARTITIONS_HASH_KEY


def _applied_all(infra_outputs: dict, partitions: list[str]) -> list[str]:
    with (
        patch("spi.deploy.display_yaml"),
        patch("spi.deploy.kubectl_apply_yaml") as apply_yaml,
    ):
        deploy._create_spi_init_values(
            Config.from_env("dev1", data_partitions=partitions), infra_outputs
        )
    return [call.args[0] for call in apply_yaml.call_args_list]


def _applied(infra_outputs: dict) -> str:
    return _applied_all(infra_outputs, ["opendes", "second"])[0]


def _partitions_hash(partitions: list[str]) -> str:
    applied = _applied_all({}, partitions)
    configmap = yaml.safe_load(applied[-1])
    assert (configmap["metadata"]["name"], configmap["metadata"]["namespace"]) == (
        PARTITIONS_CONFIGMAP,
        "osdu-flux",
    )
    return configmap["data"][PARTITIONS_HASH_KEY]


def test_deploy_identity_client_id_is_written_as_the_member():
    applied = _applied({"deploy_identity_client_id": "deployer-client-id"})

    assert "    - opendes\n    - second\n" in applied
    assert "entitlementsMembers:\n    - deployer-client-id\n" in applied


def test_no_member_is_written_without_a_deploy_identity():
    applied = _applied({})

    assert "entitlementsMembers" not in applied
    assert "    - opendes\n" in applied


def test_osdu_identity_client_id_is_written_as_the_tenant_service_account():
    applied = _applied({"identity_client_id": "osdu-client-id"})

    assert "tenantServiceAccount: osdu-client-id\n" in applied
    assert "tenantServiceAccount" not in _applied({})


def test_init_values_are_watched_by_the_releases_that_read_them():
    """Without the watch label a new partition waits for the HelmRelease interval."""
    configmap = yaml.safe_load(_applied({"deploy_identity_client_id": "deployer-client-id"}))

    assert (configmap["metadata"]["name"], configmap["metadata"]["namespace"]) == (
        "spi-init-values",
        "osdu-flux",
    )
    assert configmap["metadata"]["labels"]["reconcile.fluxcd.io/watch"] == "Enabled"


def test_the_partition_list_hash_is_written_after_the_init_values():
    """partition-init starts first, and the services roll only on a new partition set."""
    applied = _applied_all({}, ["opendes", "second"])

    assert [yaml.safe_load(doc)["metadata"]["name"] for doc in applied] == [
        "spi-init-values",
        PARTITIONS_CONFIGMAP,
    ]
    assert _partitions_hash(["second", "opendes"]) == _partitions_hash(["opendes", "second"])
    assert _partitions_hash(["opendes", "third"]) != _partitions_hash(["opendes", "second"])


def test_the_partition_list_hash_stays_a_string_once_substituted():
    """Flux substitutes into re-serialized YAML, where a bare hex digest can parse as a number."""
    value = _partitions_hash(["opendes"])

    assert isinstance(yaml.safe_load(f"partitionsHash: {value}")["partitionsHash"], str)


def test_the_partition_list_hash_is_watched_by_the_kustomization_that_reads_it():
    configmap = yaml.safe_load(_applied_all({}, ["opendes"])[-1])

    assert configmap["metadata"]["labels"]["reconcile.fluxcd.io/watch"] == "Enabled"
