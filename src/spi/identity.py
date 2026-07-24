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

"""Azure token identity helpers shared with creator-access initialization."""

from __future__ import annotations

import base64
import json
from typing import Any, Mapping


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decode JWT claims without validating a token already issued by Azure CLI."""

    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("Azure CLI returned a malformed access token")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload).decode("utf-8")
        claims = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Azure CLI returned an unreadable access token") from exc
    if not isinstance(claims, dict):
        raise ValueError("Azure CLI access token payload is not a JSON object")
    return claims


def projected_user_id(claims: Mapping[str, Any]) -> str:
    """Return the identifier projected by the Stack's Istio Lua filter."""

    issuer = str(claims.get("iss", ""))
    if "sts.windows.net" in issuer:
        if claims.get("unique_name"):
            return str(claims["unique_name"])
        if claims.get("oid") and claims.get("appid"):
            return str(claims["appid"])
        if claims.get("upn"):
            return str(claims["upn"])
    elif "login.microsoftonline.com" in issuer:
        for name in ("unique_name", "oid", "azp"):
            if claims.get(name):
                return str(claims[name])
    raise ValueError("Azure token has no identity claim projected by the Stack gateway")


def projected_user_ids(claims: Mapping[str, Any]) -> list[str]:
    """Return creator identifiers that cover both accepted AAD token versions."""

    identifiers = [projected_user_id(claims)]
    is_user = bool(
        claims.get("unique_name")
        or claims.get("upn")
        or claims.get("scp")
        or claims.get("idtyp") == "user"
    )
    oid = str(claims.get("oid", "")).strip()
    if is_user and oid and oid.lower() not in {value.lower() for value in identifiers}:
        identifiers.append(oid)
    return identifiers
