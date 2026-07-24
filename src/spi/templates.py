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

import json

# Disabled/dummy App Insights fallback. core-lib-azure >= 2.5.6 NPEs on every
# request if the App Insights SDK is not initialized (see osdu_config_configmap).
# When no real App Insights is provisioned we still set a syntactically valid
# connection string so the SDK initializes. Inline configuration disables
# telemetry collection, auxiliary exporters, and retry persistence.
_DUMMY_AI_INSTRUMENTATION_KEY = "00000000-0000-0000-0000-000000000000"
_DUMMY_AI_CONNECTION_STRING = (
    f"InstrumentationKey={_DUMMY_AI_INSTRUMENTATION_KEY};IngestionEndpoint=https://localhost/"
)
_DUMMY_AI_CONFIGURATION = (
    f'{{"connectionString":"{_DUMMY_AI_CONNECTION_STRING}",'
    '"sampling":{"percentage":0},'
    '"preview":{"liveMetrics":{"enabled":false},"profiler":{"enabled":false},'
    '"statsbeat":{"disabled":true},"diskPersistenceMaxSizeMb":0},'
    '"internal":{"statsbeat":{"disabledAll":true},'
    '"preAggregatedStandardMetrics":{"enabled":false}}}'
)


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
    app_insights_connection_string: str = "",
) -> str:
    """ConfigMap with Azure PaaS endpoints for OSDU services.

    The PRIMARY_* keys carry the primary partition's data plane endpoints.
    OSDU services do not consume them for per-request routing — they call
    partition-service to resolve each request's backend by partition id.
    The keys exist for the schema-load Job (which targets osdu-system-db,
    primary-only by design — ADR-013) and for operator visibility.

    aad_client_id is the Entra app id used by the Spring auth filters to
    match the JWT appid claim, and by core-lib-azure to build the
    `${{aadClientId}}/.default` scope inside `getWIToken`. Defaults to the
    UAMI client id (single-resource scope, dodges AADSTS28000); override
    with the AAD_CLIENT_ID host env var to point at a separate OSDU app
    registration.

    APPLICATIONINSIGHTS_CONNECTION_STRING / APPINSIGHTS_INSTRUMENTATIONKEY are
    consumed by the bundled App Insights Java agent and the core-lib-azure 2.x
    web SDK. core-lib-azure >= 2.5.6 ships LogCustomDimensionFilter, which reads
    the App Insights request-telemetry context on EVERY request with no null
    guard; if App Insights is not initialized the service returns HTTP 500 on
    every request. AKS Automatic enabled App Insights by default, but AKS Base
    does not, so we always populate a connection string here -- the real one
    when App Insights is provisioned (infra/main.bicep), or a disabled/dummy
    fallback (telemetry goes nowhere) so the SDK still initializes and the
    filter does not NPE.
    """
    ai_conn = app_insights_connection_string or _DUMMY_AI_CONNECTION_STRING
    ai_key = appinsights_key or _DUMMY_AI_INSTRUMENTATION_KEY
    ai_disabled_config = (
        f"  APPLICATIONINSIGHTS_CONFIGURATION_CONTENT: '{_DUMMY_AI_CONFIGURATION}'\n"
        if not app_insights_connection_string
        else ""
    )
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
  APPINSIGHTS_KEY: "{ai_key}"
  APPINSIGHTS_INSTRUMENTATIONKEY: "{ai_key}"
  APPLICATIONINSIGHTS_CONNECTION_STRING: "{ai_conn}"
  APPLICATIONINSIGHTS_SELF_DIAGNOSTICS_LEVEL: "OFF"
{ai_disabled_config}\
  ELASTICSEARCH_HOST: "elasticsearch-es-http.platform.svc"
"""


def workload_identity_sa(namespace: str, client_id: str, tenant_id: str) -> str:
    """Workload Identity ServiceAccount for OSDU services."""
    return f"""\
apiVersion: v1
kind: ServiceAccount
metadata:
  name: workload-identity-sa
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
    """RequestAuthentication + PeerAuthentication + EnvoyFilter required for
    Azure-provider OSDU services to extract the caller's app id from a
    validated JWT (see ADR-016).

    The RequestAuthentication validates the bearer and parks the decoded
    payload as Envoy dynamic metadata. The EnvoyFilter's Lua reads that
    metadata and writes x-app-id / x-user-id headers, which the in-process
    Spring filters in the *-azure service images consume. The
    PeerAuthentication keeps mTLS in PERMISSIVE mode so the bootstrap Jobs
    are not rejected by managed-mesh defaults.

    Both ``entra_client_id`` (the OSDU UAMI client id) and ``aad_client_id``
    are listed in the jwtRules audiences, alongside
    ``https://management.azure.com[/]`` which the bootstrap Jobs and onboarded
    CI identities present. The Lua does not special-case any audience or
    identity: it projects the caller's own application id (``appid`` for v1
    app/MSI tokens, ``azp`` for v2) as ``x-app-id`` / ``x-user-id``, and the
    access decision belongs to entitlements (the projected identity must be a
    member; ``spi onboard`` seeds CI identities via the AddMember API).
    Service-to-service calls inside the cluster mint tokens via core-lib-azure's
    ``getWIToken`` with scope ``${{aadClientId}}/.default`` (i.e.
    ``aud=aad_client_id``), so ``aad_client_id`` must also be a valid audience
    for those calls to pass jwt_authn. When the operator does not override
    AAD_CLIENT_ID, both values are equal and only one entry is emitted per
    jwtRule.
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

                -- This filter ONLY projects identity. It extracts the calling application
                -- id (appid for v1 app/MSI tokens, azp for v2, falling back to the audience
                -- for user tokens that carry neither) as x-app-id, and the caller identity
                -- as x-user-id (below). The actual access decision belongs to entitlements,
                -- which authorizes the projected x-user-id by group membership.
                local appId = payload["appid"] or payload["azp"] or payload["aud"]
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


def spi_init_values_configmap(
    partitions: list[str], creator_user_ids: list[str] | None = None
) -> str:
    """ConfigMap consumed by the osdu-spi-init HelmRelease via valuesFrom.

    Lives in osdu-flux (where the HelmRelease is reconciled) and carries the
    full Helm values YAML. The CLI writes it based on --partition flags so that
    enabling a new partition is a CLI argument change, not a git edit.
    """
    partition_lines = "\n".join(f"    - {p}" for p in partitions)
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
    creatorUserIds: {json.dumps(creator_user_ids or [])}
"""
