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

Hybrid model:
  - Resource Group creation is imperative (``az group create``); Bicep
    cannot create the RG it deploys into.
  - AKS Automatic is declared in Bicep at ``infra/aks.bicep`` (AVM
    ``container-service/managed-cluster``). Two post-deploy imperative
    steps remain for gaps the AVM module does not cover:
    ``az aks get-credentials`` (kubeconfig merge; not a resource) and
    ``az aks mesh enable-istio-cni`` (AVM typed ``proxyRedirectionMechanism``
    out of the IstioComponents schema).
  - Key Vault soft-delete recovery is imperative pre-check (ARM cannot
    branch on a list-deleted query).
  - Everything else (Managed Identity, federated credentials, Key Vault
    creation + metadata secrets + Cosmos primary keys via ``listKeys()``,
    ACR, CosmosDB Gremlin + SQL, Service Bus + topics/subs, Storage +
    containers/tables, RBAC role assignments) is declared in Bicep at
    ``infra/main.bicep`` and deployed with ``az deployment group create``.
  - Runtime-only Key Vault secrets that depend on in-cluster seed
    passwords (tbl-storage-endpoint, redis-*, {partition}-elastic-*)
    are still written by the CLI from ``runtime_bootstrap.py`` after
    Flux has reconciled the middleware layer.

The function ``provision_azure_infra(config, dry_run=False)`` returns the
infra_outputs dict consumed by ``_create_osdu_config`` and workload-
identity ServiceAccount creation. When ``dry_run`` is True, the Azure
login check, resource group creation, and ``az deployment group what-if``
against both ``aks.bicep`` and ``main.bicep`` run; all post-deploy steps
are skipped and an empty outputs dict is returned.
"""

import json
import os
import time
from typing import Any, Dict

from .bicep import run_bicep_deployment
from .config import RG_SUFFIX_TAG, Config
from .console import console, display_result
from .paths import INFRA_ROOT
from .shell import run_command

INFRA_MAIN_BICEP = INFRA_ROOT / "main.bicep"
INFRA_AKS_BICEP = INFRA_ROOT / "aks.bicep"


# ─────────────────────────────────────────────────────────────
# Resource-name helpers (preserve the existing naming contract).
# Bicep consumes these via parameters; the template does not
# re-derive names.
#
# Every globally unique resource (storage, Cosmos, Service Bus)
# carries the per-subscription suffix from config.name_suffix so
# `spi up --env dev1` in two different subscriptions does not
# collide. KV and ACR already include the suffix via Config.from_env.
# ─────────────────────────────────────────────────────────────


def _with_suffix(base: str, suffix: str, limit: int) -> str:
    """Append the per-subscription suffix and truncate to the Azure limit.

    Truncates the base first to reserve room for the suffix; a naive
    f"{base}{suffix}"[:limit] would clip the suffix off for long bases
    (e.g. env "productiondev" + "common") and reintroduce global-name
    collisions.
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


# ─────────────────────────────────────────────────────────────
# Phase 1: Core infrastructure (imperative; Bicep-incompatible)
# ─────────────────────────────────────────────────────────────


def create_resource_group(config: Config):
    console.print("\n[bold]Creating resource group...[/bold]")
    # `az group create` is idempotent. The `--tags` flag REPLACES the entire
    # tag set, so we only pass it the first time (when no suffix tag exists
    # yet — read_rg_suffix_tag returns None). Otherwise the tag has already
    # been written, and we re-run create-without-tags to leave it intact.
    cmd = [
        "az",
        "group",
        "create",
        "--name",
        config.resource_group,
        "--location",
        config.location,
        "--output",
        "json",
    ]
    if read_rg_suffix_tag(config.resource_group) is None:
        cmd.extend(["--tags", f"{RG_SUFFIX_TAG}={config.name_suffix}"])
    run_command(cmd, description=f"Create resource group: {config.resource_group}")
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
    # `az` prints "None" (literal) when the tag is missing on an existing RG.
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


def create_aks_automatic(config: Config, dry_run: bool = False) -> Dict[str, Any]:
    """Create an AKS Automatic cluster + managed Istio via Bicep.

    The cluster is declared in ``infra/aks.bicep`` using the AVM
    ``container-service/managed-cluster`` module. Two imperative post-
    deploy steps remain for gaps the AVM module does not cover:
    kubeconfig merge (``az aks get-credentials``, not a resource) and
    Istio CNI chaining (``proxyRedirectionMechanism`` is typed out of
    the AVM IstioComponents schema).

    Returns the flattened Bicep output dict (``clusterName``,
    ``clusterResourceId``, ``oidcIssuerUrl``, ``clusterPrincipalId``).
    Returns an empty dict when ``dry_run`` is True.
    """
    header = "Previewing" if dry_run else "Deploying"
    console.print(f"\n[bold]{header} AKS Automatic cluster via Bicep...[/bold]")
    console.print(
        "  [info]Cluster is declared in infra/aks.bicep via the AVM managed-cluster module.[/info]"
    )
    aks_outputs = run_bicep_deployment(
        template_path=str(INFRA_AKS_BICEP),
        parameters={
            "clusterName": config.cluster_name,
            "location": config.location,
        },
        resource_group=config.resource_group,
        deployment_name=f"spi-aks-{config.env or 'base'}",
        what_if=dry_run,
    )

    if dry_run:
        display_result("AKS Bicep what-if preview complete")
        return {}

    display_result(f"AKS Automatic cluster {config.cluster_name} ready")

    console.print("\n[bold]Fetching cluster credentials...[/bold]")
    run_command(
        [
            "az",
            "aks",
            "get-credentials",
            "--resource-group",
            config.resource_group,
            "--name",
            config.cluster_name,
            "--overwrite-existing",
        ],
        description="Merge kubeconfig",
    )

    # AKS Automatic kubeconfigs default to the `azurecli` exec plugin
    # (kubelogin binary). Rewrite to use the `az` CLI's token cache directly
    # so every kubectl call reuses already-acquired tokens instead of
    # spawning kubelogin and re-running the OIDC exchange (which can fail
    # with AADSTS700024 once the GitHub OIDC JWT has expired mid-job).
    run_command(
        ["kubelogin", "convert-kubeconfig", "-l", "azurecli"],
        description="Convert kubeconfig to azurecli auth",
    )

    # AVM v0.13.0 types proxyRedirectionMechanism out of IstioComponents;
    # enable CNI chaining imperatively. Idempotent. CNI chaining avoids
    # the NET_ADMIN capability requirement that the default Istio sidecar
    # init container needs.
    _ensure_istio_cni_chaining(config)

    # AKS Automatic enforces Azure RBAC for Kubernetes authorization with
    # local accounts disabled, so the deploying principal needs an
    # explicit cluster-admin role assignment before kubectl can create
    # namespaces. Role-assignment propagation to AKS typically takes
    # 2-3 minutes; this step blocks until the permission becomes active.
    _grant_deployer_cluster_admin(config, aks_outputs.get("clusterResourceId", ""))

    # Deployment Safeguards are not relaxed here. On the Automatic SKU
    # they are enforced via a non-bypassable ValidatingAdmissionPolicy
    # that cannot be tuned via `az aks update --safeguards-level`; the
    # local Helm chart (software/charts/osdu-spi-service) is written to
    # satisfy the policy instead.

    return aks_outputs


def _grant_deployer_cluster_admin(config: Config, cluster_resource_id: str):
    """Grant the signed-in principal cluster-admin on the AKS cluster and wait for propagation.

    Required because AKS Automatic enforces Azure RBAC for Kubernetes and
    disables local accounts. Without this role, ``kubectl`` operations
    run by the deployer fail with ``User does not have access to the
    resource in Azure``.
    """
    if not cluster_resource_id:
        console.print("[warning]Cluster resource ID unavailable; skipping RBAC grant.[/warning]")
        return

    account_result = run_command(
        ["az", "account", "show", "--output", "json"],
        description="Resolve signed-in principal",
        display=False,
    )
    account = json.loads(account_result.stdout)
    principal_type = "User" if account.get("user", {}).get("type") == "user" else "ServicePrincipal"
    # Honor SPI_DEPLOYER_OID when set (CI passes it from a step that runs
    # while the GitHub OIDC JWT is still within its 5-minute lifetime).
    # `az ad` commands bypass the MSAL access-token cache and re-do the
    # federated exchange, which fails ~20 min into spi up with AADSTS700024.
    user_oid = os.environ.get("SPI_DEPLOYER_OID", "").strip()
    if not user_oid:
        if principal_type == "ServicePrincipal":
            # `az ad signed-in-user show` calls Graph `/me`, which is
            # delegated-flow-only. For SP auth, look up the SP by its appId
            # (returned in account.user.name) to get its objectId.
            app_id = account.get("user", {}).get("name", "")
            user_oid = run_command(
                ["az", "ad", "sp", "show", "--id", app_id, "--query", "id", "--output", "tsv"],
                description="Get deployer object ID (service principal)",
                display=False,
            ).stdout.strip()
        else:
            user_oid = run_command(
                ["az", "ad", "signed-in-user", "show", "--query", "id", "--output", "tsv"],
                description="Get deployer object ID",
                display=False,
            ).stdout.strip()

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
            user_oid,
            "--assignee-principal-type",
            principal_type,
            "--scope",
            cluster_resource_id,
            "--output",
            "none",
        ],
        description=f"Assign cluster-admin to {user_oid[:8]}...",
        # Idempotent: on re-deploys the assignment already exists and the
        # CLI returns non-zero. We tolerate that and fall through to the
        # ARM-side verification below, which distinguishes a real failure
        # from a benign "already exists".
        check=False,
    )
    _verify_role_assignment_recorded(user_oid, cluster_resource_id)
    _wait_for_cluster_rbac()


def _verify_role_assignment_recorded(user_oid: str, cluster_resource_id: str):
    """Confirm the cluster-admin assignment is visible in ARM before polling propagation.

    The preceding ``az role assignment create`` runs with ``check=False`` so a
    silent failure would otherwise be indistinguishable from slow AKS
    authorization-plane propagation. ARM listings respond within seconds and
    are independent of AKS-plane caching.
    """
    # Verify via raw ARM: every `az role assignment` subcommand resolves
    # principals through Microsoft Graph, which Conditional Access token
    # protection can block (AADSTS530084) even when ARM access is fine.
    # b1ff04bb-... is the built-in "Azure Kubernetes Service RBAC Cluster
    # Admin" role definition ID.
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
    """Enable Istio CNI chaining (not expressible in AVM managed-cluster v0.13.0)."""
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


# ─────────────────────────────────────────────────────────────
# Key Vault soft-delete pre-check (imperative; ARM cannot branch on
# list-deleted queries)
# ─────────────────────────────────────────────────────────────


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


# ─────────────────────────────────────────────────────────────
# Bicep parameter assembly and output reshaping
# ─────────────────────────────────────────────────────────────


def _build_bicep_params(config: Config, oidc_issuer: str) -> Dict[str, Any]:
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
        # DNS-mode only; both are empty strings in ip/azure modes and the
        # conditional modules in main.bicep no-op when dnsZoneName is empty.
        "dnsZoneName": config.dns_zone,
        "dnsZoneResourceGroup": config.dns_zone_rg,
        # Deployer OID -- used by rbac.bicep to grant KV Secrets Officer
        # so Phase 6 (`az keyvault secret set`) succeeds. Empty string is
        # fine for local users with RG Owner. The principal type must match
        # the actual identity or ARM rejects the role assignment with
        # UnmatchedPrincipalType.
        "deployerPrincipalId": os.environ.get("SPI_DEPLOYER_OID", "").strip(),
        "deployerPrincipalType": _deployer_principal_type(),
    }


def _deployer_principal_type() -> str:
    """Principal type of the logged-in az identity (User vs ServicePrincipal)."""
    override = os.environ.get("SPI_DEPLOYER_TYPE", "").strip()
    if override in ("User", "ServicePrincipal"):
        return override
    result = run_command(
        ["az", "account", "show", "--query", "user.type", "--output", "tsv"],
        description="Get deployer principal type",
        check=False,
        display=False,
    )
    return "User" if (result.stdout or "").strip() == "user" else "ServicePrincipal"


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
        # DNS-mode outputs (empty strings when ingress mode != dns).
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


# ─────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────


def provision_azure_infra(config: Config, dry_run: bool = False) -> Dict[str, Any]:
    """Provision all Azure PaaS resources. Returns infra_outputs for K8s bootstrap.

    Order:
      1. Verify Azure login; capture tenant/subscription IDs.
      2. Create resource group (imperative; required by ``az deployment
         group what-if`` too, so always runs).
      3. Deploy AKS Automatic via ``infra/aks.bicep`` (what-if in dry-run;
         returns ``oidcIssuerUrl`` for main.bicep).
      4. Recover soft-deleted Key Vault if present (skipped in dry-run).
      5. Deploy the main Bicep template (or run what-if preview if
         ``dry_run`` is True). This deploys all PaaS resources AND
         populates Key Vault metadata secrets (tenant-id, endpoints,
         Cosmos primary keys via ``listKeys()``) declaratively.
    """
    outputs: Dict[str, Any] = {}

    console.print("\n[bold]Verifying Azure login...[/bold]")
    result = run_command(
        ["az", "account", "show", "--output", "json"],
        description="Check Azure subscription",
    )
    account = json.loads(result.stdout)
    outputs["tenant_id"] = account.get("tenantId", "")
    outputs["subscription_id"] = account.get("id", "")
    console.print(
        f"  [info]Subscription: {account.get('name', 'unknown')} ({account.get('id', '')})[/info]"
    )

    create_resource_group(config)

    # AKS Bicep deploy returns the OIDC issuer URL directly. In dry-run
    # we run what-if on aks.bicep (returning an empty dict) and pass an
    # empty issuer so identity.bicep omits federated credentials from
    # the main.bicep preview.
    aks_outputs = create_aks_automatic(config, dry_run=dry_run)
    oidc_issuer = aks_outputs.get("oidcIssuerUrl", "")

    if not dry_run:
        _recover_soft_deleted_keyvault(config)

    header = "Previewing" if dry_run else "Deploying"
    console.print(f"\n[bold]{header} Azure PaaS resources via Bicep...[/bold]")
    console.print(
        "  [info]Identity, KeyVault, ACR, CosmosDB, Service Bus, Storage, "
        "and RBAC role assignments are declared in infra/main.bicep.[/info]"
    )
    bicep_params = _build_bicep_params(config, oidc_issuer)
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
