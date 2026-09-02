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

"""Azure PaaS infrastructure provisioning.

Everything Bicep can express lives in ``infra/aks.bicep`` and
``infra/main.bicep``. The imperative steps are the ones ARM cannot make:
creating the resource group Bicep deploys into, branching on a soft-deleted
Key Vault, merging the kubeconfig, enabling Istio CNI chaining, which the
provider rejects at creation, and granting the signed-in principal
cluster-admin on the cluster, since that principal is only known at run
time. Runtime Key Vault secrets (seed passwords, fixed hostnames, the Table
endpoint) are written later by ``deploy.py``.

``provision_azure_infra`` returns the infra_outputs dict the Kubernetes
bootstrap consumes. With ``dry_run`` the login check and resource group
creation still run, both templates go through ``what-if``, and the
post-deploy steps are skipped, returning an empty dict.
"""

import base64
import json
import os
import time
from typing import Any, Dict, Optional, Tuple

import typer

from .bicep import run_bicep_deployment
from .config import RG_SUFFIX_TAG, Config
from .console import console, display_result
from .paths import INFRA_ROOT
from .shell import run_command

INFRA_MAIN_BICEP = INFRA_ROOT / "main.bicep"
INFRA_AKS_BICEP = INFRA_ROOT / "aks.bicep"

# Passed to aks.bicep and used to resolve the zones this exact size can use.
SYSTEM_POOL_VM_SIZE = "Standard_D4lds_v5"


def _system_pool_vm_size() -> str:
    """SPI_SYSTEM_POOL_VM_SIZE overrides the default; zone resolution also
    preflights the size's ephemeral OS disk support.
    """
    return os.environ.get("SPI_SYSTEM_POOL_VM_SIZE", "").strip() or SYSTEM_POOL_VM_SIZE


# Bicep takes these names as parameters and never re-derives them. Globally
# unique resources (storage, Cosmos, Service Bus) carry config.name_suffix so
# the same env in two subscriptions does not collide; Key Vault and ACR get
# the suffix in Config.from_env.


def _with_suffix(base: str, suffix: str, limit: int) -> str:
    """Append the per-subscription suffix and truncate to the Azure limit.

    The base is truncated first so a long env name cannot clip the suffix
    off and reintroduce global-name collisions.
    """
    if not suffix:
        return base[:limit]
    return f"{base[: max(0, limit - len(suffix))]}{suffix}"


def _storage_name(prefix: str, env: str, suffix: str = "") -> str:
    """Generate a storage account name (lowercase alphanumeric, 3-24 chars)."""
    safe = (prefix + env).replace("-", "").replace("_", "").lower()
    return _with_suffix(safe, suffix, 24)


def _sb_name(partition: str, env: str, suffix: str = "") -> str:
    """Service Bus namespace name."""
    base = f"osdu-{env}-{partition}-bus"
    return _with_suffix(base, f"-{suffix}" if suffix else "", 50)


def _cosmos_sql_name(partition: str, env: str, suffix: str = "") -> str:
    """CosmosDB SQL account name for a partition."""
    base = f"osdu-{env}-{partition}-cosmos"
    return _with_suffix(base, f"-{suffix}" if suffix else "", 44)


def _cosmos_gremlin_name(env: str, suffix: str = "") -> str:
    """CosmosDB Gremlin account name."""
    base = f"osdu-{env}-graph"
    return _with_suffix(base, f"-{suffix}" if suffix else "", 44)


def create_resource_group(config: Config):
    console.print("\n[bold]Creating resource group...[/bold]")
    # `az group create` on an existing group is an ARM PUT, and omitting
    # --tags clears the tag set, so only create when absent.
    exists = (
        run_command(
            ["az", "group", "exists", "--name", config.resource_group],
            description=f"Check resource group exists: {config.resource_group}",
            display=False,
        )
        .stdout.strip()
        .lower()
        == "true"
    )
    if exists:
        if read_rg_suffix_tag(config.resource_group) is None:
            write_rg_suffix_tag(config.resource_group, config.name_suffix)
        display_result(f"Resource group {config.resource_group} ready")
        return
    run_command(
        [
            "az",
            "group",
            "create",
            "--name",
            config.resource_group,
            "--location",
            config.location,
            "--tags",
            f"{RG_SUFFIX_TAG}={config.name_suffix}",
            "--output",
            "json",
        ],
        description=f"Create resource group: {config.resource_group}",
    )
    display_result(f"Resource group {config.resource_group} ready")


def read_rg_suffix_tag(resource_group: str) -> "str | None":
    """Read the `spi-name-suffix` tag from the resource group.

    Returns:
      - the suffix string (possibly empty for legacy deployments) when the
        tag exists,
      - None when the resource group doesn't exist or doesn't carry the tag.
    """
    result = run_command(
        [
            "az",
            "group",
            "show",
            "--name",
            resource_group,
            "--query",
            f'tags."{RG_SUFFIX_TAG}"',
            "--output",
            "tsv",
        ],
        description=f"Read suffix tag from resource group: {resource_group}",
        display=False,
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    # `az` prints the literal "None" when the tag is missing.
    if not value or value == "None":
        return None
    return value


def write_rg_suffix_tag(resource_group: str, suffix: str) -> None:
    """Persist the suffix on the resource group without disturbing other tags."""
    run_command(
        [
            "az",
            "group",
            "update",
            "--name",
            resource_group,
            "--set",
            f"tags.{RG_SUFFIX_TAG}={suffix}",
            "--output",
            "none",
        ],
        description=f"Persist {RG_SUFFIX_TAG} tag on resource group: {resource_group}",
    )


def detect_legacy_keyvault(resource_group: str, env: str) -> bool:
    """True when an existing unsuffixed Key Vault is present in the RG.

    Used to pin a pre-suffix deployment to legacy naming so re-runs reconcile
    the existing resources instead of standing up a parallel set.
    """
    if not env:
        return False
    safe_env = env.replace("-", "").replace("_", "")
    legacy_kv = f"osdu{safe_env}"[:24]
    result = run_command(
        [
            "az",
            "keyvault",
            "list",
            "--resource-group",
            resource_group,
            "--query",
            f"[?name=='{legacy_kv}'].name",
            "--output",
            "tsv",
        ],
        description=f"Probe for legacy Key Vault: {legacy_kv}",
        display=False,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _resolve_system_pool_zones(config: Config) -> "list | None":
    """Return the zones the system pool size can use in this subscription,
    or None when the SKU catalogue cannot be read and the template default
    should stand.
    """
    size = _system_pool_vm_size()
    result = run_command(
        [
            "az",
            "vm",
            "list-skus",
            "--location",
            config.location,
            "--size",
            size,
            "--resource-type",
            "virtualMachines",
            "--output",
            "json",
        ],
        description=f"Resolve system pool zones in {config.location}",
        display=False,
        check=False,
    )
    if result.returncode != 0:
        console.print(
            "  [warning]Could not read the compute SKU catalogue; "
            "using the template's default availability zones.[/warning]"
        )
        return None

    # Without --all the listing keeps zone-restricted SKUs with their
    # restriction payload and hides only sizes the subscription cannot deploy.
    published: list = []
    restricted: set = set()
    ephemeral_supported: "bool | None" = None
    # The catalogue returns canonical SKU names regardless of query casing.
    for sku in json.loads(result.stdout or "[]"):
        if (sku.get("name") or "").lower() != size.lower():
            continue
        for info in sku.get("locationInfo") or []:
            published.extend(info.get("zones") or [])
        for restriction in sku.get("restrictions") or []:
            if restriction.get("type") == "Zone":
                restricted.update((restriction.get("restrictionInfo") or {}).get("zones") or [])
        for capability in sku.get("capabilities") or []:
            if capability.get("name") == "EphemeralOSDiskSupported":
                ephemeral_supported = str(capability.get("value")).lower() == "true"

    if not published:
        raise RuntimeError(
            f"{size} is not offered in {config.location}, or this subscription is "
            "not offered the size there. Choose another region, or set "
            "SPI_SYSTEM_POOL_VM_SIZE to a size the subscription can deploy."
        )

    # An absent capability means unknown; only an explicit False is fatal.
    if ephemeral_supported is False:
        raise RuntimeError(
            f"{size} does not support the ephemeral OS disk the system pool "
            "requires (osDiskType Ephemeral in infra/aks.bicep). Set "
            "SPI_SYSTEM_POOL_VM_SIZE to a size with ephemeral OS disk support."
        )

    usable = sorted(set(published) - restricted)
    if not usable:
        raise RuntimeError(
            f"{size} has no usable availability zone in {config.location} for this subscription."
        )

    # Automatic validates the system pool against the region's published zone
    # set and refuses a reduced list, so a restricted zone is fatal.
    if len(usable) < len(set(published)):
        raise RuntimeError(
            f"AKS Automatic requires every availability zone in {config.location}, but "
            f"zone(s) {', '.join(sorted(restricted))} are restricted for "
            f"{size} in this subscription. Deploy in another region "
            "or subscription."
        )

    console.print(f"  [info]System pool availability zones: {', '.join(usable)}[/info]")
    return usable


def connect_cluster(resource_group: str, cluster_name: str) -> None:
    """Merge AKS credentials and pin kubelogin to the active Azure tenant."""

    console.print("\n[bold]Fetching cluster credentials...[/bold]")
    run_command(
        [
            "az",
            "aks",
            "get-credentials",
            "--resource-group",
            resource_group,
            "--name",
            cluster_name,
            "--overwrite-existing",
        ],
        description="Merge kubeconfig",
    )

    run_command(
        ["kubelogin", "convert-kubeconfig", "-l", "azurecli"],
        description="Convert kubeconfig to azurecli auth",
    )

    account_tenant = run_command(
        ["az", "account", "show", "--query", "tenantId", "--output", "tsv"],
        description="Get deployment tenant id",
        display=False,
        check=False,
    ).stdout.strip()
    kubeconfig_user = run_command(
        ["kubectl", "config", "view", "--minify", "-o", "jsonpath={.contexts[0].context.user}"],
        description="Get kubeconfig user entry",
        display=False,
        check=False,
    ).stdout.strip()
    if account_tenant and kubeconfig_user:
        run_command(
            [
                "kubectl",
                "config",
                "set-credentials",
                kubeconfig_user,
                "--exec-command=kubelogin",
                "--exec-arg=get-token",
                "--exec-arg=--login",
                "--exec-arg=azurecli",
                "--exec-arg=--server-id",
                "--exec-arg=6dae42f8-4368-4678-94ff-3960e28e3630",
                f"--exec-env=AZURE_TENANT_ID={account_tenant}",
                "--exec-api-version=client.authentication.k8s.io/v1beta1",
            ],
            description="Pin tenant in kubeconfig exec env",
            display=False,
            check=False,
        )


def create_aks_automatic(
    config: Config,
    deployer_principal_id: str,
    deployer_principal_type: str,
    system_pool_zones: "list | None" = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Deploy the AKS Automatic cluster from ``infra/aks.bicep``.

    ``system_pool_zones`` of None means the SKU catalogue was unreadable and
    the template default applies. Returns the flattened Bicep outputs
    (``clusterName``, ``clusterResourceId``, ``oidcIssuerUrl``,
    ``clusterPrincipalId``), or an empty dict under ``dry_run``.
    """
    header = "Previewing" if dry_run else "Deploying"
    console.print(f"\n[bold]{header} AKS Automatic cluster via Bicep...[/bold]")
    console.print("  [info]Cluster is declared in infra/aks.bicep.[/info]")
    aks_parameters = {
        "clusterName": config.cluster_name,
        "location": config.location,
        "systemPoolVmSize": _system_pool_vm_size(),
    }
    if system_pool_zones is not None:
        aks_parameters["availabilityZones"] = system_pool_zones
    aks_outputs = run_bicep_deployment(
        template_path=str(INFRA_AKS_BICEP),
        parameters=aks_parameters,
        resource_group=config.resource_group,
        deployment_name=f"spi-aks-{config.env or 'base'}",
        what_if=dry_run,
    )

    if dry_run:
        display_result("AKS Bicep what-if preview complete")
        return {}

    display_result(f"AKS Automatic cluster {config.cluster_name} ready")

    connect_cluster(config.resource_group, config.cluster_name)

    # The provider rejects proxyRedirectionMechanism at creation. CNI chaining
    # avoids the NET_ADMIN capability the default sidecar init container needs.
    _ensure_istio_cni_chaining(config)

    # Local accounts are disabled, so the deployer needs a cluster-admin role
    # assignment before kubectl can create namespaces; propagation takes
    # minutes and this blocks until it is active.
    _grant_deployer_cluster_admin(
        config,
        aks_outputs.get("clusterResourceId", ""),
        deployer_principal_id,
        deployer_principal_type,
    )

    # Deployment Safeguards stay enforced: on the Automatic SKU the admission
    # policy cannot be relaxed, so the local Helm charts satisfy it instead.
    return aks_outputs


def _grant_deployer_cluster_admin(
    config: Config,
    cluster_resource_id: str,
    deployer_principal_id: str,
    deployer_principal_type: str,
):
    """Grant the signed-in principal cluster-admin on the AKS cluster and wait for propagation.

    Required because AKS Automatic enforces Azure RBAC for Kubernetes and
    disables local accounts. Without this role, ``kubectl`` operations
    run by the deployer fail with ``User does not have access to the
    resource in Azure``.
    """
    if not cluster_resource_id:
        console.print("[warning]Cluster resource ID unavailable; skipping RBAC grant.[/warning]")
        return

    console.print("\n[bold]Granting deployer cluster-admin...[/bold]")
    run_command(
        [
            "az",
            "role",
            "assignment",
            "create",
            "--role",
            "Azure Kubernetes Service RBAC Cluster Admin",
            "--assignee-object-id",
            deployer_principal_id,
            "--assignee-principal-type",
            deployer_principal_type,
            "--scope",
            cluster_resource_id,
            "--output",
            "none",
        ],
        description=f"Assign cluster-admin to {deployer_principal_id[:8]}...",
        # On re-deploys the assignment exists and the CLI returns non-zero;
        # the ARM read below tells that apart from a real failure.
        check=False,
    )
    _verify_role_assignment_recorded(deployer_principal_id, cluster_resource_id)
    _wait_for_cluster_rbac()


def _decode_jwt_claim(token: str, claim: str) -> str:
    """Extract a claim from a JWT payload without signature verification.

    Safe here because the token comes straight from the local az token
    cache and is only mined for the caller's own object ID; it is never
    used to make an authorization decision.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        value = claims.get(claim, "")
        return value if isinstance(value, str) else ""
    except (IndexError, ValueError):
        return ""


def _deployer_oid_from_arm_token() -> str:
    """Read the signed-in principal's object ID from a cached ARM access token.

    Returns an empty string on any failure so callers can fall back to the
    Graph-based lookups.
    """
    result = run_command(
        [
            "az",
            "account",
            "get-access-token",
            "--resource",
            "https://management.azure.com/",
            "--query",
            "accessToken",
            "--output",
            "tsv",
        ],
        description="Get deployer object ID from ARM token",
        display=False,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return _decode_jwt_claim(result.stdout.strip(), "oid")


def _verify_role_assignment_recorded(user_oid: str, cluster_resource_id: str):
    """Confirm the cluster-admin assignment is visible in ARM before polling propagation.

    The preceding ``az role assignment create`` runs with ``check=False`` so a
    silent failure would otherwise be indistinguishable from slow AKS
    authorization-plane propagation. ARM listings respond within seconds and
    are independent of AKS-plane caching.
    """
    # Raw ARM rather than `az role assignment`, which resolves principals
    # through Graph and can be blocked by Conditional Access (AADSTS530084)
    # while ARM access is fine. The GUID is the built-in AKS RBAC Cluster
    # Admin role.
    aks_rbac_cluster_admin_role_id = "b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b"
    result = run_command(
        [
            "az",
            "rest",
            "--method",
            "get",
            "--url",
            (
                f"https://management.azure.com{cluster_resource_id}"
                "/providers/Microsoft.Authorization/roleAssignments"
                "?api-version=2022-04-01"
            ),
            "--query",
            (
                f"length(value[?properties.principalId=='{user_oid}' && "
                f"contains(properties.roleDefinitionId, '{aks_rbac_cluster_admin_role_id}')])"
            ),
            "--output",
            "tsv",
        ],
        description="Verify cluster-admin assignment exists",
        check=False,
        display=False,
    )
    count_str = (result.stdout or "").strip()
    if result.returncode != 0 or not count_str.isdigit() or int(count_str) < 1:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(
            f"Cluster-admin role assignment for {user_oid[:8]}... is not recorded on "
            f"{cluster_resource_id}. The preceding `az role assignment create` likely "
            f"failed silently. az stderr: {stderr!r}"
        )


def _wait_for_cluster_rbac(timeout_seconds: int = 600):
    """Poll ``kubectl auth can-i`` until AKS Azure RBAC recognizes the grant.

    Role assignment propagation to the AKS authorization layer typically
    takes 2-3 minutes for users and 5-8 minutes for service principals.
    Namespace creation is a representative cluster-scoped check.
    """
    last_response = ""
    last_returncode = -1
    with console.status("[bold]Waiting for AKS RBAC propagation (~2-8 min)...[/bold]"):
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            result = run_command(
                ["kubectl", "auth", "can-i", "create", "namespace"],
                description="Probe AKS RBAC",
                display=False,
                check=False,
            )
            last_returncode = result.returncode
            last_response = ((result.stdout or "") + (result.stderr or "")).strip()
            if result.returncode == 0 and "yes" in (result.stdout or "").lower():
                display_result("AKS Azure RBAC propagated")
                return
            time.sleep(10)
    raise RuntimeError(
        f"AKS Azure RBAC did not propagate within {timeout_seconds}s "
        f"(last kubectl returncode={last_returncode}, response={last_response!r}). "
        "Verify the deployer has 'Azure Kubernetes Service RBAC Cluster Admin' on the cluster."
    )


def _ensure_istio_cni_chaining(config: Config):
    """Enable Istio CNI chaining, which the provider rejects at cluster creation."""
    result = run_command(
        [
            "az",
            "aks",
            "show",
            "--resource-group",
            config.resource_group,
            "--name",
            config.cluster_name,
            "--query",
            "serviceMeshProfile.istio.components.proxyRedirectionMechanism",
            "--output",
            "tsv",
        ],
        description="Check Istio CNI chaining status",
        display=False,
    )
    if (result.stdout or "").strip() == "CNIChaining":
        display_result("Istio CNI chaining already enabled")
        return

    console.print("\n[bold]Enabling Istio CNI chaining...[/bold]")
    run_command(
        [
            "az",
            "aks",
            "mesh",
            "enable-istio-cni",
            "--resource-group",
            config.resource_group,
            "--name",
            config.cluster_name,
        ],
        description="Enable Istio CNI chaining",
    )
    display_result("Istio CNI chaining enabled")


def _recover_soft_deleted_keyvault(config: Config):
    """If the target Key Vault was previously soft-deleted, recover it.

    Bicep would otherwise fail with "vault name already exists in this
    region" when attempting to create a vault whose soft-deleted twin
    still occupies the namespace.
    """
    deleted_check = run_command(
        [
            "az",
            "keyvault",
            "list-deleted",
            "--query",
            f"[?name=='{config.keyvault_name}']",
            "--output",
            "json",
        ],
        description=f"Check for soft-deleted Key Vault: {config.keyvault_name}",
        check=False,
        display=False,
    )
    deleted_vaults = json.loads(deleted_check.stdout or "[]")
    if deleted_vaults:
        console.print(
            f"\n[warning]Recovering soft-deleted Key Vault '{config.keyvault_name}'...[/warning]"
        )
        run_command(
            [
                "az",
                "keyvault",
                "recover",
                "--name",
                config.keyvault_name,
                "--resource-group",
                config.resource_group,
                "--output",
                "json",
            ],
            description=f"Recover Key Vault: {config.keyvault_name}",
        )
        display_result(f"Key Vault {config.keyvault_name} recovered")


def _build_bicep_params(
    config: Config,
    oidc_issuer: str,
    deployer_principal_id: str,
    deployer_principal_type: str,
) -> Dict[str, Any]:
    """Translate Config into the parameter dict consumed by infra/main.bicep."""
    s = config.name_suffix
    return {
        "envName": config.env,
        "location": config.location,
        "identityName": config.identity_name,
        "externalDnsIdentityName": config.external_dns_identity_name,
        "keyVaultName": config.keyvault_name,
        "acrName": config.acr_name,
        "dataPartitions": config.data_partitions,
        "primaryPartition": config.primary_partition,
        "gremlinAccountName": _cosmos_gremlin_name(config.env, s),
        "commonStorageName": _storage_name("osdu" + config.env + "common", "", s),
        "cosmosSqlNames": [_cosmos_sql_name(p, config.env, s) for p in config.data_partitions],
        "serviceBusNames": [_sb_name(p, config.env, s) for p in config.data_partitions],
        "partitionStorageNames": [
            _storage_name("osdu" + config.env + p, "", s) for p in config.data_partitions
        ],
        "oidcIssuerUrl": oidc_issuer,
        # Empty outside dns mode; main.bicep's conditional modules then no-op.
        "dnsZoneName": config.dns_zone,
        "dnsZoneResourceGroup": config.dns_zone_rg,
        # rbac.bicep grants this principal Key Vault Secrets Officer for the
        # post-deploy secret writes; RG Owner carries no data-plane access.
        "deployerPrincipalId": deployer_principal_id,
        "deployerPrincipalType": deployer_principal_type,
    }


def _deployer_principal_type(account: Optional[Dict[str, Any]] = None) -> str:
    """Principal type of the logged-in az identity (User vs ServicePrincipal)."""
    override = os.environ.get("SPI_DEPLOYER_TYPE", "").strip()
    if override in ("User", "ServicePrincipal"):
        return override
    if account is not None:
        return "User" if account.get("user", {}).get("type") == "user" else "ServicePrincipal"
    result = run_command(
        ["az", "account", "show", "--query", "user.type", "--output", "tsv"],
        description="Get deployer principal type",
        check=False,
        display=False,
    )
    return "User" if (result.stdout or "").strip() == "user" else "ServicePrincipal"


def _resolve_deployer_principal(account: Dict[str, Any]) -> tuple[str, str]:
    """Resolve the deployer object ID without requiring Microsoft Graph.

    CI can provide ``SPI_DEPLOYER_OID`` while its GitHub OIDC assertion is
    fresh. Local users and service principals use the ``oid`` from the cached
    ARM access token. Graph is a best-effort fallback only: Conditional Access
    can refuse a Graph token even when ARM access is valid.
    """

    principal_type = _deployer_principal_type(account)
    principal_id = os.environ.get("SPI_DEPLOYER_OID", "").strip()
    if principal_id:
        return principal_id, principal_type

    principal_id = _deployer_oid_from_arm_token()
    if principal_id:
        return principal_id, principal_type

    if principal_type == "ServicePrincipal":
        app_id = account.get("user", {}).get("name", "")
        if app_id:
            result = run_command(
                ["az", "ad", "sp", "show", "--id", app_id, "--query", "id", "--output", "tsv"],
                description="Get deployer object ID (service principal)",
                display=False,
                check=False,
            )
            principal_id = (result.stdout or "").strip()
    else:
        result = run_command(
            ["az", "ad", "signed-in-user", "show", "--query", "id", "--output", "tsv"],
            description="Get deployer object ID",
            display=False,
            check=False,
        )
        principal_id = (result.stdout or "").strip()

    if not principal_id:
        console.print(
            "[error]Unable to resolve the deployer object ID from the ARM token or "
            "Microsoft Graph. Set SPI_DEPLOYER_OID and retry.[/error]"
        )
        raise typer.Exit(code=1)
    return principal_id, principal_type


def _reshape_bicep_outputs(bicep_outputs: Dict[str, Any]) -> Dict[str, Any]:
    """Convert Bicep camelCase outputs into the legacy infra_outputs dict.

    Bicep emits per-partition data as parallel arrays (indexed by the
    dataPartitions order). This function zips those arrays back into the
    per-partition keys that the downstream code reads
    (e.g., ``opendes_cosmos_endpoint``).
    """
    out: Dict[str, Any] = {
        "identity_client_id": bicep_outputs.get("identityClientId", ""),
        "identity_principal_id": bicep_outputs.get("identityPrincipalId", ""),
        "identity_id": bicep_outputs.get("identityResourceId", ""),
        "keyvault_uri": bicep_outputs.get("keyvaultUri", ""),
        "keyvault_id": bicep_outputs.get("keyvaultId", ""),
        "acr_id": bicep_outputs.get("acrId", ""),
        "acr_login_server": bicep_outputs.get("acrLoginServer", ""),
        "graph_endpoint": bicep_outputs.get("graphEndpoint", ""),
        "graph_account_id": bicep_outputs.get("graphAccountId", ""),
        "common_storage_name": bicep_outputs.get("commonStorageName", ""),
        "common_storage_id": bicep_outputs.get("commonStorageId", ""),
        # Empty outside dns mode.
        "external_dns_client_id": bicep_outputs.get("externalDnsClientId", ""),
        "external_dns_principal_id": bicep_outputs.get("externalDnsPrincipalId", ""),
    }

    partition_names = bicep_outputs.get("partitionNames", []) or []
    cosmos_endpoints = bicep_outputs.get("partitionCosmosEndpoints", []) or []
    cosmos_account_ids = bicep_outputs.get("partitionCosmosAccountIds", []) or []
    sb_ids = bicep_outputs.get("partitionServiceBusIds", []) or []
    sb_names = bicep_outputs.get("partitionServiceBusNames", []) or []
    storage_ids = bicep_outputs.get("partitionStorageIds", []) or []
    storage_names = bicep_outputs.get("partitionStorageNamesOut", []) or []

    for i, partition in enumerate(partition_names):
        if i < len(cosmos_endpoints):
            out[f"{partition}_cosmos_endpoint"] = cosmos_endpoints[i]
        if i < len(cosmos_account_ids):
            out[f"{partition}_cosmos_account_id"] = cosmos_account_ids[i]
        if i < len(sb_ids):
            out[f"{partition}_servicebus_id"] = sb_ids[i]
        if i < len(sb_names):
            out[f"{partition}_sb_namespace"] = sb_names[i]
        if i < len(storage_ids):
            out[f"{partition}_storage_id"] = storage_ids[i]
        if i < len(storage_names):
            out[f"{partition}_storage_name"] = storage_names[i]

    return out


def _get_azure_account() -> Dict[str, Any]:
    """Return the active Azure account without mutating Azure state."""
    console.print("\n[bold]Verifying Azure login...[/bold]")
    result = run_command(
        ["az", "account", "show", "--output", "json"],
        description="Check Azure subscription",
    )
    account = json.loads(result.stdout)
    console.print(
        f"  [info]Subscription: {account.get('name', 'unknown')} ({account.get('id', '')})[/info]"
    )
    return account


def provision_azure_infra(
    config: Config,
    dry_run: bool = False,
    *,
    account: Optional[Dict[str, Any]] = None,
    deployer_principal: Optional[Tuple[str, str]] = None,
) -> Dict[str, Any]:
    """Provision all Azure PaaS resources and return the infra outputs.

    The resource group is created even under ``dry_run`` because what-if
    needs a scope to preview into.
    """
    outputs: Dict[str, Any] = {}

    if account is None:
        account = _get_azure_account()
    outputs["tenant_id"] = account.get("tenantId", "")
    outputs["subscription_id"] = account.get("id", "")

    # Resolved before any Azure mutation; the same identity gets AKS
    # cluster-admin and Key Vault Secrets Officer.
    if deployer_principal is None:
        deployer_principal = _resolve_deployer_principal(account)
    deployer_principal_id, deployer_principal_type = deployer_principal

    # Preflight before the resource group exists, so an unusable system pool
    # size or zone set stops the run with nothing created.
    system_pool_zones = _resolve_system_pool_zones(config)

    create_resource_group(config)

    # Under dry-run the what-if returns no issuer, and an empty issuer makes
    # identity.bicep omit federated credentials from the main.bicep preview.
    aks_outputs = create_aks_automatic(
        config,
        deployer_principal_id,
        deployer_principal_type,
        system_pool_zones=system_pool_zones,
        dry_run=dry_run,
    )
    oidc_issuer = aks_outputs.get("oidcIssuerUrl", "")

    if not dry_run:
        _recover_soft_deleted_keyvault(config)

    header = "Previewing" if dry_run else "Deploying"
    console.print(f"\n[bold]{header} Azure PaaS resources via Bicep...[/bold]")
    console.print(
        "  [info]Identity, KeyVault, ACR, CosmosDB, Service Bus, Storage, "
        "and RBAC role assignments are declared in infra/main.bicep.[/info]"
    )
    bicep_params = _build_bicep_params(
        config,
        oidc_issuer,
        deployer_principal_id,
        deployer_principal_type,
    )
    bicep_outputs = run_bicep_deployment(
        template_path=str(INFRA_MAIN_BICEP),
        parameters=bicep_params,
        resource_group=config.resource_group,
        deployment_name=f"spi-{config.env or 'base'}",
        what_if=dry_run,
    )

    if dry_run:
        display_result("Bicep what-if preview complete")
        return outputs

    outputs.update(_reshape_bicep_outputs(bicep_outputs))
    display_result("Bicep deployment complete")

    return outputs
