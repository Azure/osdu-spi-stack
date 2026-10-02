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

"""The person's own token and the id the mesh projects for it."""

import base64
import json
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from spi import cli, identity

runner = CliRunner()

# Synthetic fixture values; never real principal or tenant identifiers.
V1 = "https://sts.windows.net/tenant-id/"
V2 = "https://login.microsoftonline.com/tenant-id/v2.0"
PERSON = {"iss": V1, "unique_name": "alice@contoso.com", "oid": "oid-1", "appid": "az-cli"}


def _jwt(claims: dict) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part(claims)}.sig"


@pytest.mark.parametrize(
    "claims, expected",
    [
        (PERSON, ("alice@contoso.com", "unique_name")),
        ({"iss": V1, "oid": "oid-1", "appid": "app-1"}, ("app-1", "appid")),
        ({"iss": V1, "upn": "alice@contoso.com"}, ("alice@contoso.com", "upn")),
        ({"iss": V1, "appid": "app-1", "upn": "alice@contoso.com"}, ("alice@contoso.com", "upn")),
        ({"iss": V1, "oid": "oid-1"}, ("", "")),
        ({"iss": V2, "oid": "oid-1", "azp": "app-1"}, ("oid-1", "oid")),
        (
            {"iss": V2, "unique_name": "alice@contoso.com", "oid": "oid-1"},
            ("alice@contoso.com", "unique_name"),
        ),
        ({"iss": V2, "azp": "app-1"}, ("app-1", "azp")),
        ({"iss": "https://example.com/", "unique_name": "alice@contoso.com"}, ("", "")),
    ],
)
def test_projected_id_follows_the_filter_claim_order(claims, expected):
    assert identity.projected_user_id(claims) == expected


def test_claims_of_a_non_jwt_are_empty():
    assert identity.decode_claims("not-a-jwt") == {}
    assert identity.decode_claims("a.!!!.c") == {}


def _person_token(claims=PERSON, *, returncode=0, stderr="", aad="uami-id", resource=None):
    result = CompletedProcess([], returncode, stdout=_jwt(claims) + "\n", stderr=stderr)
    with (
        patch("spi.identity._read_osdu_config", return_value={"AAD_CLIENT_ID": aad}),
        patch("spi.identity.read_workload_identity_client_id", return_value="uami-id"),
        patch("spi.identity.read_cluster_config", return_value={"AZURE_TENANT_ID": "tenant-id"}),
        patch("spi.identity.run_command", return_value=result) as run_command,
    ):
        person = identity.person_token(resource)
    return person, run_command


def test_person_token_asks_the_environment_tenant_for_the_management_audience():
    person, run_command = _person_token({**PERSON, "exp": 1700000000})

    command = run_command.call_args.args[0]
    assert command[command.index("--resource") + 1] == "https://management.azure.com"
    assert command[command.index("--tenant") + 1] == "tenant-id"
    assert run_command.call_args.kwargs["display"] is False
    assert (person.user_id, person.claim) == ("alice@contoso.com", "unique_name")
    assert person.expires_on == "1700000000"


def test_person_token_uses_an_app_registration_audience_when_the_environment_has_one():
    person, run_command = _person_token(aad="app-registration-id")

    command = run_command.call_args.args[0]
    assert command[command.index("--resource") + 1] == "app-registration-id"
    assert person.audience == "app-registration-id"


def test_person_token_names_az_login_when_az_has_no_session():
    with pytest.raises(identity.IdentityError, match="az login"):
        _person_token(returncode=1, stderr="ERROR: Please run 'az login' to setup account.")


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": V1, "oid": "oid-1", "appid": "app-1"},
        {"iss": V2, "oid": "oid-1", "azp": "app-1"},
        {"iss": V2, "oid": "oid-1", "azp": "app-1", "scp": "user_impersonation", "idtyp": "app"},
    ],
)
def test_person_token_refuses_a_service_principal_login(claims):
    with pytest.raises(identity.IdentityError, match="not a person"):
        _person_token(claims)


def test_person_token_accepts_a_delegated_v2_token_named_by_oid():
    person, _ = _person_token({"iss": V2, "oid": "oid-1", "azp": "az-cli", "scp": "access"})

    assert (person.user_id, person.claim) == ("oid-1", "oid")


def test_person_token_refuses_a_token_the_filter_cannot_name():
    with pytest.raises(identity.IdentityError, match="no claim"):
        _person_token({"iss": V1, "oid": "oid-1"})


def _token_cli(monkeypatch, args):
    monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
    person = identity.PersonToken("person-bearer", "1", "aud", "alice@contoso.com", "unique_name")
    with patch("spi.identity.person_token", return_value=person) as person_token:
        result = runner.invoke(cli.app, ["token", *args])
    return result, person_token


def test_token_me_prints_only_the_persons_token_on_stdout(monkeypatch):
    result, person_token = _token_cli(monkeypatch, ["--me", "--resource", "custom-aud"])

    assert result.exit_code == 0
    assert result.stdout == "person-bearer\n"
    assert "alice@contoso.com" in result.stderr
    person_token.assert_called_once_with("custom-aud")


def test_token_me_json_names_the_user_and_claim(monkeypatch):
    result, _ = _token_cli(monkeypatch, ["--me", "--json"])

    document = json.loads(result.stdout)
    assert document["apiVersion"] == "spi.osdu.dev/v1"
    assert (document["token"], document["user"], document["claim"]) == (
        "person-bearer",
        "alice@contoso.com",
        "unique_name",
    )


@pytest.mark.parametrize("other", ["--member", "--no-access"])
def test_token_me_cannot_be_combined_with_an_identity(monkeypatch, other):
    result, person_token = _token_cli(monkeypatch, ["--me", other])

    assert result.exit_code == 1
    assert result.stdout == ""
    person_token.assert_not_called()
