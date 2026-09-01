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

    aad_client_id is the app id the Spring auth filters match against the
    JWT appid claim and core-lib-azure scopes `getWIToken` to.
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
    """Istio resources that project the caller's app id from a validated JWT.

    The RequestAuthentication validates the bearer and parks the payload as
    Envoy dynamic metadata; the EnvoyFilter's Lua writes x-app-id and
    x-user-id headers from it for the Spring filters in the *-azure images.
    The PeerAuthentication keeps mTLS PERMISSIVE so the bootstrap Jobs are
    not rejected.

    Both client ids are jwtRule audiences. Bootstrap Jobs present
    ``aud=https://management.azure.com/`` and the Lua pins their x-app-id to
    ``entra_client_id``; service-to-service tokens carry
    ``aud=aad_client_id`` and must pass jwt_authn too. When the two ids are
    equal only one audience entry is emitted.
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
              local entraClientId = "{entra_client_id}"

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

                local aud = payload["aud"]
                if aud then
                  h:headers():add("x-app-id", aud)
                  if aud == "https://management.azure.com/"
                     or aud == "https://management.azure.com" then
                    if payload["appid"] then
                      h:headers():replace("x-app-id", entraClientId)
                      h:headers():add("x-user-id", entraClientId)
                    end
                    return
                  end
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


def spi_init_values_configmap(partitions: list[str]) -> str:
    """ConfigMap consumed by the osdu-spi-init HelmRelease via valuesFrom.

    Lives in osdu-flux (where the HelmRelease is reconciled) and carries the
    full Helm values YAML. The CLI writes it based on --partition flags so that
    enabling a new partition is a CLI argument change, not a git edit.
    `spi info` reads the same ConfigMap back, so the legal tag name it reports
    is the one the init Jobs rendered from.
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
    legalTag: {LEGAL_TAG_BASE}
"""
