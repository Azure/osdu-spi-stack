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

"""Tests for the ARM-token deployer object-ID resolution.

Guards the Graph-free path: Conditional Access token protection can refuse
to issue Microsoft Graph tokens (AADSTS530084) while ARM access works, so
the deployer OID must be recoverable from a cached ARM access token alone.
"""

import base64
import json
from unittest.mock import MagicMock, patch

from spi.azure_infra import _decode_jwt_claim, _deployer_oid_from_arm_token

# Synthetic fixture values; never real principal or tenant identifiers.
OID = "11111111-2222-4333-8444-555555555555"
TID = "99999999-8888-4777-8666-555555555555"


def _forge_jwt(payload: dict) -> str:
    """Build an unsigned JWT-shaped token: header.payload.signature."""

    def b64(part: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64(payload)}.fakesig"


class TestDecodeJwtClaim:
    def test_extracts_oid(self):
        token = _forge_jwt({"oid": OID, "tid": TID, "aud": "https://management.azure.com/"})
        assert _decode_jwt_claim(token, "oid") == OID

    def test_missing_claim_returns_empty(self):
        token = _forge_jwt({"tid": TID})
        assert _decode_jwt_claim(token, "oid") == ""

    def test_non_string_claim_returns_empty(self):
        token = _forge_jwt({"oid": ["not", "a", "string"]})
        assert _decode_jwt_claim(token, "oid") == ""

    def test_unpadded_base64_segment(self):
        # urlsafe_b64encode output stripped of '=' must still decode. Grow a
        # filler claim until the stripped payload is not a multiple of 4,
        # which is the shape real AAD tokens arrive in.
        for pad in range(3):
            token = _forge_jwt({"oid": OID, "x": "y" * pad})
            if len(token.split(".")[1]) % 4 != 0:
                break
        assert len(token.split(".")[1]) % 4 != 0
        assert _decode_jwt_claim(token, "oid") == OID

    def test_garbage_token_returns_empty(self):
        assert _decode_jwt_claim("not-a-jwt", "oid") == ""
        assert _decode_jwt_claim("", "oid") == ""
        assert _decode_jwt_claim("a.!!!not-base64!!!.c", "oid") == ""


class TestDeployerOidFromArmToken:
    def _result(self, returncode=0, stdout=""):
        result = MagicMock()
        result.returncode = returncode
        result.stdout = stdout
        return result

    def test_returns_oid_from_token(self):
        token = _forge_jwt({"oid": OID})
        with patch("spi.azure_infra.run_command") as run:
            run.return_value = self._result(stdout=token + "\n")
            assert _deployer_oid_from_arm_token() == OID
        # Must hit the token cache, never Graph.
        argv = run.call_args[0][0]
        assert argv[:3] == ["az", "account", "get-access-token"]

    def test_az_failure_returns_empty(self):
        with patch("spi.azure_infra.run_command") as run:
            run.return_value = self._result(returncode=1)
            assert _deployer_oid_from_arm_token() == ""

    def test_malformed_token_returns_empty(self):
        with patch("spi.azure_infra.run_command") as run:
            run.return_value = self._result(stdout="garbage")
            assert _deployer_oid_from_arm_token() == ""


def test_resolver_prefers_the_arm_token_over_microsoft_graph():
    """Regression: _resolve_deployer_principal went straight to Graph, so a tenant
    whose Conditional Access blocks Graph (AADSTS530084) but permits ARM would fail
    -- and for a service principal it failed silently, skipping the Key Vault grant
    and only surfacing later during secret writes, after AKS already existed."""
    from spi import azure_infra

    account = json.dumps({"user": {"type": "user", "name": "someone@example.com"}})

    with (
        patch.object(azure_infra, "run_command") as run,
        patch.object(azure_infra, "_deployer_oid_from_arm_token", return_value="arm-oid") as arm,
    ):
        run.return_value = MagicMock(stdout=account, returncode=0)
        oid, principal_type = azure_infra._resolve_deployer_principal()

    assert oid == "arm-oid"
    assert principal_type == "User"
    arm.assert_called_once()
    # Graph must not be consulted when the ARM token already yielded the object ID.
    for call in run.call_args_list:
        assert "signed-in-user" not in call.args[0]


def test_resolver_falls_back_to_graph_when_the_arm_token_yields_nothing():
    """The ARM path is preferred, not mandatory: keep the Graph fallback working."""
    from spi import azure_infra

    account = json.dumps({"user": {"type": "user", "name": "someone@example.com"}})

    def fake_run(cmd, **kwargs):
        if "signed-in-user" in cmd:
            return MagicMock(stdout="graph-oid\n", returncode=0)
        return MagicMock(stdout=account, returncode=0)

    with (
        patch.object(azure_infra, "run_command", side_effect=fake_run),
        patch.object(azure_infra, "_deployer_oid_from_arm_token", return_value=""),
    ):
        oid, principal_type = azure_infra._resolve_deployer_principal()

    assert oid == "graph-oid"
    assert principal_type == "User"


def test_resolver_uses_the_arm_token_for_service_principals_too():
    """The SP path previously degraded to an empty OID on Graph failure, which
    silently skipped the Key Vault Secrets Officer assignment."""
    from spi import azure_infra

    account = json.dumps({"user": {"type": "servicePrincipal", "name": "app-id"}})

    with (
        patch.object(azure_infra, "run_command") as run,
        patch.object(azure_infra, "_deployer_oid_from_arm_token", return_value="sp-arm-oid"),
    ):
        run.return_value = MagicMock(stdout=account, returncode=0)
        oid, principal_type = azure_infra._resolve_deployer_principal()

    assert oid == "sp-arm-oid"
    assert principal_type == "ServicePrincipal"
    for call in run.call_args_list:
        assert "sp" not in call.args[0][:3] or "show" not in call.args[0]
