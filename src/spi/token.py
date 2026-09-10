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

"""Mint an app-only Entra token as the environment's deploy or no-access identity.

Every OSDU Azure service admits only app-only tokens, so a developer's own
``az account get-access-token`` (which carries ``upn``) is refused. A
developer with cluster access instead asks the API server for a projected
ServiceAccount token and exchanges it at Entra, the same federation fork CI
performs with a GitHub OIDC token. The result carries the identity's
``appid`` and the v1 issuer, which the Istio filter projects as the caller.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .bootstrap import read_cluster_config
from .info import _read_osdu_config, _read_workload_identity_client_id, token_audience
from .shell import run_process
from .templates import DEPLOYER_SERVICE_ACCOUNT, NO_ACCESS_SERVICE_ACCOUNT, TESTER_NAMESPACE

EXCHANGE_AUDIENCE = "api://AzureADTokenExchange"
PROJECTED_TOKEN_DURATION = "10m"


class TokenError(RuntimeError):
    """The token could not be minted; the message names the fix."""


@dataclass(frozen=True)
class MintedToken:
    access_token: str
    expires_on: str
    client_id: str
    service_account: str
    audience: str

    def as_dict(self) -> dict[str, str]:
        return {
            "token": self.access_token,
            "expires_on": self.expires_on,
            "client_id": self.client_id,
            "service_account": f"{TESTER_NAMESPACE}/{self.service_account}",
            "audience": self.audience,
        }


def _projected_token(service_account: str) -> str:
    result = run_process(
        [
            "kubectl",
            "create",
            "token",
            service_account,
            "-n",
            TESTER_NAMESPACE,
            "--audience",
            EXCHANGE_AUDIENCE,
            "--duration",
            PROJECTED_TOKEN_DURATION,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or "kubectl failed"
        if "not found" in detail.lower():
            raise TokenError(
                f"ServiceAccount {TESTER_NAMESPACE}/{service_account} not found; run "
                "'spi up' on a release that provisions it first."
            )
        raise TokenError(f"Could not create a token for {service_account}: {detail}")
    token = result.stdout.strip()
    if not token:
        raise TokenError(f"kubectl returned an empty token for {service_account}")
    return token


def _exchange(tenant_id: str, client_id: str, assertion: str, resource: str) -> dict:
    """Client-credentials exchange at the v1 endpoint.

    The v2 endpoint emits ``azp`` and omits ``appid``; the Azure-provider
    services read ``appid``, so only the v1 endpoint yields a usable token.
    """
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
            "resource": resource,
        }
    ).encode()
    request = urllib.request.Request(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/token",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("error_description", detail)
        except ValueError:
            pass
        if "AADSTS70021" in detail or "AADSTS700213" in detail:
            raise TokenError(
                f"Entra holds no federated credential for the {TESTER_NAMESPACE} ServiceAccount "
                f"on {client_id}; run 'spi up' on a release that provisions it first."
            ) from None
        raise TokenError(f"Entra refused the exchange ({exc.code}): {detail.strip()}") from None
    except urllib.error.URLError as exc:
        raise TokenError(f"Could not reach Entra: {exc.reason}") from None
    except OSError as exc:
        raise TokenError(f"Could not reach Entra: {exc}") from None
    if "access_token" not in payload:
        raise TokenError(f"Entra returned no access token: {payload}")
    return payload


def mint_token(*, no_access: bool = False, resource: Optional[str] = None) -> MintedToken:
    """An app-only bearer as the deploy identity, or the no-access identity.

    ``resource`` defaults to the audience ``spi info`` publishes as
    ``azure.token_audience``: the management audience unless the operator
    overrode ``AAD_CLIENT_ID`` with an app registration.
    """
    cluster_cfg = read_cluster_config()
    key = "NO_ACCESS_IDENTITY_CLIENT_ID" if no_access else "DEPLOY_IDENTITY_CLIENT_ID"
    client_id = cluster_cfg.get(key, "")
    tenant_id = cluster_cfg.get("AZURE_TENANT_ID", "")
    missing = [
        name for name, value in ((key, client_id), ("AZURE_TENANT_ID", tenant_id)) if not value
    ]
    if missing:
        raise TokenError(
            f"spi-cluster-config carries no {', '.join(missing)}; run 'spi up' on a release "
            "that provisions the deploy identity first."
        )
    service_account = NO_ACCESS_SERVICE_ACCOUNT if no_access else DEPLOYER_SERVICE_ACCOUNT
    audience = resource or token_audience(
        _read_osdu_config().get("AAD_CLIENT_ID", ""), _read_workload_identity_client_id()
    )
    assertion = _projected_token(service_account)
    payload = _exchange(tenant_id, client_id, assertion, audience)
    return MintedToken(
        access_token=str(payload["access_token"]),
        expires_on=str(payload.get("expires_on", "")),
        client_id=client_id,
        service_account=service_account,
        audience=audience,
    )
