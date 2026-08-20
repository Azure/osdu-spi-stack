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

import pytest
import typer

from spi.azure_infra import (
    _build_bicep_params,
    _decode_jwt_claim,
    _deployer_oid_from_arm_token,
    _resolve_deployer_principal,
    provision_azure_infra,
)
from spi.cli import _resolve_up_context
from spi.config import Config

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


class TestResolveDeployerPrincipal:
    def test_environment_override_wins_without_arm_or_graph(self, monkeypatch):
        monkeypatch.setenv("SPI_DEPLOYER_OID", OID)
        monkeypatch.setenv("SPI_DEPLOYER_TYPE", "ServicePrincipal")
        account = {"user": {"type": "user", "name": "ignored"}}

        with (
            patch("spi.azure_infra._deployer_oid_from_arm_token") as arm,
            patch("spi.azure_infra.run_command") as run,
        ):
            assert _resolve_deployer_principal(account) == (OID, "ServicePrincipal")

        arm.assert_not_called()
        run.assert_not_called()

    @pytest.mark.parametrize(
        ("account", "expected_type"),
        [
            ({"user": {"type": "user", "name": "user@example.com"}}, "User"),
            ({"user": {"type": "servicePrincipal", "name": "client-id"}}, "ServicePrincipal"),
        ],
    )
    def test_arm_token_oid_works_for_users_and_service_principals(
        self, monkeypatch, account, expected_type
    ):
        monkeypatch.delenv("SPI_DEPLOYER_OID", raising=False)
        monkeypatch.delenv("SPI_DEPLOYER_TYPE", raising=False)

        with (
            patch("spi.azure_infra._deployer_oid_from_arm_token", return_value=OID),
            patch("spi.azure_infra.run_command") as graph,
        ):
            assert _resolve_deployer_principal(account) == (OID, expected_type)

        graph.assert_not_called()

    def test_graph_is_a_best_effort_fallback(self, monkeypatch):
        monkeypatch.delenv("SPI_DEPLOYER_OID", raising=False)
        monkeypatch.delenv("SPI_DEPLOYER_TYPE", raising=False)
        account = {"user": {"type": "servicePrincipal", "name": "client-id"}}
        result = MagicMock(returncode=0, stdout=OID + "\n")

        with (
            patch("spi.azure_infra._deployer_oid_from_arm_token", return_value=""),
            patch("spi.azure_infra.run_command", return_value=result) as graph,
        ):
            assert _resolve_deployer_principal(account) == (OID, "ServicePrincipal")

        assert graph.call_args.args[0][:4] == ["az", "ad", "sp", "show"]
        assert graph.call_args.kwargs["check"] is False

    def test_graph_fallback_resolves_signed_in_user(self, monkeypatch):
        monkeypatch.delenv("SPI_DEPLOYER_OID", raising=False)
        monkeypatch.delenv("SPI_DEPLOYER_TYPE", raising=False)
        account = {"user": {"type": "user", "name": "user@example.com"}}
        result = MagicMock(returncode=0, stdout=OID + "\n")

        with (
            patch("spi.azure_infra._deployer_oid_from_arm_token", return_value=""),
            patch("spi.azure_infra.run_command", return_value=result) as graph,
        ):
            assert _resolve_deployer_principal(account) == (OID, "User")

        assert graph.call_args.args[0] == [
            "az",
            "ad",
            "signed-in-user",
            "show",
            "--query",
            "id",
            "--output",
            "tsv",
        ]
        assert graph.call_args.kwargs["check"] is False

    def test_unresolved_oid_fails_with_actionable_error(self, monkeypatch):
        monkeypatch.delenv("SPI_DEPLOYER_OID", raising=False)
        monkeypatch.delenv("SPI_DEPLOYER_TYPE", raising=False)
        account = {"user": {"type": "user", "name": "user@example.com"}}
        result = MagicMock(returncode=1, stdout="")

        with (
            patch("spi.azure_infra._deployer_oid_from_arm_token", return_value=""),
            patch("spi.azure_infra.run_command", return_value=result),
            pytest.raises(typer.Exit),
        ):
            _resolve_deployer_principal(account)


def test_bicep_params_receive_resolved_deployer_identity():
    params = _build_bicep_params(
        Config(env="test"),
        "https://oidc.example/",
        OID,
        "User",
    )

    assert params["deployerPrincipalId"] == OID
    assert params["deployerPrincipalType"] == "User"


def test_unresolved_deployer_fails_before_resource_group_creation(monkeypatch):
    monkeypatch.delenv("SPI_DEPLOYER_OID", raising=False)
    account_result = MagicMock(
        stdout=json.dumps(
            {
                "id": "subscription-id",
                "tenantId": TID,
                "name": "subscription",
                "user": {"type": "user", "name": "user@example.com"},
            }
        )
    )

    with (
        patch("spi.azure_infra.run_command", return_value=account_result),
        patch("spi.azure_infra._resolve_deployer_principal", side_effect=typer.Exit(code=1)),
        patch("spi.azure_infra.create_resource_group") as create_rg,
        pytest.raises(typer.Exit),
    ):
        provision_azure_infra(Config(env="test"))

    create_rg.assert_not_called()


def test_unresolved_deployer_fails_before_suffix_persistence():
    account = {
        "id": "subscription-id",
        "tenantId": TID,
        "name": "subscription",
        "user": {"type": "user", "name": "user@example.com"},
    }

    with (
        patch("spi.azure_infra._get_azure_account", return_value=account),
        patch(
            "spi.azure_infra._resolve_deployer_principal",
            side_effect=typer.Exit(code=1),
        ),
        patch("spi.cli._resolve_name_suffix") as resolve_suffix,
        pytest.raises(typer.Exit),
    ):
        _resolve_up_context("test")

    resolve_suffix.assert_not_called()
