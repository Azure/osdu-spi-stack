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

"""Deployment orchestrator.

Provisions Azure PaaS (via ``azure_infra.provision_azure_infra``), bootstraps
the cluster (namespaces, StorageClasses, Gateway API CRDs, ingress ConfigMap,
Workload Identity SAs, in-cluster seed secrets), activates GitOps via Flux,
and writes the KV runtime secrets that OSDU services read at startup.
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import typer

from . import __version__
from .azure_infra import provision_azure_infra
from .bicep import run_bicep_deployment
from .bootstrap import (
    create_istio_revision_configmap,
    create_storage_classes,
    ensure_namespaces,
    install_gateway_api_crds,
)
from .config import Config, IngressMode, Profile
from .console import console, display_result, display_yaml
from .deploy_record import upsert_deploy_record
from .images import (
    DEFAULT_IMAGE_BRANCH,
    ImageResolutionError,
    ResolvedImage,
    resolve_image_lock,
)
from .ingress import (
    create_ingress_config,
    discover_dns_zone,
    get_ingress_ip,
    resolve_post_deploy_inputs,
)
from .paths import INFRA_ROOT
from .pins import ServicePin, apply_image_lock, apply_schema_load_backfill, describe_pin, read_lock
from .secrets import ensure_secrets, get_or_create_seed
from .shell import kubectl_apply_yaml, prune_kube_context, run_command, run_process
from .templates import (
    istio_auth_resources,
    osdu_config_configmap,
    spi_init_values_configmap,
    workload_identity_sa,
)

GITREPO_NAME = "osdu-spi-stack-system"

INFRA_FLUX_BICEP = INFRA_ROOT / "flux.bicep"


def _resolve_aad_client_id(identity_client_id: str) -> str:
    """Return the appid services should mint service-to-service tokens for.

    Defaults to the OSDU UAMI client id (single-resource scope, dodges
    AADSTS28000); the AAD_CLIENT_ID host env var points at a separate app
    registration instead. The Istio audience list and the osdu-config
    ConfigMap must agree on this value, or service-to-service calls fail
    jwt_authn and reach the Spring filter without an x-app-id header.
    """
    return os.environ.get("AAD_CLIENT_ID", "").strip() or identity_client_id


def _create_osdu_config(config: Config, infra_outputs: dict) -> None:
    """Create the osdu-config ConfigMap and workload identity SAs."""
    console.print("\n[bold]Creating OSDU configuration...[/bold]")

    partition = config.primary_partition
    identity_client_id = infra_outputs.get("identity_client_id", "")
    aad_client_id = _resolve_aad_client_id(identity_client_id)
    yaml_content = osdu_config_configmap(
        domain="",  # Updated later by `spi info` once external IP is known
        primary_partition=partition,
        tenant_id=infra_outputs.get("tenant_id", ""),
        identity_client_id=identity_client_id,
        aad_client_id=aad_client_id,
        keyvault_uri=infra_outputs.get("keyvault_uri", ""),
        keyvault_name=config.keyvault_name,
        primary_cosmosdb_endpoint=infra_outputs.get(f"{partition}_cosmos_endpoint", ""),
        primary_storage_account_name=infra_outputs.get("common_storage_name", ""),
        primary_servicebus_namespace=infra_outputs.get(f"{partition}_sb_namespace", ""),
    )
    display_yaml(yaml_content, "ConfigMap: osdu-config")
    kubectl_apply_yaml(yaml_content, "apply osdu-config ConfigMap")
    display_result("osdu-config ConfigMap created")

    for ns in ["platform", "osdu"]:
        sa_yaml = workload_identity_sa(
            namespace=ns,
            client_id=infra_outputs.get("identity_client_id", ""),
            tenant_id=infra_outputs.get("tenant_id", ""),
        )
        kubectl_apply_yaml(sa_yaml, f"apply workload-identity-sa in {ns}")
    display_result("Workload Identity ServiceAccounts created")


def _create_istio_auth(config: Config, infra_outputs: dict) -> None:
    """Apply RequestAuthentication + PeerAuthentication + EnvoyFilter that
    project the JWT payload into x-app-id / x-user-id headers.
    Required because the Azure-provider OSDU service images read identity
    from those headers; without these resources every authenticated call is
    rejected with app-id= empty.
    """
    console.print("\n[bold]Applying OSDU Istio JWT projection...[/bold]")
    identity_client_id = infra_outputs.get("identity_client_id", "")
    yaml_content = istio_auth_resources(
        namespace="osdu",
        tenant_id=infra_outputs.get("tenant_id", ""),
        entra_client_id=identity_client_id,
        aad_client_id=_resolve_aad_client_id(identity_client_id),
    )
    display_yaml(yaml_content, "Istio: RequestAuthentication + PeerAuthentication + EnvoyFilter")
    kubectl_apply_yaml(yaml_content, "apply osdu Istio JWT projection")
    display_result(
        "Istio JWT projection applied (RequestAuthentication, PeerAuthentication, EnvoyFilter)"
    )


def _create_spi_init_values(config: Config) -> None:
    """Apply the spi-init-values ConfigMap that the osdu-spi-init HelmRelease
    consumes via valuesFrom. Must run before Flux reconciles the HelmRelease.
    """
    console.print("\n[bold]Creating SPI init values ConfigMap...[/bold]")
    yaml_content = spi_init_values_configmap(config.data_partitions)
    display_yaml(yaml_content, "ConfigMap: spi-init-values")
    kubectl_apply_yaml(yaml_content, "apply spi-init-values ConfigMap")
    display_result(
        f"spi-init-values ConfigMap created for partitions: {', '.join(config.data_partitions)}"
    )


def _resolve_image_lock(image_branch: str) -> dict[str, ResolvedImage]:
    """Resolve the current OSDU service images for the Flux image lock."""

    console.print("\n[bold]Resolving OSDU service images...[/bold]")
    try:
        resolved = resolve_image_lock(branch=image_branch)
    except ImageResolutionError as exc:
        console.print(f"[error]Unable to resolve OSDU service images: {exc}[/error]")
        raise

    for name, image in resolved.items():
        console.print(
            f"  [success]{name}[/success] -> {image.repository.split('/')[-1]}:{image.tag[:12]}"
        )

    return resolved


def _ensure_image_lock(
    config: Config,
    refresh_images: bool | None,
    image_branch: str,
    resolved_images: dict[str, ResolvedImage],
) -> dict[str, ServicePin]:
    """Create or refresh the core image lock according to the CLI intent."""

    if config.profile is not Profile.CORE:
        return {}

    if refresh_images is not True:
        existing_lock = read_lock(required=False)
        if existing_lock is not None:
            apply_schema_load_backfill(branch=image_branch)
            return {}
        if refresh_images is False:
            raise RuntimeError(
                "The core image lock is missing and --no-refresh-images was specified. "
                "Rerun with --refresh-images or omit the image option to create it."
            )
        resolved_images = _resolve_image_lock(image_branch)

    console.print("\n[bold]Updating OSDU image lock...[/bold]")
    pins = apply_image_lock(resolved_images, image_branch)
    display_result("osdu-image-lock ConfigMap updated")
    if pins:
        console.print(
            "[warning]Active service pins preserved: "
            + ", ".join(f"{name} ({describe_pin(pin)})" for name, pin in sorted(pins.items()))
            + "; release with 'spi service reset <service>'.[/warning]"
        )
    return pins


def _write_keyvault_bootstrap_secrets(
    config: Config,
    keyvault_name: str,
    storage_account_name: str,
    elastic_password: str,
    redis_password: str,
) -> None:
    """Write the secrets OSDU services read at startup.

    Elastic credentials are written per partition because the partition
    record resolves them by partition-prefixed name; every partition shares
    the one in-cluster Elasticsearch and therefore the same password.
    """
    console.print("\n[bold]Writing OSDU bootstrap secrets to Key Vault...[/bold]")
    tbl_endpoint = f"https://{storage_account_name}.table.core.windows.net/"
    # ECK's certificate SANs cover the .svc form only; the .svc.cluster.local
    # form fails hostname verification.
    elastic_endpoint = "https://elasticsearch-es-http.platform.svc:9200"
    redis_hostname = "platform-redis-master.platform.svc.cluster.local"

    secrets_to_write: list[tuple[str, str]] = [
        ("tbl-storage-endpoint", tbl_endpoint),
        ("redis-hostname", redis_hostname),
        ("redis-password", redis_password),
    ]
    for p in config.data_partitions:
        secrets_to_write.extend(
            [
                (f"{p}-elastic-endpoint", elastic_endpoint),
                (f"{p}-elastic-username", "elastic"),
                (f"{p}-elastic-password", elastic_password),
            ]
        )

    # The Secrets Officer assignment from rbac.bicep can take minutes to reach
    # the data plane; retry the first write on ForbiddenByRbac.
    deadline = time.time() + 300
    first = True
    for name, value in secrets_to_write:
        while True:
            result = run_process(
                [
                    "az",
                    "keyvault",
                    "secret",
                    "set",
                    "--vault-name",
                    keyvault_name,
                    "--name",
                    name,
                    "--value",
                    value,
                    "--output",
                    "none",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                break
            combined = (result.stderr or "") + (result.stdout or "")
            if "ForbiddenByRbac" in combined and first and time.time() < deadline:
                console.print(
                    "  [info]Key Vault role assignment not yet propagated; retrying in 30s...[/info]"
                )
                time.sleep(30)
                continue
            if result.stderr.strip():
                console.print(
                    f"[error]az keyvault secret set failed for {name}: {result.stderr.strip()}[/error]"
                )
            raise typer.Exit(code=1)
        first = False
        console.print(f"  [success]{name}[/success]")

    display_result(f"{len(secrets_to_write)} Key Vault secrets written")


def _read_git_repository() -> dict:
    result = run_process(
        [
            "kubectl",
            "get",
            "gitrepository",
            GITREPO_NAME,
            "-n",
            "osdu-flux",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or "kubectl failed"
        raise RuntimeError(f"Could not read GitRepository {GITREPO_NAME}: {detail}")
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse GitRepository {GITREPO_NAME}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"GitRepository {GITREPO_NAME} returned an invalid object")
    return parsed


def _wait_for_git_repository(timeout_seconds: int = 600) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        result = run_process(
            [
                "kubectl",
                "get",
                "gitrepository",
                GITREPO_NAME,
                "-n",
                "osdu-flux",
                "-o",
                "name",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0 and result.stdout.strip():
            return
        detail = (result.stderr or result.stdout or "").lower()
        if detail and not any(
            marker in detail
            for marker in ("not found", "no matches for kind", "doesn't have a resource type")
        ):
            raise RuntimeError(
                f"Could not discover GitRepository {GITREPO_NAME}: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        time.sleep(5)
    raise RuntimeError(
        f"GitRepository {GITREPO_NAME} did not appear within {timeout_seconds} seconds"
    )


def _resolved_revision(repository: dict, expected_ref: str, ref_field: str) -> str:
    actual_ref = repository.get("spec", {}).get("ref", {}).get(ref_field, "")
    if actual_ref != expected_ref:
        raise RuntimeError(
            f"GitRepository requested {ref_field} {actual_ref!r}, expected {expected_ref!r}"
        )

    ready = next(
        (
            condition
            for condition in repository.get("status", {}).get("conditions", [])
            if condition.get("type") == "Ready"
        ),
        {},
    )
    if ready.get("status") != "True":
        detail = ready.get("message") or ready.get("reason") or "not Ready"
        raise RuntimeError(f"GitRepository {GITREPO_NAME} is not Ready: {detail}")

    revision = repository.get("status", {}).get("artifact", {}).get("revision", "")
    prefix, separator, digest = revision.partition("@")
    # The qualified form is restricted to ref_field's own namespace so a tag
    # deployment cannot validate a same-named branch's artifact.
    namespace = "refs/tags/" if ref_field == "tag" else "refs/heads/"
    accepted_refs = {expected_ref, f"{namespace}{expected_ref}"}
    match = re.fullmatch(r"(?:sha1|sha256):([0-9a-fA-F]{40,64})", digest)
    if separator != "@" or prefix not in accepted_refs or match is None:
        raise RuntimeError(
            f"GitRepository artifact revision {revision!r} does not identify {expected_ref!r}"
        )
    return match.group(1).lower()


def _set_source_suspended(suspended: bool, *, check: bool = True) -> None:
    action = "Suspend" if suspended else "Resume"
    run_command(
        [
            "kubectl",
            "patch",
            "gitrepository",
            GITREPO_NAME,
            "-n",
            "osdu-flux",
            "--type=merge",
            "-p",
            json.dumps({"spec": {"suspend": suspended}}),
        ],
        description=f"{action} GitRepository",
        check=check,
    )


def _finalize_gitops_source(config: Config) -> None:
    """Verify the requested source revision, suspend it, and record the deploy."""

    expected_ref = config.repo_tag or config.repo_branch
    ref_field = "tag" if config.repo_tag else "branch"
    console.print(f"\n[bold]Verifying GitOps source {ref_field}: {expected_ref}...[/bold]")

    _wait_for_git_repository()
    try:
        _set_source_suspended(False)
        run_command(
            [
                "flux",
                "reconcile",
                "source",
                "git",
                GITREPO_NAME,
                "-n",
                "osdu-flux",
                "--timeout",
                "10m",
            ],
            description=f"Reconcile GitRepository ({expected_ref})",
        )
        resolved_commit = _resolved_revision(
            _read_git_repository(),
            expected_ref,
            ref_field,
        )
        _set_source_suspended(True)
    except Exception:
        _set_source_suspended(True, check=False)
        raise

    deployed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    resource_group_id = run_command(
        [
            "az",
            "group",
            "show",
            "--name",
            config.resource_group,
            "--query",
            "id",
            "--output",
            "tsv",
        ],
        description="Resolve resource group ID",
        display=False,
    ).stdout.strip()
    if not resource_group_id:
        raise RuntimeError(f"Could not resolve resource group ID for {config.resource_group}")
    run_command(
        [
            "az",
            "tag",
            "update",
            "--resource-id",
            resource_group_id,
            "--operation",
            "Merge",
            "--tags",
            f"spi-stack-version={expected_ref}",
            f"spi-deployed-utc={deployed_at}",
            "--output",
            "none",
        ],
        description="Record deployed stack version on resource group",
    )
    record = upsert_deploy_record(
        ref=expected_ref,
        resolved_commit=resolved_commit,
        deployed_at=deployed_at,
        cli_version=__version__,
        profile=config.profile.value,
        initial_maintenance=bool(config.repo_tag),
    )
    state = "maintenance enabled" if record.maintenance else "deployable after convergence"
    display_result(f"GitRepository pinned to {expected_ref} ({resolved_commit[:12]}); {state}.")


def deploy_azure(
    config: Config,
    dry_run: bool = False,
    refresh_images: bool | None = None,
    image_branch: str = DEFAULT_IMAGE_BRANCH,
    azure_account: Optional[Dict[str, Any]] = None,
    deployer_principal: Optional[Tuple[str, str]] = None,
) -> None:
    """Provision Azure infra, bootstrap Kubernetes, deploy via GitOps.

    Under ``dry_run`` only the Bicep what-if previews run; the Kubernetes
    bootstrap and GitOps activation are skipped.
    """
    resolved_images: dict[str, ResolvedImage] = {}
    # Only core consumes the image lock. Resolving before provisioning means a
    # registry failure stops the run before anything is half-configured.
    if refresh_images and not dry_run and config.profile is Profile.CORE:
        resolved_images = _resolve_image_lock(image_branch)

    # main.bicep's ExternalDNS identity and role modules need the zone's
    # name and resource group as parameters.
    if not dry_run and config.ingress_mode == IngressMode.DNS and not config.dns_zone:
        zone, rg = discover_dns_zone()
        config.dns_zone = zone
        config.dns_zone_rg = rg

    infra_outputs = provision_azure_infra(
        config,
        dry_run=dry_run,
        account=azure_account,
        deployer_principal=deployer_principal,
    )

    if dry_run:
        return

    istio_revision = ensure_namespaces()
    create_istio_revision_configmap(istio_revision)
    ensure_secrets()
    create_storage_classes()
    install_gateway_api_crds()
    _ensure_image_lock(config, refresh_images, image_branch, resolved_images)
    _create_osdu_config(config, infra_outputs)
    _create_istio_auth(config, infra_outputs)
    _create_spi_init_values(config)

    # Bare deploys no Gateway, so there is no ingress to configure.
    if config.profile is not Profile.BARE:
        resolve_post_deploy_inputs(config)
        create_ingress_config(
            config=config,
            external_dns_client_id=infra_outputs.get("external_dns_client_id", ""),
            tenant_id=infra_outputs.get("tenant_id", ""),
            gateway_ip=get_ingress_ip(),
        )

    console.print("\n[bold]Deploying Flux extension and GitOps config via Bicep...[/bold]")
    run_bicep_deployment(
        template_path=str(INFRA_FLUX_BICEP),
        parameters={
            "clusterName": config.cluster_name,
            "repoUrl": config.repo_url,
            "repoBranch": config.repo_branch,
            "repoTag": config.repo_tag,
            "profile": config.profile.value,
            "ingressMode": config.ingress_mode.value,
        },
        resource_group=config.resource_group,
        deployment_name=f"spi-flux-{config.env or 'base'}",
    )
    if config.profile is Profile.BARE:
        display_result(
            "GitOps activated for profile: bare (empty reconciliation; no middleware or ingress)"
        )
    else:
        display_result(
            f"GitOps activated for profile: {config.profile.value}, "
            f"ingress: {config.ingress_mode.value}"
        )

    seed = get_or_create_seed()
    _write_keyvault_bootstrap_secrets(
        config=config,
        keyvault_name=config.keyvault_name,
        storage_account_name=infra_outputs.get("common_storage_name", ""),
        elastic_password=seed["elastic_password"],
        redis_password=seed["redis_password"],
    )

    _finalize_gitops_source(config)


def _cluster_api_server(config: Config) -> str:
    """The API server FQDN of the cluster about to be deleted, or "".

    The FQDN proves a kubeconfig context belongs to this cluster rather than
    a same-named one in another subscription; empty means the cluster is
    gone and the prune leaves the kubeconfig alone. `privateFqdn` comes first
    because `az aks get-credentials` wrote that form for a private cluster.
    """
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
            "privateFqdn || fqdn",
            "--output",
            "tsv",
        ],
        description=f"Look up API server for {config.cluster_name}",
        display=False,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def cleanup_azure(config: Config) -> None:
    """Delete Azure resource group and all resources.

    The kubeconfig entries `spi up` merged in point at a cluster that is
    about to stop answering, so they are pruned on the way out. The prune
    waits for Azure to report the resource group gone; `--no-wait` returns on
    acceptance, and an accepted delete can still fail afterwards.
    """
    console.print("\n[bold]Cleaning up Azure resources...[/bold]")
    api_server = _cluster_api_server(config)
    result = run_command(
        ["az", "group", "delete", "--name", config.resource_group, "--yes", "--no-wait"],
        description=f"Delete resource group: {config.resource_group}",
        check=False,
    )
    if result.returncode != 0:
        console.print(f"[error]Azure cleanup request failed for {config.resource_group}.[/error]")
        raise typer.Exit(code=1)

    console.print("  [info]Waiting briefly for Azure to acknowledge the deletion...[/info]")
    deadline = time.time() + 60
    while time.time() < deadline:
        exists = run_command(
            ["az", "group", "exists", "--name", config.resource_group],
            description=f"Check resource group status: {config.resource_group}",
            display=False,
            check=False,
        )
        if exists.returncode == 0 and exists.stdout.strip().lower() == "false":
            prune_kube_context(config.cluster_name, server_fqdn=api_server)
            display_result(f"Resource group {config.resource_group} deleted")
            return
        time.sleep(10)

    display_result("Cleanup accepted by Azure; deletion is continuing in the background")
    console.print(
        f"  [warning]Verify later with: az group exists --name {config.resource_group}[/warning]"
    )
    console.print(
        f"  [warning]kubeconfig context {config.cluster_name} is left in place until the "
        f"deletion is confirmed[/warning]"
    )
