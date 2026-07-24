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

"""SPI CLI - Deploy OSDU SPI Stack on Azure AKS."""

import os
from typing import List, Optional

import typer
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .checks import PREREQ_TOOLS, check_prerequisites
from .config import BASE_NAME, AksMode, Config, IngressMode, Profile
from .console import console, display_result, display_yaml
from .guard import (
    DEFAULT_FLUX_NAMESPACE,
    SPI_GITREPOSITORY,
    get_suspend_status,
    resolve_flux_namespace,
    verify_spi_cluster,
)
from .identity import decode_jwt_claims, projected_user_ids
from .images import (
    DEFAULT_GHCR_ORG,
    DEFAULT_GHCR_TAG,
    DEFAULT_IMAGE_BRANCH,
    IMAGE_LOCK_CONFIGMAP,
    IMAGE_LOCK_NAMESPACE,
    ImageResolutionError,
    ImageSource,
    render_image_lock_configmap,
    resolve_image_lock,
)
from .ingress import ensure_istio_revision_published, resolve_acme_email, resolve_ingress_mode
from .shell import kubectl_apply_yaml, kubectl_json, run_command

app = typer.Typer(
    name="spi",
    help="SPI Stack - deploy, monitor, and manage OSDU on Azure AKS.",
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"spi {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the spi version and exit.",
    ),
) -> None:
    """SPI Stack - deploy, monitor, and manage OSDU on Azure AKS."""


def _show_config(config: Config, *, show_application_insights: bool = True):
    table = Table(title="SPI Stack Deployment", border_style="cyan")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Profile", config.profile.value)
    if config.env:
        table.add_row("Environment", config.env)
    table.add_row("Cluster Name", config.cluster_name)
    table.add_row("Resource Group", config.resource_group)
    table.add_row("Location", config.location)
    table.add_row(
        "AKS Mode",
        "Automatic 1.36"
        if config.aks_mode == AksMode.AUTOMATIC
        else "Base + Node Autoprovisioning",
    )
    table.add_row("Repository", config.repo_url)
    table.add_row("Branch", config.repo_branch)
    table.add_row("Data Partitions", ", ".join(config.data_partitions))
    table.add_row("Key Vault", config.keyvault_name)
    selector = config.image_ref or config.image_tag
    image_value = f"{config.image_source.value}:{selector}"
    if config.image_source == ImageSource.GHCR:
        image_value = f"{image_value} ({config.image_org})"
    table.add_row("Service Images", image_value)
    if show_application_insights:
        table.add_row(
            "Application Insights",
            "enabled" if config.application_insights else "disabled (dummy configuration)",
        )
    table.add_row("Ingress Mode", config.ingress_mode.value)
    if config.ingress_mode == IngressMode.DNS and config.dns_zone:
        table.add_row("DNS Zone", f"{config.dns_zone} (rg: {config.dns_zone_rg})")

    aad_override = os.environ.get("AAD_CLIENT_ID", "").strip()
    if aad_override:
        table.add_row("AAD Client ID", f"{aad_override} [dim](env override)[/dim]")
    else:
        table.add_row("AAD Client ID", "[dim](default: UAMI client id)[/dim]")
    table.add_row(
        "Creator Access",
        ", ".join(config.creator_user_ids) if config.creator_user_ids else "[dim]disabled[/dim]",
    )

    console.print(table)


def _show_next_steps(config: Config):
    console.print("\n[bold]Deployment initiated. Next steps:[/bold]")

    table = Table(border_style="dim")
    table.add_column("Action", style="cyan")
    table.add_column("Command", style="yellow")

    table.add_row("Watch progress", "kubectl get kustomizations -n osdu-flux --watch")
    table.add_row("Check operators", "kubectl get pods -n foundation")
    table.add_row("Check middleware", "kubectl get pods -n platform")
    table.add_row("Check services", "kubectl get pods -n osdu")
    table.add_row("View status", "uv run spi status")
    table.add_row("Cleanup", f"uv run spi down{config.env_flag}")

    console.print(table)


def _build_config(
    profile: Profile = Profile.CORE,
    env: str = "",
    repo_url: str = "https://github.com/Azure/osdu-spi-stack.git",
    branch: str = "main",
    location: str = "eastus2",
    data_partitions: Optional[List[str]] = None,
    ingress_mode: IngressMode = IngressMode.AZURE,
    dns_zone: str = "",
    ingress_prefix: str = "",
    acme_email: str = "",
    creator_user_ids: Optional[List[str]] = None,
    name_suffix: str = "",
    aks_mode: AksMode = AksMode.AUTOMATIC,
    application_insights: bool = False,
    image_source: ImageSource = ImageSource.GHCR,
    image_org: str = DEFAULT_GHCR_ORG,
    image_tag: Optional[str] = None,
    image_ref: str = "",
) -> Config:
    resolved_tag = image_tag
    resolved_ref = image_ref
    if image_source == ImageSource.GHCR and resolved_tag is None and not resolved_ref:
        resolved_tag = DEFAULT_GHCR_TAG
    if image_source == ImageSource.COMMUNITY:
        resolved_tag = ""
        resolved_ref = resolved_ref or DEFAULT_IMAGE_BRANCH

    return Config.from_env(
        env=env,
        name_suffix=name_suffix,
        profile=profile,
        repo_url=repo_url,
        repo_branch=branch,
        location=location,
        data_partitions=data_partitions or ["opendes"],
        ingress_mode=ingress_mode,
        dns_zone=dns_zone,
        ingress_prefix=ingress_prefix,
        acme_email=acme_email,
        creator_user_ids=creator_user_ids or [],
        aks_mode=aks_mode,
        application_insights=application_insights,
        image_source=image_source,
        image_org=image_org,
        image_tag=resolved_tag or "",
        image_ref=resolved_ref,
    )


def _resolve_creator_user_ids(seed_creator: bool, override: str) -> list[str]:
    """Resolve the current Azure caller exactly as the gateway projects it."""

    requested = override.strip()
    if not seed_creator:
        if requested:
            raise ValueError("--creator-user-id cannot be used with --no-seed-creator")
        return []
    if requested:
        if "\n" in requested or "\r" in requested:
            raise ValueError("--creator-user-id must be a single-line value")
        return [requested]

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
        description="Resolve Stack creator identity",
        display=False,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(
            "Unable to resolve the current Azure caller for Entitlements initialization. "
            "Run 'az login', pass --creator-user-id, or use --no-seed-creator."
        )
    return projected_user_ids(decode_jwt_claims(result.stdout.strip()))


def _resolve_image_selection(
    *,
    image_source: Optional[ImageSource],
    image_org: Optional[str],
    image_tag: Optional[str],
    image_ref: Optional[str],
    image_branch: Optional[str],
    current: Optional[tuple[ImageSource, str, str, str]] = None,
) -> tuple[ImageSource, str, str, str]:
    """Resolve explicit image options against a deployment's current selection."""
    for option_name, value in (
        ("--image-org", image_org),
        ("--image-tag", image_tag),
        ("--image-ref", image_ref),
        ("--image-branch", image_branch),
    ):
        if value is not None and not value.strip():
            raise ValueError(f"{option_name} must not be empty")

    image_org = image_org.strip() if image_org is not None else None
    image_tag = image_tag.strip() if image_tag is not None else None
    image_ref = image_ref.strip() if image_ref is not None else None
    image_branch = image_branch.strip() if image_branch is not None else None

    if image_tag is not None and image_ref is not None:
        raise ValueError("--image-tag and --image-ref cannot be used together")
    if image_branch is not None and (image_tag is not None or image_ref is not None):
        raise ValueError("--image-branch cannot be combined with --image-tag or --image-ref")
    if image_branch is not None and image_source not in (None, ImageSource.COMMUNITY):
        raise ValueError("--image-branch is a legacy community-image option")

    base_source, base_org, base_tag, base_ref = current or (
        ImageSource.GHCR,
        DEFAULT_GHCR_ORG,
        DEFAULT_GHCR_TAG,
        "",
    )

    if image_branch is not None:
        return ImageSource.COMMUNITY, "", "", image_branch

    inferred_source = (
        ImageSource.GHCR
        if image_source is None
        and (image_tag is not None or image_ref is not None or image_org is not None)
        else None
    )
    source = image_source or inferred_source or base_source
    source_changed = image_source is not None and source != base_source
    source_changed = source_changed or (inferred_source is not None and source != base_source)

    if source_changed:
        if source == ImageSource.GHCR:
            tag, ref = DEFAULT_GHCR_TAG, ""
        else:
            tag, ref = "", DEFAULT_IMAGE_BRANCH
    else:
        tag, ref = base_tag, base_ref

    if image_tag is not None:
        if source != ImageSource.GHCR:
            raise ValueError("--image-tag is supported only with --image-source ghcr")
        tag, ref = image_tag, ""
    elif image_ref is not None:
        if source != ImageSource.GHCR:
            raise ValueError(
                "--image-ref is for GHCR Git refs; use --image-branch for community images"
            )
        tag, ref = "", image_ref

    if source == ImageSource.GHCR:
        if image_org is not None:
            org = image_org
        elif source_changed or base_source != ImageSource.GHCR:
            org = DEFAULT_GHCR_ORG
        else:
            org = base_org or DEFAULT_GHCR_ORG
    else:
        if image_org is not None:
            raise ValueError("--image-org is supported only with --image-source ghcr")
        org = ""

    return source, org, tag, ref


def _read_image_lock_selection() -> tuple[ImageSource, str, str, str]:
    """Read the source, organization, and selector pinned in the live image lock."""
    configmap = kubectl_json(["get", "configmap", IMAGE_LOCK_CONFIGMAP, "-n", IMAGE_LOCK_NAMESPACE])
    if configmap is None:
        raise RuntimeError(
            f"Unable to read {IMAGE_LOCK_NAMESPACE}/{IMAGE_LOCK_CONFIGMAP}; "
            "specify an image source and selector explicitly"
        )

    data = configmap.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"{IMAGE_LOCK_NAMESPACE}/{IMAGE_LOCK_CONFIGMAP} has no image-lock data")

    raw_source = data.get("IMAGE_SOURCE", ImageSource.COMMUNITY.value)
    try:
        source = ImageSource(raw_source)
    except ValueError as exc:
        raise RuntimeError(
            f"{IMAGE_LOCK_NAMESPACE}/{IMAGE_LOCK_CONFIGMAP} has invalid IMAGE_SOURCE {raw_source!r}"
        ) from exc

    if source == ImageSource.GHCR:
        tag = data.get("IMAGE_TAG", "")
        ref = data.get("IMAGE_REF", "")
        if not tag and not ref:
            tag = DEFAULT_GHCR_TAG
        org = data.get("IMAGE_ORG", "") or DEFAULT_GHCR_ORG
        return source, org, tag, ref

    ref = data.get("IMAGE_REF") or data.get("IMAGE_BRANCH") or DEFAULT_IMAGE_BRANCH
    return source, "", "", ref


def _resolve_name_suffix(env: str, for_up: bool) -> str:
    """Resolve the per-deployment name suffix from the resource group tag.

    Lookup order:
      1. If the RG already carries the `spi-name-suffix` tag, use its value
         (empty string = legacy pre-suffix deployment, pin to legacy names).
      2. If the RG exists without the tag but holds a legacy unsuffixed Key
         Vault, treat as legacy: return "" so names stay unsuffixed. On `up`
         we also persist the empty marker so future runs short-circuit at
         step 1.
      3. Otherwise mint a new random suffix. On `up` for an existing RG
         (resumed/failed deploy) we persist immediately; create_resource_group
         writes the tag for a brand-new RG via the --tags flag.

    `for_up=False` (used by `down`) skips persistence — it's read-only so the
    displayed config table accurately reflects what's in Azure.
    """
    from .config import generate_name_suffix

    if not env:
        return ""

    from .azure_infra import detect_legacy_keyvault, read_rg_suffix_tag, write_rg_suffix_tag

    rg = f"spi-stack-{env}"
    existing = read_rg_suffix_tag(rg)
    if existing is not None:
        return existing

    # RG missing or RG present without our tag. Distinguish legacy from fresh.
    if detect_legacy_keyvault(rg, env):
        if for_up:
            write_rg_suffix_tag(rg, "")
        return ""

    suffix = generate_name_suffix()
    if for_up:
        # Brand-new RGs get tagged by create_resource_group via --tags.
        # If the RG already exists (resumed/failed deploy with no legacy KV),
        # persist now so subsequent runs are stable.
        rg_exists = run_command(
            ["az", "group", "exists", "--name", rg],
            description=f"Check resource group exists: {rg}",
            display=False,
            check=False,
        )
        if rg_exists.returncode == 0 and rg_exists.stdout.strip().lower() == "true":
            write_rg_suffix_tag(rg, suffix)
    return suffix


def _resolve_application_insights(
    env: str,
    requested: Optional[bool],
    for_up: bool,
) -> bool:
    """Resolve and preserve the environment's Application Insights mode.

    New environments default to disabled. Once an environment has been
    created, its mode is immutable so an idempotent rerun cannot silently
    orphan paid resources or rewrite service configuration.
    """
    from .azure_infra import (
        detect_existing_application_insights,
        detect_existing_log_analytics,
        read_deployed_application_insights_mode,
        read_rg_application_insights_tag,
        resource_group_has_resources,
        write_rg_application_insights_tag,
    )

    environment_label = env or "base"
    rg = f"{BASE_NAME}-{env}" if env else BASE_NAME
    persisted = read_rg_application_insights_tag(rg)
    if persisted is not None:
        if requested is not None and requested != persisted:
            raise RuntimeError(
                f"Environment {environment_label!r} was created with Application Insights "
                f"{'enabled' if persisted else 'disabled'}. The setting cannot be "
                "changed in place; run 'spi down' and create a new environment."
            )
        return persisted

    rg_exists = run_command(
        ["az", "group", "exists", "--name", rg],
        description=f"Check resource group exists: {rg}",
        display=False,
        check=False,
    )
    if rg_exists.returncode != 0:
        raise RuntimeError(
            f"Unable to determine whether resource group {rg} exists: "
            f"{rg_exists.stderr.strip() or rg_exists.stdout.strip()}"
        )
    exists = rg_exists.stdout.strip().lower() == "true"
    if exists and not resource_group_has_resources(rg):
        resolved = bool(requested)
        if for_up:
            write_rg_application_insights_tag(rg, resolved)
        return resolved
    component_exists = detect_existing_application_insights(rg, env) if exists else False
    workspace_exists = (
        detect_existing_log_analytics(rg, env) if exists and not component_exists else False
    )
    deployed_mode = (
        read_deployed_application_insights_mode(rg, env)
        if exists and not component_exists and not workspace_exists
        else None
    )
    inferred = component_exists or workspace_exists or deployed_mode is True

    if exists and requested is not None and requested != inferred:
        raise RuntimeError(
            f"Existing environment {environment_label!r} has Application Insights "
            f"{'enabled' if inferred else 'disabled'}. The setting cannot be changed "
            "in place; run 'spi down' and create a new environment."
        )

    resolved = inferred if requested is None else requested
    if for_up and exists:
        write_rg_application_insights_tag(rg, resolved)
    return resolved


def _resolve_aks_mode(
    env: str,
    requested: "AksMode | None",
    for_up: bool,
) -> AksMode:
    """Resolve, validate, and preserve the environment's AKS topology."""
    from .azure_infra import (
        detect_existing_aks_mode,
        read_rg_aks_mode_tag,
        write_rg_aks_mode_tag,
    )

    environment_label = env or "base"
    rg = f"{BASE_NAME}-{env}" if env else BASE_NAME
    cluster_name = rg
    persisted = read_rg_aks_mode_tag(rg)

    rg_exists = run_command(
        ["az", "group", "exists", "--name", rg],
        description=f"Check resource group exists: {rg}",
        display=False,
        check=False,
    )
    if rg_exists.returncode != 0:
        raise RuntimeError(
            f"Unable to determine whether resource group {rg} exists: "
            f"{rg_exists.stderr.strip() or rg_exists.stdout.strip()}"
        )
    exists = rg_exists.stdout.strip().lower() == "true"
    actual = detect_existing_aks_mode(rg, cluster_name) if exists else None

    if persisted is not None and actual is not None and persisted != actual:
        raise RuntimeError(
            f"Environment {environment_label!r} is tagged for AKS {persisted.value}, "
            f"but the live cluster is {actual.value}. Repair the tag or cluster before rerunning."
        )

    established = actual or persisted
    if established is not None:
        if requested is not None and requested != established:
            raise RuntimeError(
                f"Environment {environment_label!r} was created with AKS "
                f"{established.value}. The mode cannot be changed in place; run "
                "'spi down' and create a new environment."
            )
        if for_up and exists and persisted is None:
            write_rg_aks_mode_tag(rg, established)
        return established

    resolved = requested or AksMode.AUTOMATIC
    if for_up and exists:
        write_rg_aks_mode_tag(rg, resolved)
    return resolved


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


@app.command()
def check(
    output_json: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
):
    """Validate that required CLI tools are installed."""
    from .checks import results_to_json, run_checks

    results = run_checks()
    missing = sum(1 for r in results if not r["installed"])

    if output_json:
        print(results_to_json(results))
        raise typer.Exit(code=1 if missing else 0)

    table = Table(title="SPI Stack Prerequisites", border_style="cyan")
    table.add_column("Tool", style="cyan", min_width=10)
    table.add_column("Status", justify="center", min_width=8)
    table.add_column("Detail")

    for r in results:
        if r["installed"]:
            status = "[success]OK[/success]"
            detail = r["version"]
        else:
            status = "[error]MISSING[/error]"
            hint = r.get("install_cmd", "")
            detail = f"[info]{hint}[/info]" if hint else "[dim]no install hint[/dim]"
        table.add_row(r["name"], status, detail)

    console.print()
    console.print(table)

    installed = sum(1 for r in results if r["installed"])
    if missing == 0:
        console.print(f"\n[success]All {len(results)} tools available.[/success]")
    else:
        console.print(
            f"\n[warning]{installed}/{len(results)} installed, {missing} missing.[/warning]"
        )
        raise typer.Exit(code=1)


@app.command()
def up(
    profile: Optional[Profile] = typer.Option(
        None,
        help="Deployment profile: core (default; middleware + OSDU services) or "
        "minimal (middleware only, no OSDU services).",
    ),
    env: str = typer.Option(..., "--env", help="Environment name (required, e.g. dev1, test)"),
    repo_url: str = typer.Option(
        "https://github.com/Azure/osdu-spi-stack.git",
        "--repo",
        help="Git repository URL",
    ),
    branch: str = typer.Option("main", "--branch", help="Git branch"),
    location: str = typer.Option("eastus2", "--location", help="Azure region"),
    aks_mode: Optional[AksMode] = typer.Option(
        None,
        "--aks-mode",
        help="AKS topology: automatic (default) or base (Base SKU + Node Autoprovisioning). "
        "The choice is preserved on reruns.",
    ),
    data_partitions: Optional[List[str]] = typer.Option(
        None, "--partition", help="Data partition names (can specify multiple)"
    ),
    ingress_mode: Optional[IngressMode] = typer.Option(
        None,
        "--ingress-mode",
        help="Ingress mode: azure (default; auto-FQDN + TLS) or dns (custom zone). "
        "Also honors SPI_INGRESS_MODE env var.",
    ),
    dns_zone: str = typer.Option(
        "",
        "--dns-zone",
        help="Azure DNS zone to use in dns mode. Auto-discovered from the current "
        "subscription if omitted and exactly one zone exists.",
    ),
    ingress_prefix: str = typer.Option(
        "",
        "--ingress-prefix",
        help="Hostname prefix used in dns mode. Defaults to the --env value.",
    ),
    acme_email: str = typer.Option(
        "",
        "--acme-email",
        help="Contact email for Let's Encrypt ACME account. Also honors SPI_ACME_EMAIL.",
    ),
    seed_creator: bool = typer.Option(
        True,
        "--seed-creator/--no-seed-creator",
        help="Seed the current Azure caller into Entitlements root groups during init.",
    ),
    creator_user_id: str = typer.Option(
        "",
        "--creator-user-id",
        help="Override the creator identifier projected as x-user-id. "
        "Defaults to the current Azure CLI token claims.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview Azure PaaS changes via Bicep what-if. Creates the resource group "
        "(required by what-if) but skips AKS, Kubernetes bootstrap, and GitOps.",
    ),
    refresh_images: bool = typer.Option(
        True,
        "--refresh-images/--no-refresh-images",
        help="Resolve service images and write the Flux image lock.",
    ),
    image_source: Optional[ImageSource] = typer.Option(
        None,
        "--image-source",
        help="Service image source: ghcr (SPI service forks) or community.",
    ),
    image_org: Optional[str] = typer.Option(
        None,
        "--image-org",
        help="GitHub organization containing SPI service repositories and GHCR packages.",
    ),
    image_tag: Optional[str] = typer.Option(
        None,
        "--image-tag",
        help="Exact GHCR tag shared by the service fleet, such as main-snapshot "
        "or a coordinated release tag.",
    ),
    image_ref: Optional[str] = typer.Option(
        None,
        "--image-ref",
        help="Advanced: resolve the same Git ref to a sha-* image in every service repository.",
    ),
    image_branch: Optional[str] = typer.Option(
        None,
        "--image-branch",
        help="Legacy alias for community image branches.",
    ),
    application_insights: Optional[bool] = typer.Option(
        None,
        "--application-insights/--no-application-insights",
        help="Deploy workspace-based Application Insights. New environments default "
        "to disabled; the choice is preserved on reruns.",
    ),
):
    """Provision Azure infrastructure and deploy the OSDU SPI stack."""
    if profile is None:
        profile = Profile.CORE

    title = "[bold]SPI Stack[/bold] - Azure-native OSDU Software Stack"
    if dry_run:
        title += "\n[warning]DRY RUN: previewing Bicep changes only[/warning]"
    else:
        title += "\nAKS + Azure PaaS + Flux CD GitOps"

    console.print(Panel(title, border_style="cyan"))
    check_prerequisites(PREREQ_TOOLS)

    # Resolve the persistent suffix from the RG tag (or mint a new one) so
    # derived resource names are stable across `spi up` re-runs and don't
    # collide with deployments in other subscriptions.
    name_suffix = _resolve_name_suffix(env, for_up=True)
    try:
        resolved_aks_mode = _resolve_aks_mode(
            env,
            requested=aks_mode,
            for_up=not dry_run,
        )
        resolved_application_insights = _resolve_application_insights(
            env,
            requested=application_insights,
            for_up=not dry_run,
        )
    except RuntimeError as exc:
        console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=2)
    try:
        resolved_creator_user_ids = _resolve_creator_user_ids(seed_creator, creator_user_id)
        (
            resolved_image_source,
            resolved_image_org,
            resolved_image_tag,
            resolved_image_ref,
        ) = _resolve_image_selection(
            image_source=image_source,
            image_org=image_org,
            image_tag=image_tag,
            image_ref=image_ref,
            image_branch=image_branch,
        )
    except ValueError as exc:
        console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=2)

    config = _build_config(
        profile=profile,
        env=env,
        repo_url=repo_url,
        branch=branch,
        location=location,
        data_partitions=data_partitions,
        ingress_mode=resolve_ingress_mode(ingress_mode),
        dns_zone=dns_zone,
        ingress_prefix=ingress_prefix,
        acme_email=resolve_acme_email(acme_email),
        creator_user_ids=resolved_creator_user_ids,
        name_suffix=name_suffix,
        aks_mode=resolved_aks_mode,
        application_insights=resolved_application_insights,
        image_source=resolved_image_source,
        image_org=resolved_image_org,
        image_tag=resolved_image_tag,
        image_ref=resolved_image_ref,
    )

    _show_config(config)

    try:
        from .deploy import deploy_azure

        deploy_azure(
            config,
            dry_run=dry_run,
            refresh_images=refresh_images,
        )
        if dry_run:
            console.print(
                "\n[success]Dry-run complete. No AKS cluster or Kubernetes workloads "
                "were provisioned.[/success]"
            )
            console.print(
                "[dim]Federated credentials and anything that depends on the AKS OIDC "
                "issuer are skipped in the preview; a real run will add them.[/dim]\n"
            )
        else:
            _show_next_steps(config)
            console.print(
                "\n[success]SPI Stack deployment initiated. Flux is reconciling in the background.[/success]"
            )
            console.print(
                "[dim]Environment is pinned to the current commit. "
                "Run 'spi reconcile' to pull updates when ready.[/dim]\n"
            )
    except Exception as e:
        console.print(f"\n[error]Deployment failed: {e}[/error]")
        raise typer.Exit(code=1)


@app.command()
def down(
    env: str = typer.Option(..., "--env", help="Environment name"),
):
    """Tear down all Azure resources."""
    console.print(Panel("[bold]SPI Stack Cleanup[/bold]", border_style="cyan"))
    check_prerequisites(["az"])

    name_suffix = _resolve_name_suffix(env, for_up=False)
    # Teardown must stay available even when the recorded mode disagrees with
    # the cluster, which is exactly the state an operator needs to clean up.
    try:
        aks_mode = _resolve_aks_mode(env, requested=None, for_up=False)
    except RuntimeError as exc:
        console.print(f"[warning]{exc}[/warning]")
        aks_mode = AksMode.AUTOMATIC
    config = _build_config(
        env=env,
        name_suffix=name_suffix,
        aks_mode=aks_mode,
    )
    _show_config(config, show_application_insights=False)

    from .deploy import cleanup_azure

    cleanup_azure(config)


@app.command()
def info(
    show_secrets: bool = typer.Option(
        False, "--show-secrets", help="Display live Kubernetes credentials"
    ),
    show_apis: bool = typer.Option(
        False, "--show-apis", help="Expand the full OSDU API endpoint list"
    ),
    output_json: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
):
    """Show cluster access endpoints and optional credentials."""
    ctx = verify_spi_cluster()

    from .info import render_info

    if not output_json:
        console.print(f"  [dim]Cluster context: {ctx}[/dim]")
    render_info(show_secrets=show_secrets, show_apis=show_apis, output_json=output_json)


@app.command()
def status(
    watch: bool = typer.Option(False, "--watch", "-w", help="Continuous refresh"),
):
    """Show deployment health and reconciliation progress."""
    ctx = verify_spi_cluster()
    console.print(f"  [dim]Cluster context: {ctx}[/dim]")

    from .status import render_status, watch_status

    if watch:
        watch_status()
    else:
        render_status()


def _flux_resource_names(kind: str, namespace: str) -> List[str]:
    """Names of all Flux resources of ``kind`` in ``namespace``.

    Raises RuntimeError if the kubectl listing itself fails. A failed query
    must not be mistaken for "no resources": CI-mode suspend (ADR-032) would
    then report a successful freeze while HelmReleases keep reconciling, the
    exact failure this path exists to prevent. ``kubectl_json`` returns None
    on command failure and a dict (possibly with an empty ``items``) on success.
    """
    data = kubectl_json(["get", kind, "-n", namespace])
    if data is None:
        raise RuntimeError(
            f"Failed to list Flux {kind} resources in namespace '{namespace}'; "
            "refusing to continue because cluster state could not be read."
        )
    return [
        item["metadata"]["name"]
        for item in data.get("items", [])
        if item.get("metadata", {}).get("name")
    ]


def _set_flux_suspend(namespace: str, suspend: bool) -> None:
    """Suspend or resume the SPI Stack's GitRepository, Kustomizations, and HelmReleases.

    The deploy lane (ADR-032) requires the cluster in CI mode -- all Flux reconcilers
    frozen -- so a ``kubectl set image`` is not drift-corrected back to the chart's
    pinned image. Suspending the GitRepository source alone (ADR-014) is not enough:
    the Kustomizations and, critically, the HelmReleases keep reconciling the cached
    artifact and revert the deployed image. Freeze all three.
    """
    patch = '{"spec":{"suspend":true}}' if suspend else '{"spec":{"suspend":false}}'
    verb = "Suspend" if suspend else "Resume"
    targets = [("gitrepository", [SPI_GITREPOSITORY])]
    for kind in ("kustomization", "helmrelease"):
        targets.append((kind, _flux_resource_names(kind, namespace)))
    for kind, names in targets:
        for name in names:
            run_command(
                ["kubectl", "patch", kind, name, "-n", namespace, "--type=merge", "-p", patch],
                description=f"{verb} {kind}/{name}",
                check=False,
            )


@app.command()
def reconcile(
    suspend: bool = typer.Option(False, "--suspend", help="Freeze: stop Flux auto-reconciliation"),
    resume: bool = typer.Option(
        False, "--resume", help="Unfreeze: resume Flux auto-reconciliation"
    ),
    refresh_images: bool = typer.Option(
        False,
        "--refresh-images",
        help="Resolve service images and update osdu-image-lock before reconciling.",
    ),
    image_source: Optional[ImageSource] = typer.Option(
        None,
        "--image-source",
        help="Service image source: ghcr (SPI service forks) or community.",
    ),
    image_org: Optional[str] = typer.Option(
        None,
        "--image-org",
        help="GitHub organization containing SPI service repositories and GHCR packages.",
    ),
    image_tag: Optional[str] = typer.Option(
        None,
        "--image-tag",
        help="Exact GHCR tag shared by the service fleet, such as main-snapshot "
        "or a coordinated release tag.",
    ),
    image_ref: Optional[str] = typer.Option(
        None,
        "--image-ref",
        help="Advanced: resolve the same Git ref to a sha-* image in every service repository.",
    ),
    image_branch: Optional[str] = typer.Option(
        None,
        "--image-branch",
        help="Legacy alias for community image branches.",
    ),
):
    """Force Flux to reconcile the git source and stack."""
    import datetime

    if suspend and resume:
        console.print("[error]Cannot use --suspend and --resume together.[/error]")
        raise typer.Exit(code=1)
    if refresh_images and (suspend or resume):
        console.print(
            "[error]--refresh-images cannot be combined with --suspend or --resume.[/error]"
        )
        raise typer.Exit(code=1)
    if not refresh_images and any(
        option is not None
        for option in (image_source, image_org, image_tag, image_ref, image_branch)
    ):
        console.print("[error]Image selector options require --refresh-images.[/error]")
        raise typer.Exit(code=2)

    ctx = verify_spi_cluster()
    console.print(f"  [dim]Cluster context: {ctx}[/dim]")

    if suspend:
        ns = resolve_flux_namespace()
        console.print(f"\n[bold]Entering CI mode: freezing Flux reconciliation in '{ns}'...[/bold]")
        _set_flux_suspend(ns, True)
        console.print(
            "[warning]GitRepository, Kustomizations, and HelmReleases suspended.[/warning]"
        )
        console.print(
            "[dim]The cluster is pinned and safe for deploy-lane CI (ADR-032). "
            "Run 'uv run spi reconcile --resume' to unfreeze.[/dim]"
        )
        return

    if resume:
        ns = resolve_flux_namespace()
        # An environment created before the revision was published would let
        # Flux apply an empty istio.io/rev and drop sidecar injection.
        try:
            ensure_istio_revision_published()
        except RuntimeError as exc:
            console.print(f"[error]{exc}[/error]")
            raise typer.Exit(code=1)
        console.print(f"\n[bold]Resuming Flux reconciliation in '{ns}'...[/bold]")
        _set_flux_suspend(ns, False)
        console.print("[success]GitRepository, Kustomizations, and HelmReleases resumed.[/success]")
        return

    if refresh_images:
        try:
            current_selection = _read_image_lock_selection()
        except RuntimeError as exc:
            if (
                image_source is None
                and image_tag is None
                and image_ref is None
                and image_branch is None
            ):
                console.print(f"[error]{exc}[/error]")
                raise typer.Exit(code=1)
            current_selection = None

        try:
            resolved_source, resolved_org, resolved_tag, resolved_ref = _resolve_image_selection(
                image_source=image_source,
                image_org=image_org,
                image_tag=image_tag,
                image_ref=image_ref,
                image_branch=image_branch,
                current=current_selection,
            )
        except ValueError as exc:
            console.print(f"[error]{exc}[/error]")
            raise typer.Exit(code=2)

        selector = resolved_ref or resolved_tag
        console.print(
            f"\n[bold]Resolving {resolved_source.value} service images at {selector}...[/bold]"
        )
        try:
            resolved = resolve_image_lock(
                source=resolved_source,
                tag=resolved_tag,
                ref=resolved_ref,
                org=resolved_org,
            )
        except ImageResolutionError as exc:
            console.print(f"[error]Unable to resolve OSDU service images: {exc}[/error]")
            raise typer.Exit(code=1)

        for name, image in resolved.items():
            console.print(
                f"  [success]{name}[/success] -> {image.repository.split('/')[-1]}:{image.tag[:12]}"
            )

        image_lock_yaml = render_image_lock_configmap(
            resolved,
            source=resolved_source,
            tag=resolved_tag,
            ref=resolved_ref,
            org=resolved_org,
        )
        display_yaml(image_lock_yaml, "ConfigMap: osdu-image-lock")
        kubectl_apply_yaml(image_lock_yaml, "apply osdu-image-lock ConfigMap")
        display_result("osdu-image-lock ConfigMap updated")

    # Default: force reconcile
    if get_suspend_status():
        console.print(
            Panel(
                "[bold yellow]GitRepository is currently SUSPENDED.[/bold yellow]\n"
                "This reconcile is a one-shot trigger; Flux will not auto-reconcile future commits.\n"
                "[dim]Use --resume to unfreeze, or --suspend to re-freeze after.[/dim]",
                border_style="yellow",
            )
        )

    ts = datetime.datetime.now().isoformat()
    ns = resolve_flux_namespace()
    console.print(f"\n[bold]Reconciling (namespace '{ns}')...[/bold]")

    run_command(
        [
            "kubectl",
            "annotate",
            "--overwrite",
            f"gitrepository/{SPI_GITREPOSITORY}",
            "-n",
            ns,
            f"reconcile.fluxcd.io/requestedAt={ts}",
        ],
        description="Trigger GitRepository reconciliation",
    )

    for name in _flux_resource_names("kustomization", ns):
        run_command(
            [
                "kubectl",
                "annotate",
                "--overwrite",
                f"kustomization/{name}",
                "-n",
                ns,
                f"reconcile.fluxcd.io/requestedAt={ts}",
            ],
            description=f"Trigger Kustomization reconciliation ({name})",
            check=False,
        )

    console.print("[success]Reconciliation triggered.[/success]")


@app.command()
def update(
    check: bool = typer.Option(False, "--check", help="Check for an update; do not install."),
    force: bool = typer.Option(
        False, "--force", help="Reinstall even if already on the latest version."
    ),
    silent: bool = typer.Option(
        False, "--silent", help="Suppress changelog and command panels; print only the outcome."
    ),
    token: Optional[str] = typer.Option(
        None, "--token", help="GitHub token for release-notes fetch (overrides env / gh auth)."
    ),
):
    """Check for and install the latest spi release from GitHub Releases."""
    from packaging.version import InvalidVersion
    from packaging.version import Version as _Version
    from rich.markdown import Markdown

    from . import update as _update

    if __version__ == "0.0.0+source":
        console.print("[info]you are running from a source checkout; pull with git instead.[/info]")
        raise typer.Exit(code=0)

    try:
        current = _Version(__version__)
    except InvalidVersion:
        console.print(f"[error]cannot parse current spi version '{__version__}'.[/error]")
        raise typer.Exit(code=1)

    installer = _update.detect_installer()
    if installer is None:
        console.print(
            "[error]spi was not installed by uv tool or pipx; manual upgrade required.[/error]"
        )
        console.print("[dim]If you cloned the repo, use `git pull` instead.[/dim]")
        raise typer.Exit(code=1)

    tok = _update.resolve_github_token(token)

    try:
        release = _update.fetch_latest_release(token=tok)
        latest = _update.parse_version_from_release(release)
    except _update.UpdateError as exc:
        console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=1)

    up_to_date = current >= latest

    if check:
        if up_to_date:
            console.print(f"spi {current} (already on latest)")
        else:
            console.print(f"spi {current} -> {latest} (update available)")
        raise typer.Exit(code=0)

    if up_to_date and not force:
        if silent:
            typer.echo(f"spi {current}")
        else:
            console.print(f"[success]spi {current} (already on latest)[/success]")
        raise typer.Exit(code=0)

    try:
        wheel_url = _update.find_wheel_asset_url(release)
    except _update.UpdateError as exc:
        console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=1)

    if not silent:
        notes = _update.fetch_release_notes(current, latest, token=tok)
        if notes:
            console.print(
                Panel(
                    Markdown(notes),
                    title=f"Changelog {current} -> {latest}",
                    border_style="cyan",
                )
            )
        else:
            console.print(
                "[info](unable to fetch release notes; "
                "set GITHUB_TOKEN or `gh auth login` to raise rate limits)[/info]"
            )

    rc = _update.run_upgrade(installer, wheel_url, display=not silent)
    if rc != 0:
        if silent:
            typer.echo(f"spi upgrade failed (exit {rc})", err=True)
        raise typer.Exit(code=1)

    on_disk = _update.installed_version()
    if on_disk is None or on_disk < latest:
        actual = on_disk if on_disk is not None else current
        if silent:
            typer.echo(f"spi upgrade no-op: still on {actual}", err=True)
        else:
            console.print(
                f"[error]upgrade reported success but installed version is still {actual}.[/error]"
            )
        raise typer.Exit(code=1)

    if silent:
        typer.echo(f"spi {on_disk}")
    else:
        console.print(
            Panel(
                f"[success]Updated spi {current} -> {on_disk}[/success]",
                border_style="green",
            )
        )


@app.command()
def onboard(
    service: str = typer.Option(..., "--service", help="Service short name (e.g. partition)."),
    repo: str = typer.Option(
        ..., "--repo", help="Target GitHub repo as org/repo (e.g. my-org/partition)."
    ),
    aks_cluster: str = typer.Option(
        ..., "--aks-cluster", help="AKS cluster name to grant deploy access to."
    ),
    aks_rg: str = typer.Option(..., "--aks-rg", help="Resource group of the AKS cluster."),
    identities_rg: str = typer.Option(
        ..., "--identities-rg", help="Resource group for the CI managed identity."
    ),
    namespace: str = typer.Option(
        "osdu", "--namespace", help="Kubernetes namespace the service Deployment lives in."
    ),
    flux_namespace: str = typer.Option(
        DEFAULT_FLUX_NAMESPACE,
        "--flux-namespace",
        help="Namespace holding the Flux Kustomizations (this stack uses osdu-flux).",
    ),
    partition: str = typer.Option(
        "opendes",
        "--partition",
        help="Data partition whose entitlements groups the CI identity is seeded into.",
    ),
    keyvault: Optional[str] = typer.Option(
        None,
        "--keyvault",
        help="Key Vault name to grant Secrets User on (for acceptance-test secrets).",
    ),
    gateway_url: Optional[str] = typer.Option(
        None,
        "--gateway-url",
        help="Cluster ingress base URL; written as the GATEWAY_URL repo variable when provided.",
    ),
    no_data_access_token_env: Optional[str] = typer.Option(
        None,
        "--no-data-access-token-env",
        help=(
            "Opt this repo into a no-data-access token and choose its test env name. "
            "Storage defaults to NO_DATA_ACCESS_TESTER_ACCESS_TOKEN; an existing repo "
            "profile value is reused. Pass an empty value to disable."
        ),
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan without making changes."),
    force_rewrite_secrets: bool = typer.Option(
        False,
        "--force-rewrite-secrets",
        help="Overwrite AZURE_* repo secrets even if already present.",
    ),
):
    """Grant a GitHub service-fork repo permission to deploy into this cluster.

    Cluster-side half of CI/CD onboarding (design SS9.4.A): creates a managed identity +
    federated credentials + Azure RBAC and, for test profiles that opt in, creates or reuses
    the shared no-data-access identity without RBAC or entitlements. It then writes the
    AZURE_* secrets and repo->cluster link variables onto the target repo. Idempotent on
    re-run; running it against a NEW cluster seamlessly re-homes the repo. Use --dry-run
    to preview.
    """
    from .onboard import OnboardInputs
    from .onboard import onboard as _run_onboard

    _run_onboard(
        OnboardInputs(
            service=service,
            repo=repo,
            aks_cluster=aks_cluster,
            aks_rg=aks_rg,
            identities_rg=identities_rg,
            namespace=namespace,
            flux_namespace=flux_namespace,
            partition=partition,
            keyvault=keyvault,
            gateway_url=gateway_url,
            no_data_access_token_env=no_data_access_token_env,
            dry_run=dry_run,
            force_rewrite_secrets=force_rewrite_secrets,
        )
    )
