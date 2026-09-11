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

from spi import deploy
from spi.config import Config


def _applied(infra_outputs: dict) -> str:
    with (
        patch("spi.deploy.display_yaml"),
        patch("spi.deploy.kubectl_apply_yaml") as apply_yaml,
    ):
        deploy._create_spi_init_values(
            Config.from_env("dev1", data_partitions=["opendes", "second"]), infra_outputs
        )
    return apply_yaml.call_args.args[0]


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
