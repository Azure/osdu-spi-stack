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

"""Guards the identity projection the Istio EnvoyFilter performs.

Every caller must reach the Spring filters as itself. A branch that rewrites
one audience to a fixed principal would make the deploy identity and the
no-access identity indistinguishable and let any management-audience token
act as the platform.
"""

from spi.templates import istio_auth_resources

UAMI = "11111111-1111-1111-1111-111111111111"
APP = "22222222-2222-2222-2222-222222222222"


def _lua(entra_client_id: str = UAMI, aad_client_id: str = UAMI) -> str:
    yaml = istio_auth_resources(
        namespace="osdu",
        tenant_id="tenant-id",
        entra_client_id=entra_client_id,
        aad_client_id=aad_client_id,
    )
    return yaml.split("inlineCode: |", 1)[1]


def test_lua_never_substitutes_a_fixed_principal():
    lua = _lua()

    assert UAMI not in lua
    assert "management.azure.com" not in lua
    assert 'h:headers():replace("x-app-id"' not in lua


def test_lua_projects_the_callers_own_app_id():
    lua = _lua()

    assert 'payload["appid"] or payload["azp"] or payload["aud"]' in lua
    assert 'h:headers():add("x-app-id", appId)' in lua
    assert 'h:headers():add("x-user-id", payload["appid"])' in lua


def test_audience_list_still_accepts_management_and_both_client_ids():
    yaml = istio_auth_resources(
        namespace="osdu", tenant_id="tenant-id", entra_client_id=UAMI, aad_client_id=APP
    )
    rules = yaml.split("inlineCode: |", 1)[0]

    assert '- "https://management.azure.com/"' in rules
    assert f'- "{UAMI}"' in rules
    assert f'- "{APP}"' in rules
