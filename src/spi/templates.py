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

"""YAML templates for Kubernetes resources."""

import hashlib
from typing import Sequence


def storage_class(
    name: str,
    provisioner: str,
    extra_params: str = "",
    reclaim_policy: str = "Delete",
    allow_volume_expansion: bool = True,
) -> str:
    yaml = f"""\
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: {name}
  labels:
    app.kubernetes.io/managed-by: osdu-spi-stack
provisioner: {provisioner}
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: {reclaim_policy}
allowVolumeExpansion: {str(allow_volume_expansion).lower()}"""
    if extra_params:
        yaml += f"\nparameters:\n{extra_params}"
    return yaml


def osdu_config_configmap(
    domain: str,
    primary_partition: str,
    tenant_id: str,
    identity_client_id: str,
    aad_client_id: str,
    keyvault_uri: str,
    keyvault_name: str,
    primary_cosmosdb_endpoint: str,
    primary_storage_account_name: str,
    primary_servicebus_namespace: str,
    appinsights_key: str = "",
) -> str:
    """ConfigMap with Azure PaaS endpoints for OSDU services.

    Services resolve per-request backends through partition-service; the
    PRIMARY_* keys exist for the schema-load Job, which targets the
    primary-only system database, and for operator visibility.

    aad_client_id is the resource core-lib-azure scopes `getWIToken` to for
    service-to-service calls, and one of the audiences the Istio jwtRules
    accept. It is not the appid the Lua projects; that is each token's own.
    """
    return f"""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: osdu-config
  namespace: osdu
  labels:
    app.kubernetes.io/managed-by: osdu-spi-stack
data:
  DOMAIN: "{domain}"
  PRIMARY_PARTITION: "{primary_partition}"
  AZURE_TENANT_ID: "{tenant_id}"
  AAD_CLIENT_ID: "{aad_client_id}"
  KEYVAULT_URI: "{keyvault_uri}"
  KEYVAULT_URL: "{keyvault_uri}"
  KEYVAULT_NAME: "{keyvault_name}"
  PRIMARY_COSMOSDB_ENDPOINT: "{primary_cosmosdb_endpoint}"
  COSMOSDB_DATABASE: "osdu-db"
  PRIMARY_STORAGE_ACCOUNT_NAME: "{primary_storage_account_name}"
  PRIMARY_SERVICEBUS_NAMESPACE: "{primary_servicebus_namespace}"
  REDIS_PORT: "6379"
  SERVER_PORT: "8080"
  APPINSIGHTS_KEY: "{appinsights_key}"
  ELASTICSEARCH_HOST: "elasticsearch-es-http.platform.svc"
"""


# The subjects infra/modules/identity.bicep federates the deploy and no-access
# identities to; spi up applies the accounts and spi token mints through them.
# The namespace is outside the mesh: test Jobs reach services through the gateway.
TESTER_NAMESPACE = "spi-test"
DEPLOYER_SERVICE_ACCOUNT = "spi-deployer"
NO_ACCESS_SERVICE_ACCOUNT = "spi-no-access"
TESTER_SERVICE_ACCOUNTS = (
    (DEPLOYER_SERVICE_ACCOUNT, "deploy_identity_client_id"),
    (NO_ACCESS_SERVICE_ACCOUNT, "no_access_identity_client_id"),
)


def workload_identity_sa(
    namespace: str, client_id: str, tenant_id: str, name: str = "workload-identity-sa"
) -> str:
    """Workload Identity ServiceAccount for OSDU services.

    The default name is what every OSDU pod binds; the deploy and no-access
    identities each trust a differently named account in the same namespace,
    which spi token mints through and an in-cluster test Job can run as.
    """
    return f"""\
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {name}
  namespace: {namespace}
  annotations:
    azure.workload.identity/client-id: "{client_id}"
    azure.workload.identity/tenant-id: "{tenant_id}"
  labels:
    azure.workload.identity/use: "true"
    app.kubernetes.io/managed-by: osdu-spi-stack
"""


def istio_auth_resources(
    namespace: str,
    tenant_id: str,
    entra_client_id: str,
    aad_client_id: str,
) -> str:
    """Istio resources that project the caller's app id from a validated JWT.

    The RequestAuthentication validates the bearer and parks the payload as
    Envoy dynamic metadata; the EnvoyFilter's Lua writes x-app-id and
    x-user-id headers from it for the Spring filters in the *-azure images.
    The PeerAuthentication keeps mTLS PERMISSIVE so the bootstrap Jobs are
    not rejected.

    Both client ids are jwtRule audiences. Bootstrap Jobs and acceptance
    callers present ``aud=https://management.azure.com/``; service-to-service
    tokens carry ``aud=aad_client_id`` and must pass jwt_authn too. When the
    two ids are equal only one audience entry is emitted.

    The Lua projects every caller as itself: x-app-id is the token's own
    ``appid`` (v1) or ``azp`` (v2) and x-user-id follows the issuer-specific
    claims, so the audience a token was minted for never changes which
    principal a service sees. Authorization stays with entitlements.
    """
    extra_aud = (
        f'\n        - "{aad_client_id}"'
        if aad_client_id and aad_client_id != entra_client_id
        else ""
    )
    return f"""\
apiVersion: security.istio.io/v1
kind: RequestAuthentication
metadata:
  name: spi-osdu-jwt-authn
  namespace: {namespace}
  labels:
    app.kubernetes.io/managed-by: osdu-spi-stack
spec:
  jwtRules:
    - issuer: "https://sts.windows.net/{tenant_id}/"
      jwksUri: "https://login.microsoftonline.com/common/discovery/v2.0/keys"
      audiences:
        - "{entra_client_id}"{extra_aud}
        - "https://management.azure.com"
        - "https://management.azure.com/"
      outputPayloadToHeader: "x-payload"
      forwardOriginalToken: true
      fromHeaders:
        - name: Authorization
          prefix: "Bearer "
    - issuer: "https://login.microsoftonline.com/{tenant_id}/v2.0"
      jwksUri: "https://login.microsoftonline.com/common/discovery/v2.0/keys"
      audiences:
        - "{entra_client_id}"{extra_aud}
      outputPayloadToHeader: "x-payload"
      forwardOriginalToken: true
      fromHeaders:
        - name: Authorization
          prefix: "Bearer "
---
apiVersion: security.istio.io/v1
kind: PeerAuthentication
metadata:
  name: spi-osdu-mtls
  namespace: {namespace}
  labels:
    app.kubernetes.io/managed-by: osdu-spi-stack
spec:
  mtls:
    mode: PERMISSIVE
---
apiVersion: networking.istio.io/v1alpha3
kind: EnvoyFilter
metadata:
  name: spi-osdu-identity-filter
  namespace: {namespace}
  labels:
    app.kubernetes.io/managed-by: osdu-spi-stack
spec:
  configPatches:
    - applyTo: HTTP_FILTER
      match:
        context: SIDECAR_INBOUND
        listener:
          filterChain:
            filter:
              name: envoy.filters.network.http_connection_manager
              subFilter:
                name: envoy.filters.http.router
      patch:
        operation: INSERT_BEFORE
        value:
          name: envoy.lua.spi-osdu-identity-filter
          typed_config:
            "@type": "type.googleapis.com/envoy.extensions.filters.http.lua.v3.Lua"
            inlineCode: |
              local AAD_V1_ISSUER = "sts.windows.net"
              local AAD_V2_ISSUER = "login.microsoftonline.com"

              local function processAADV1(payload, h)
                if payload["unique_name"] then
                  h:headers():add("x-user-id", payload["unique_name"])
                elseif payload["oid"] and payload["appid"] then
                  h:headers():add("x-user-id", payload["appid"])
                elseif payload["upn"] then
                  h:headers():add("x-user-id", payload["upn"])
                end
              end

              local function processAADV2(payload, h)
                if payload["unique_name"] then
                  h:headers():add("x-user-id", payload["unique_name"])
                elseif payload["oid"] then
                  h:headers():add("x-user-id", payload["oid"])
                elseif payload["azp"] then
                  h:headers():add("x-user-id", payload["azp"])
                end
              end

              function envoy_on_request(h)
                h:headers():remove("x-user-id")
                h:headers():remove("x-app-id")

                local meta = h:streamInfo():dynamicMetadata():get(
                  "envoy.filters.http.jwt_authn")
                if not meta or not meta["payload"] then
                  return
                end
                local payload = meta["payload"]

                local appId = payload["appid"] or payload["azp"]
                if appId then
                  h:headers():add("x-app-id", appId)
                end

                local iss = payload["iss"]
                if iss and string.find(iss, AAD_V1_ISSUER) then
                  processAADV1(payload, h)
                elseif iss and string.find(iss, AAD_V2_ISSUER) then
                  processAADV2(payload, h)
                end
              end
"""


# legal-init creates "{partition}-{LEGAL_TAG_BASE}"; must match the
# osdu-spi-init chart's `legalTag` default.
LEGAL_TAG_BASE = "demo-legaltag"


ENTITLEMENTS_MEMBERS_COMPONENT = "entitlements-members"
# Must equal membersGeneration in the init chart's values.yaml.
ENTITLEMENTS_MEMBERS_GENERATION = 2


def spi_init_values_configmap(
    partitions: list[str], members: Sequence[str] = (), legal_tag: str = LEGAL_TAG_BASE
) -> str:
    """ConfigMap consumed by the osdu-spi-init HelmRelease via valuesFrom.

    Lives in osdu-flux (where the HelmRelease is reconciled) and carries the
    full Helm values YAML. The CLI writes it based on --partition flags so that
    enabling a new partition is a CLI argument change, not a git edit.
    `spi info` reads the same ConfigMap back, so the legal tag name it reports
    is the one the init Jobs rendered from. ``members`` are the principals
    entitlements-members seeds; omitted entirely when empty so the chart
    renders no Job.
    """
    partition_lines = "\n".join(f"    - {p}" for p in partitions)
    member_lines = "".join(f"    - {m}\n" for m in sorted(set(members)))
    members_block = f"    entitlementsMembers:\n{member_lines}" if member_lines else ""
    return f"""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: spi-init-values
  namespace: osdu-flux
  labels:
    app.kubernetes.io/managed-by: osdu-spi-stack
data:
  values.yaml: |
    partitions:
{partition_lines}
    legalTag: {legal_tag}
{members_block}"""


def parse_init_values(text: str) -> dict:
    """Parse the CLI-written values.yaml blob without a yaml dependency.

    The shape is fixed: scalar keys and list keys whose items are ``- x``
    lines. Returns ``{"partitions": [...], "legalTag": "...",
    "entitlementsMembers": [...]}`` with absent keys missing.
    """
    parsed: dict = {}
    current: list | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") and current is not None:
            current.append(stripped[2:].strip())
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            current = None
            continue
        value = value.strip()
        if value:
            parsed[key.strip()] = value
            current = None
        else:
            current = []
            parsed[key.strip()] = current
    return parsed


def entitlements_members_job_name(partition: str, members: Sequence[str]) -> str:
    """The Job name the chart renders for this member list.

    Must match templates/entitlements-members.yaml: sortAlpha, join ",",
    "#" and the generation, sha256sum, trunc 8. A render test holds the
    two together.
    """
    seed = f"{','.join(sorted(set(members)))}#{ENTITLEMENTS_MEMBERS_GENERATION}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
    return f"{ENTITLEMENTS_MEMBERS_COMPONENT}-{partition}-{digest}"
