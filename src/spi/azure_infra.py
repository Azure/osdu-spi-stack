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
  - AKS Automatic is declared in Bicep at ``infra/aks.bicep``. Two
    post-deploy imperative steps remain:
    ``az aks get-credentials`` (kubeconfig merge; not a resource) and
    ``az aks mesh enable-istio-cni`` (the resource provider rejects
    ``proxyRedirectionMechanism`` at create time).
  - Key Vault soft-delete recovery is imperative pre-check (ARM cannot
    branch on a list-deleted query).
  - Everything else (Managed Identity, federated credentials, Key Vault
    creation + metadata secrets, ACR, CosmosDB Gremlin + SQL, Service Bus +
    topics/subs, Storage + containers/tables, RBAC role assignments) is
    declared in Bicep at ``infra/main.bicep`` and deployed with
    ``az deployment group create``. Local auth is disabled on the Cosmos and
    Service Bus accounts (ADR-027), so their key/connection secrets are
    ``DISABLED`` placeholders and services authenticate with Workload
    Identity.
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

import base64
import json
import os
import time
from typing import Any, Dict, Optional, Tuple

import typer

from .bicep import run_bicep_deployment
from .config import (
    RG_AKS_MODE_TAG,
    RG_APPLICATION_INSIGHTS_TAG,
    RG_SUFFIX_TAG,
    AksMode,
    Config,
)
from .console import console, display_result
from .paths import INFRA_ROOT
from .shell import run_command

INFRA_MAIN_BICEP = INFRA_ROOT / "main.bicep"
INFRA_AKS_BICEP = INFRA_ROOT / "aks.bicep"
INFRA_AKS_BASE_BICEP = INFRA_ROOT / "aks-base.bicep"

# System pool VM size. Kept here rather than only in Bicep so the CLI can
# resolve the zones this exact size can actually use in the target region.
SYSTEM_POOL_VM_SIZE = "Standard_D4lds_v5"


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


def create_resource_group(
    config: Config,
    persist_application_insights: bool = True,
    persist_aks_mode: bool = True,
) -> None:
    console.print("\n[bold]Creating resource group...[/bold]")
    exists = run_command(
        ["az", "group", "exists", "--name", config.resource_group],
        description=f"Check resource group exists: {config.resource_group}",
        display=False,
        check=False,
    )
    if exists.returncode == 0 and exists.stdout.strip().lower() == "true":
        display_result(f"Resource group {config.resource_group} ready")
        return

    # `az group create --tags` replaces the entire tag set when the group
    # already exists, so only call create for a genuinely new resource group.
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
    tags = []
    if persist_application_insights:
        tags.append(f"{RG_APPLICATION_INSIGHTS_TAG}={str(config.application_insights).lower()}")
    if persist_aks_mode:
        tags.append(f"{RG_AKS_MODE_TAG}={config.aks_mode.value}")
    if config.name_suffix:
        tags.append(f"{RG_SUFFIX_TAG}={config.name_suffix}")
    if tags:
        cmd.extend(["--tags", *tags])
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


def read_rg_application_insights_tag(resource_group: str) -> "bool | None":
    """Read the persisted Application Insights mode from the resource group."""
    result = run_command(
        [
            "az",
            "group",
            "show",
            "--name",
            resource_group,
            "--query",
            f'tags."{RG_APPLICATION_INSIGHTS_TAG}"',
            "--output",
            "tsv",
        ],
        description=f"Read Application Insights tag from resource group: {resource_group}",
        display=False,
        check=False,
    )
    if result.returncode != 0:
        error = f"{result.stdout}\n{result.stderr}".lower()
        if "resourcegroupnotfound" in error:
            return None
        raise RuntimeError(
            f"Unable to read the Application Insights mode from "
            f"{resource_group}: {result.stderr.strip() or result.stdout.strip()}"
        )
    value = result.stdout.strip().lower()
    if not value or value == "none":
        return None
    if value not in {"true", "false"}:
        raise RuntimeError(
            f"Resource group {resource_group} has invalid "
            f"{RG_APPLICATION_INSIGHTS_TAG} tag value {value!r}."
        )
    return value == "true"


def write_rg_application_insights_tag(resource_group: str, enabled: bool) -> None:
    """Persist the Application Insights mode without disturbing other tags."""
    run_command(
        [
            "az",
            "group",
            "update",
            "--name",
            resource_group,
            "--set",
            f"tags.{RG_APPLICATION_INSIGHTS_TAG}={str(enabled).lower()}",
            "--output",
            "none",
        ],
        description=(
            f"Persist {RG_APPLICATION_INSIGHTS_TAG} tag on resource group: {resource_group}"
        ),
    )


def read_rg_aks_mode_tag(resource_group: str) -> "AksMode | None":
    """Read the persisted AKS deployment mode from the resource group."""
    result = run_command(
        [
            "az",
            "group",
            "show",
            "--name",
            resource_group,
            "--query",
            f'tags."{RG_AKS_MODE_TAG}"',
            "--output",
            "tsv",
        ],
        description=f"Read AKS mode from resource group: {resource_group}",
        display=False,
        check=False,
    )
    if result.returncode != 0:
        error = f"{result.stdout}\n{result.stderr}".lower()
        if "resourcegroupnotfound" in error:
            return None
        raise RuntimeError(
            f"Unable to read the AKS mode from {resource_group}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    value = result.stdout.strip().lower()
    if not value or value == "none":
        return None
    try:
        return AksMode(value)
    except ValueError as exc:
        raise RuntimeError(
            f"Resource group {resource_group} has invalid {RG_AKS_MODE_TAG} tag value {value!r}."
        ) from exc


def write_rg_aks_mode_tag(resource_group: str, mode: AksMode) -> None:
    """Persist the AKS deployment mode without disturbing other tags."""
    run_command(
        [
            "az",
            "group",
            "update",
            "--name",
            resource_group,
            "--set",
            f"tags.{RG_AKS_MODE_TAG}={mode.value}",
            "--output",
            "none",
        ],
        description=f"Persist {RG_AKS_MODE_TAG} tag on resource group: {resource_group}",
    )


def _aks_mode_from_cluster(cluster: Dict[str, Any]) -> AksMode:
    """Classify an existing cluster and reject unsupported Base shapes."""
    sku_name = str((cluster.get("sku") or {}).get("name", "")).lower()
    if sku_name == "automatic":
        return AksMode.AUTOMATIC
    if sku_name == "base":
        node_provisioning = str(
            (cluster.get("nodeProvisioningProfile") or {}).get("mode", "")
        ).lower()
        if node_provisioning != "auto":
            raise RuntimeError(
                "Existing AKS cluster uses the Base SKU without Node Autoprovisioning. "
                "SPI can only adopt its managed Base + NAP topology."
            )
        return AksMode.BASE
    raise RuntimeError(f"Existing AKS cluster has unsupported SKU {sku_name or '<missing>'!r}.")


def detect_existing_aks_mode(resource_group: str, cluster_name: str) -> "AksMode | None":
    """Return the actual mode of an existing SPI cluster, or None if absent."""
    result = run_command(
        [
            "az",
            "aks",
            "show",
            "--resource-group",
            resource_group,
            "--name",
            cluster_name,
            "--output",
            "json",
        ],
        description=f"Detect existing AKS mode: {cluster_name}",
        display=False,
        check=False,
    )
    if result.returncode != 0:
        error = f"{result.stdout}\n{result.stderr}".lower()
        if (
            "resourcenotfound" in error
            or "resourcegroupnotfound" in error
            or "managedclusternotfound" in error
        ):
            return None
        raise RuntimeError(
            f"Unable to inspect AKS cluster {cluster_name}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        cluster = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Unable to parse AKS cluster {cluster_name}.") from exc
    return _aks_mode_from_cluster(cluster)


def _resource_exists(
    resource_group: str,
    name: str,
    resource_type: str,
    description: str,
) -> bool:
    """Probe one Azure resource while distinguishing absence from CLI failure."""
    result = run_command(
        [
            "az",
            "resource",
            "show",
            "--resource-group",
            resource_group,
            "--name",
            name,
            "--resource-type",
            resource_type,
            "--output",
            "none",
        ],
        description=description,
        display=False,
        check=False,
    )
    if result.returncode == 0:
        return True

    error = f"{result.stdout}\n{result.stderr}".lower()
    if "resourcenotfound" in error or "resourcegroupnotfound" in error:
        return False
    raise RuntimeError(
        f"Unable to determine whether {name} exists in {resource_group}: "
        f"{result.stderr.strip() or result.stdout.strip()}"
    )


def detect_existing_application_insights(resource_group: str, env: str) -> bool:
    """Return True when the environment's legacy App Insights component exists."""
    name = f"osdu-{env or 'base'}-insights"
    return _resource_exists(
        resource_group,
        name,
        "Microsoft.Insights/components",
        f"Detect existing Application Insights: {name}",
    )


def detect_existing_log_analytics(resource_group: str, env: str) -> bool:
    """Return True when the environment's legacy telemetry workspace exists."""
    name = f"osdu-{env or 'base'}-logs"
    return _resource_exists(
        resource_group,
        name,
        "Microsoft.OperationalInsights/workspaces",
        f"Detect existing Log Analytics workspace: {name}",
    )


def read_deployed_application_insights_mode(
    resource_group: str,
    env: str,
) -> "bool | None":
    """Read telemetry intent from the most recent main Bicep deployment."""
    deployment_name = f"spi-{env or 'base'}"
    result = run_command(
        [
            "az",
            "deployment",
            "group",
            "show",
            "--resource-group",
            resource_group,
            "--name",
            deployment_name,
            "--query",
            "properties.parameters",
            "--output",
            "json",
        ],
        description=f"Read telemetry mode from deployment: {deployment_name}",
        display=False,
        check=False,
    )
    if result.returncode != 0:
        error = f"{result.stdout}\n{result.stderr}".lower()
        if "deploymentnotfound" in error:
            return None
        raise RuntimeError(
            f"Unable to read deployment {deployment_name} in {resource_group}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    try:
        parameters = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Unable to parse deployment {deployment_name} parameters.") from exc
    if not isinstance(parameters, dict):
        return None

    enabled_parameter = parameters.get("enableApplicationInsights")
    if isinstance(enabled_parameter, dict) and "value" in enabled_parameter:
        value = enabled_parameter["value"]
        if isinstance(value, bool):
            return value
        if str(value).lower() in {"true", "false"}:
            return str(value).lower() == "true"
        raise RuntimeError(
            f"Deployment {deployment_name} has invalid enableApplicationInsights value {value!r}."
        )

    name_parameter = parameters.get("appInsightsName")
    if isinstance(name_parameter, dict) and "value" in name_parameter:
        value = name_parameter["value"]
        return None if value is None else bool(str(value).strip())
    return None


def resource_group_has_resources(resource_group: str) -> bool:
    """Return whether an existing resource group contains deployed resources."""
    result = run_command(
        [
            "az",
            "resource",
            "list",
            "--resource-group",
            resource_group,
            "--query",
            "[0].id",
            "--output",
            "tsv",
        ],
        description=f"Check resource group contents: {resource_group}",
        display=False,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Unable to inspect resource group {resource_group}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    value = result.stdout.strip()
    return bool(value and value.lower() != "none")


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


def create_aks(
    config: Config,
    deployer_principal_id: str,
    deployer_principal_type: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Create the selected AKS topology plus managed Istio via Bicep.

    Automatic uses ``infra/aks.bicep`` and is the default. Base + Node
    Autoprovisioning uses ``infra/aks-base.bicep``. Two imperative post-deploy
    steps remain:
    kubeconfig merge (``az aks get-credentials``, not a resource) and
    Istio CNI chaining (``proxyRedirectionMechanism`` is rejected by the
    resource provider at create time).

    Returns the flattened Bicep output dict (``clusterName``,
    ``clusterResourceId``, ``oidcIssuerUrl``, ``clusterPrincipalId``).
    Returns an empty dict when ``dry_run`` is True.
    """
    header = "Previewing" if dry_run else "Deploying"
    mode_label = (
        "Automatic 1.36" if config.aks_mode == AksMode.AUTOMATIC else "Base + Node Autoprovisioning"
    )
    template_path = (
        INFRA_AKS_BICEP if config.aks_mode == AksMode.AUTOMATIC else INFRA_AKS_BASE_BICEP
    )
    console.print(f"\n[bold]{header} AKS {mode_label} cluster via Bicep...[/bold]")
    console.print(
        f"  [info]Cluster is declared in {template_path.name} as a managedClusters resource.[/info]"
    )
    aks_outputs = None if dry_run else _existing_aks_outputs(config)
    if aks_outputs:
        display_result(f"AKS cluster {config.cluster_name} already exists")
    else:
        zones = _resolve_system_pool_zones(config)
        aks_outputs = run_bicep_deployment(
            template_path=str(template_path),
            parameters={
                "clusterName": config.cluster_name,
                "location": config.location,
                "systemPoolVmSize": SYSTEM_POOL_VM_SIZE,
                "availabilityZones": zones,
            },
            resource_group=config.resource_group,
            deployment_name=f"spi-aks-{config.env or 'base'}",
            what_if=dry_run,
        )

        if dry_run:
            display_result("AKS Bicep what-if preview complete")
            return {}

        display_result(f"AKS cluster {config.cluster_name} ready")
        # Some identityProfile values can be absent from the deployment output
        # until the resource provider finishes population. Read the live cluster
        # before PaaS RBAC so kubelet AcrPull is never silently skipped.
        live_outputs = _existing_aks_outputs(config)
        if live_outputs:
            aks_outputs.update(live_outputs)

    if not aks_outputs.get("istioRevision"):
        raise RuntimeError(
            f"AKS cluster {config.cluster_name} did not report its managed Istio revision."
        )
    if not aks_outputs.get("kubeletIdentityObjectId"):
        raise RuntimeError(
            f"AKS cluster {config.cluster_name} did not report its kubelet identity. "
            "Refusing to continue because the required AcrPull grant would be skipped."
        )

    _ensure_cluster_identity_network_contributor(config, aks_outputs)

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
    _pin_kubeconfig_tenant()

    # The resource provider rejects proxyRedirectionMechanism at create
    # time; enable CNI chaining imperatively. Idempotent. CNI chaining
    # avoids the NET_ADMIN capability requirement that the default Istio
    # sidecar init container needs.
    _ensure_istio_cni_chaining(config)

    # The cluster enforces Azure RBAC for Kubernetes authorization
    # (aadProfile.enableAzureRBAC) with local accounts disabled, so the
    # deploying principal needs an explicit cluster-admin role assignment
    # before kubectl can create namespaces. Role-assignment propagation to
    # AKS typically takes 2-3 minutes; this step blocks until active.
    _grant_deployer_cluster_admin(
        config,
        aks_outputs.get("clusterResourceId", ""),
        deployer_principal_id,
        deployer_principal_type,
    )

    return aks_outputs


def _pin_kubeconfig_tenant() -> None:
    """Prevent inherited shell settings from selecting the wrong Azure tenant."""
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
    if not account_tenant or not kubeconfig_user:
        return
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
        description="Pin tenant in kubeconfig exec environment",
        display=False,
    )


NETWORK_CONTRIBUTOR_ROLE_ID = "4d97b98b-1d4f-4787-a291-c67834d212e7"


def _ensure_cluster_identity_network_contributor(
    config: Config,
    aks_outputs: Dict[str, Any],
) -> None:
    """Guarantee the cluster identity can read and join the BYO VNet.

    Node autoprovisioning resolves the node subnet as the cluster's
    control-plane identity. Bicep declares the grant, but a role assignment
    created against a just-created managed identity can be dropped after the
    deployment reports success, and the only symptom is every AKSNodeClass
    reporting SubnetsReady=False with a 403, long after `spi up` exits. ARM is
    the source of truth, so verify and repair here.
    """
    principal_id = aks_outputs.get("clusterPrincipalId", "")
    if not principal_id:
        raise RuntimeError(
            f"AKS cluster {config.cluster_name} did not report its control-plane identity, "
            "so the VNet role assignment node autoprovisioning needs cannot be verified."
        )

    vnet_id = (
        f"/subscriptions/{_subscription_id()}/resourceGroups/{config.resource_group}"
        f"/providers/Microsoft.Network/virtualNetworks/{config.cluster_name}-vnet"
    )
    result = run_command(
        [
            "az",
            "rest",
            "--method",
            "get",
            "--url",
            (
                f"https://management.azure.com{vnet_id}"
                "/providers/Microsoft.Authorization/roleAssignments"
                "?api-version=2022-04-01"
            ),
            "--query",
            (
                f"length(value[?properties.principalId=='{principal_id}' && "
                f"contains(properties.roleDefinitionId, '{NETWORK_CONTRIBUTOR_ROLE_ID}')])"
            ),
            "--output",
            "tsv",
        ],
        description="Verify cluster identity can read the node subnet",
        display=False,
        check=False,
    )
    try:
        recorded = int((result.stdout or "0").strip())
    except ValueError:
        recorded = 0
    if result.returncode == 0 and recorded > 0:
        return

    console.print(
        "[warning]Cluster identity is missing Network Contributor on the VNet; "
        "node autoprovisioning cannot resolve the subnet. Reapplying.[/warning]"
    )
    run_command(
        [
            "az",
            "role",
            "assignment",
            "create",
            "--role",
            "Network Contributor",
            "--assignee-object-id",
            principal_id,
            "--assignee-principal-type",
            "ServicePrincipal",
            "--scope",
            vnet_id,
            "--output",
            "none",
        ],
        description="Grant cluster identity Network Contributor on the VNet",
        check=False,
    )
    display_result("Cluster identity can resolve the node subnet")


def _subscription_id() -> str:
    """Return the subscription the CLI is currently signed in to."""
    return run_command(
        ["az", "account", "show", "--query", "id", "--output", "tsv"],
        description="Resolve subscription id",
        display=False,
    ).stdout.strip()


def _resolve_system_pool_zones(config: Config) -> list:
    """Return the availability zones the system pool can actually use.

    Zone availability is per subscription, not just per region: a size can be
    published in three zones while one of them is restricted for this
    subscription. Passing a restricted zone fails with
    AvailabilityZoneNotSupported, and the Automatic SKU separately rejects a
    reduced zone set, so the usable set has to be resolved before deploying.
    """
    result = run_command(
        [
            "az",
            "vm",
            "list-skus",
            "--location",
            config.location,
            "--size",
            SYSTEM_POOL_VM_SIZE,
            "--resource-type",
            "virtualMachines",
            "--output",
            "json",
        ],
        description=f"Resolve system pool zones in {config.location}",
        display=False,
        check=False,
    )
    published: list = []
    restricted: set = set()
    if result.returncode == 0:
        for sku in json.loads(result.stdout or "[]"):
            if sku.get("name") != SYSTEM_POOL_VM_SIZE:
                continue
            for info in sku.get("locationInfo") or []:
                published.extend(info.get("zones") or [])
            for restriction in sku.get("restrictions") or []:
                if restriction.get("type") == "Zone":
                    restricted.update((restriction.get("restrictionInfo") or {}).get("zones") or [])

    if not published:
        raise RuntimeError(
            f"{SYSTEM_POOL_VM_SIZE} is not offered in {config.location}. "
            "Choose a region that offers it."
        )

    usable = sorted(set(published) - restricted)
    if not usable:
        raise RuntimeError(
            f"{SYSTEM_POOL_VM_SIZE} has no usable availability zone in {config.location} "
            "for this subscription."
        )

    # Automatic validates the system pool against the region's recommended zone
    # set and refuses a reduced list, so a restricted zone is fatal there while
    # Base can simply avoid it.
    if config.aks_mode == AksMode.AUTOMATIC and len(usable) < len(set(published)):
        raise RuntimeError(
            f"AKS Automatic requires every availability zone in {config.location}, but "
            f"zone(s) {', '.join(sorted(restricted))} are restricted for "
            f"{SYSTEM_POOL_VM_SIZE} in this subscription. Deploy Automatic in another "
            "region, or use --aks-mode base here."
        )

    console.print(f"  [info]System pool availability zones: {', '.join(usable)}[/info]")
    return usable


def _existing_aks_outputs(config: Config) -> "Dict[str, Any] | None":
    """Return outputs for an already-ready AKS cluster, or None if absent."""
    result = run_command(
        [
            "az",
            "aks",
            "show",
            "--resource-group",
            config.resource_group,
            "--name",
            config.cluster_name,
            "--output",
            "json",
        ],
        description=f"Check existing AKS cluster: {config.cluster_name}",
        display=False,
        check=False,
    )
    if result.returncode != 0:
        return None

    cluster = json.loads(result.stdout or "{}")
    location = (cluster.get("location") or "").lower()
    if location and location != config.location.lower():
        raise RuntimeError(
            f"AKS cluster {config.cluster_name} already exists in {location}, "
            f"but this run targets {config.location}. Delete the resource group or use "
            "the existing location."
        )

    state = cluster.get("provisioningState")
    if state != "Succeeded":
        console.print(
            f"[warning]Existing AKS cluster {config.cluster_name} is {state}; "
            "submitting Bicep deployment to reconcile it.[/warning]"
        )
        return None

    actual_mode = _aks_mode_from_cluster(cluster)
    if actual_mode != config.aks_mode:
        raise RuntimeError(
            f"AKS cluster {config.cluster_name} is {actual_mode.value}, but this run "
            f"requested {config.aks_mode.value}. AKS mode cannot be changed in place; "
            "use the existing mode or create a new environment."
        )

    identities = cluster.get("identity", {}).get("userAssignedIdentities", {}) or {}
    principal_id = ""
    if identities:
        principal_id = next(iter(identities.values())).get("principalId", "")

    # Kubelet (node) identity object ID. The fresh-deploy path receives this from the
    # aks.bicep `kubeletIdentityObjectId` output; on an idempotent re-run against an
    # existing cluster it must be read here too. Without it, `_build_bicep_params`
    # passes an empty value and the kubelet AcrPull role assignment is silently
    # skipped, so nodes cannot pull images from the SPI ACR on re-runs.
    identity_profile = cluster.get("identityProfile") or {}
    kubelet_identity = identity_profile.get("kubeletidentity") or {}
    istio_revisions = ((cluster.get("serviceMeshProfile") or {}).get("istio") or {}).get(
        "revisions"
    ) or []

    return {
        "clusterName": cluster.get("name", config.cluster_name),
        "clusterResourceId": cluster.get("id", ""),
        "oidcIssuerUrl": cluster.get("oidcIssuerProfile", {}).get("issuerUrl", ""),
        "clusterPrincipalId": principal_id,
        "kubeletIdentityObjectId": kubelet_identity.get("objectId", ""),
        "istioRevision": istio_revisions[0] if istio_revisions else "",
    }


def _grant_deployer_cluster_admin(
    config: Config,
    cluster_resource_id: str,
    deployer_principal_id: str,
    deployer_principal_type: str,
):
    """Grant the signed-in principal cluster-admin on the AKS cluster and wait for propagation.

    Required because the cluster enforces Azure RBAC for Kubernetes and
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
        # Idempotent: on re-deploys the assignment already exists and the
        # CLI returns non-zero. We tolerate that and fall through to the
        # ARM-side verification below, which distinguishes a real failure
        # from a benign "already exists".
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
                f"contains(properties.roleDefinitionId, "
                f"'{aks_rbac_cluster_admin_role_id}')])"
            ),
            "--output",
            "tsv",
        ],
        description="Verify cluster-admin assignment exists",
        check=False,
        display=False,
    )
    assignment_count = 0
    if result.returncode == 0:
        try:
            assignment_count = int((result.stdout or "0").strip())
        except ValueError:
            assignment_count = 0
    if result.returncode != 0 or assignment_count < 1:
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
    """Enable Istio CNI chaining after AKS create."""
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
    previous_dynamic_install = os.environ.get("AZURE_EXTENSION_USE_DYNAMIC_INSTALL")
    os.environ["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] = "yes_without_prompt"
    try:
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
    finally:
        if previous_dynamic_install is None:
            os.environ.pop("AZURE_EXTENSION_USE_DYNAMIC_INSTALL", None)
        else:
            os.environ["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] = previous_dynamic_install
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


def _build_bicep_params(
    config: Config,
    oidc_issuer: str,
    deployer_principal_id: str,
    deployer_principal_type: str,
    kubelet_identity_object_id: str = "",
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
        # DNS-mode only; both are empty strings in ip/azure modes and the
        # conditional modules in main.bicep no-op when dnsZoneName is empty.
        "dnsZoneName": config.dns_zone,
        "dnsZoneResourceGroup": config.dns_zone_rg,
        # Used by rbac.bicep to grant KV Secrets Officer so Phase 6
        # (`az keyvault secret set`) succeeds against RBAC-enabled vaults.
        "deployerPrincipalId": deployer_principal_id,
        "deployerPrincipalType": deployer_principal_type,
        # AKS kubelet identity object ID (from the AKS deployment output).
        # rbac.bicep grants it AcrPull so nodes can pull images from the SPI
        # ACR (required for custom OSDU service images). Empty in dry-run.
        "kubeletIdentityObjectId": kubelet_identity_object_id,
        "enableApplicationInsights": config.application_insights,
        # The resources are optional, but services always receive either the
        # real connection values or a disabled/dummy fallback in osdu-config.
        "appInsightsName": (
            f"osdu-{config.env or 'base'}-insights" if config.application_insights else ""
        ),
        "logAnalyticsName": (
            f"osdu-{config.env or 'base'}-logs" if config.application_insights else ""
        ),
    }


def _deployer_principal_type(account: Optional[Dict[str, Any]] = None) -> str:
    """Principal type of the logged-in az identity (User vs ServicePrincipal)."""
    override = (
        os.environ.get("SPI_DEPLOYER_PRINCIPAL_TYPE", "").strip()
        or os.environ.get("SPI_DEPLOYER_TYPE", "").strip()
    )
    if override in {"User", "ServicePrincipal"}:
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
        # DNS-mode outputs (empty strings when ingress mode != dns).
        "external_dns_client_id": bicep_outputs.get("externalDnsClientId", ""),
        "external_dns_principal_id": bicep_outputs.get("externalDnsPrincipalId", ""),
        # Application Insights (empty when not provisioned; deploy.py falls back
        # to a disabled/dummy connection string so core-lib-azure does not NPE).
        "app_insights_connection_string": bicep_outputs.get("appInsightsConnectionString", ""),
        "app_insights_instrumentation_key": bicep_outputs.get("appInsightsInstrumentationKey", ""),
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
         ``DISABLED`` key/connection placeholders) declaratively.
    """
    outputs: Dict[str, Any] = {}

    if account is None:
        account = _get_azure_account()
    outputs["tenant_id"] = account.get("tenantId", "")
    outputs["subscription_id"] = account.get("id", "")

    # Resolve before any Azure mutation. The same identity is used for AKS
    # cluster-admin and Key Vault Secrets Officer.
    if deployer_principal is None:
        deployer_principal = _resolve_deployer_principal(account)
    deployer_principal_id, deployer_principal_type = deployer_principal

    # What-if requires an RG but must not freeze an observability choice before
    # anything is deployed. A later real run can choose either mode.
    create_resource_group(
        config,
        persist_application_insights=not dry_run,
        persist_aks_mode=not dry_run,
    )

    # AKS Bicep deploy returns the OIDC issuer URL directly. In dry-run
    # we run what-if on aks.bicep (returning an empty dict) and pass an
    # empty issuer so identity.bicep omits federated credentials from
    # the main.bicep preview.
    aks_outputs = create_aks(
        config,
        deployer_principal_id,
        deployer_principal_type,
        dry_run=dry_run,
    )
    oidc_issuer = aks_outputs.get("oidcIssuerUrl", "")
    kubelet_identity_object_id = aks_outputs.get("kubeletIdentityObjectId", "")
    outputs["istio_revision"] = aks_outputs.get("istioRevision", "")

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
        kubelet_identity_object_id,
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
