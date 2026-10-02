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

"""The signed-in person's own token and the id the mesh gives it.

Entitlements knows a caller by the ``x-user-id`` header, which the Istio
identity filter writes from the token's claims. A person is a member only
when the id stored in entitlements equals that value, so the id is read from
the token rather than asked for.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Optional

from .bootstrap import read_cluster_config, read_workload_identity_client_id
from .info import _read_osdu_config, token_audience
from .shell import run_command
from .status import STATUS_API_VERSION

_V1_ISSUER = "sts.windows.net"
_V2_ISSUER = "login.microsoftonline.com"
_APP_CLAIMS = ("appid", "azp")


class IdentityError(RuntimeError):
    """The person's token could not be used; the message names the fix."""


def decode_claims(token: str) -> dict:
    """A JWT's payload, unverified; empty when the token is not a JWT.

    The token comes from the local az cache and is read only to learn the
    caller's own claims. It never backs an authorization decision here.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def projected_user_id(claims: dict) -> tuple[str, str]:
    """The ``x-user-id`` the mesh writes for these claims, and the claim it came from.

    Mirrors processAADV1 and processAADV2 in the identity filter Lua in
    templates.istio_auth_resources. Both are empty when the filter would
    write nothing.
    """
    issuer = str(claims.get("iss") or "")
    if _V1_ISSUER in issuer:
        if claims.get("unique_name"):
            return str(claims["unique_name"]), "unique_name"
        if claims.get("oid") and claims.get("appid"):
            return str(claims["appid"]), "appid"
        if claims.get("upn"):
            return str(claims["upn"]), "upn"
    elif _V2_ISSUER in issuer:
        for claim in ("unique_name", "oid", "azp"):
            if claims.get(claim):
                return str(claims[claim]), claim
    return "", ""


@dataclass(frozen=True)
class PersonToken:
    access_token: str
    expires_on: str
    audience: str
    user_id: str
    claim: str

    def as_dict(self) -> dict[str, str]:
        return {
            "apiVersion": STATUS_API_VERSION,
            "token": self.access_token,
            "expires_on": self.expires_on,
            "audience": self.audience,
            "user": self.user_id,
            "claim": self.claim,
        }


def person_token(resource: Optional[str] = None) -> PersonToken:
    """The signed-in person's bearer for the environment, from the az cache.

    ``resource`` defaults to the audience ``spi token`` mints for, and the
    token is requested from the environment's tenant so a login whose
    default subscription sits elsewhere still yields a token the mesh admits.
    """
    audience = resource or token_audience(
        _read_osdu_config().get("AAD_CLIENT_ID", ""), read_workload_identity_client_id()
    )
    command = [
        "az",
        "account",
        "get-access-token",
        "--resource",
        audience,
        "--query",
        "accessToken",
        "--output",
        "tsv",
    ]
    tenant_id = read_cluster_config().get("AZURE_TENANT_ID", "")
    if tenant_id:
        command += ["--tenant", tenant_id]
    result = run_command(command, display=False, check=False)
    token = (result.stdout or "").strip()
    if result.returncode != 0 or not token:
        detail = (result.stderr or "").strip().splitlines()
        reason = f": {detail[-1]}" if detail else ""
        raise IdentityError(
            f"Could not get your token for {audience} from az{reason}. Sign in with 'az login'."
        )
    claims = decode_claims(token)
    user_id, claim = projected_user_id(claims)
    if not user_id:
        raise IdentityError(
            "Your az token carries no claim the mesh uses to name a caller "
            "(looked for unique_name, appid with oid, upn, oid, azp)."
        )
    # A v2 app-only token is projected by oid and may omit idtyp; only a delegated one has scp.
    app_only = claim == "oid" and not claims.get("scp")
    if claim in _APP_CLAIMS or claims.get("idtyp") == "app" or app_only:
        raise IdentityError(
            f"az is signed in as the application {user_id}, not a person. "
            "Sign in with 'az login' as yourself."
        )
    return PersonToken(
        access_token=token,
        expires_on=str(claims.get("exp", "")),
        audience=audience,
        user_id=user_id,
        claim=claim,
    )
