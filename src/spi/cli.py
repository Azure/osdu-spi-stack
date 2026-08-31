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

"""SPI CLI - Deploy OSDU SPI Stack on Azure AKS Automatic."""

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import typer
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .bootstrap import create_istio_revision_configmap
from .checks import PREREQ_TOOLS, check_prerequisites
from .config import Config, IngressMode, Profile
from .console import console, display_result
from .guard import get_suspend_status, verify_spi_cluster
from .images import (
    DEFAULT_IMAGE_BRANCH,
    ImageResolutionError,
    resolve_image_lock,
)
from .ingress import resolve_acme_email, resolve_ingress_mode
from .pins import (
    PinError,
    ResetRefusedError,
    VerifyError,
    apply_image_lock,
    apply_schema_load_backfill,
    live_pins,
    pin_service,
    pin_service_image,
    reset_service,
    sweep_stale_ephemeral_pins,
    verify_service_image,
)
from .shell import run_command

app = typer.Typer(
    name="spi",
    help="SPI Stack - deploy, monitor, and manage OSDU on Azure AKS Automatic.",
    add_completion=False,
)

service_app = typer.Typer(
    help="Pin individual services to merge-request or fork-built images for validation."
)
app.add_typer(service_app, name="service")

maintenance_app = typer.Typer(help="Manage backing-environment maintenance state.")
app.add_typer(maintenance_app, name="maintenance")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"spi {__version__}")
        raise typer.Exit()


def _emit_outcome(outcome: str, code: Optional[str], detail: str, **extra) -> None:
    """Print the machine-readable envelope for a `--json` service command.

    One compact line, printed last on stdout: unlike `spi status --json`,
    these commands show Rich command panels while they work, so consumers
    parse the final stdout line rather than the whole stream.
    """
    from .status import STATUS_API_VERSION

    payload: Dict[str, Any] = {
        "apiVersion": STATUS_API_VERSION,
        "outcome": outcome,
        "code": code,
        "detail": detail,
    }
    payload.update(extra)
    print(json.dumps(payload))


def _guarded_context(output_json: bool) -> str:
    """Run the cluster guard, keeping the `--json` final-line contract.

    The guard prints its diagnosis and exits 1; a `--json` caller still needs
    the envelope as the last stdout line to classify the failure.
    """
    try:
        return verify_spi_cluster()
    except typer.Exit:
        if output_json:
            _emit_outcome("error", None, "cluster guard failed; see the log above")
        raise


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
    """SPI Stack - deploy, monitor, and manage OSDU on Azure AKS Automatic."""


def _show_config(config: Config):
    table = Table(title="SPI Stack Deployment", border_style="cyan")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Profile", config.profile.value)
    if config.env:
        table.add_row("Environment", config.env)
    table.add_row("Cluster Name", config.cluster_name)
    table.add_row("Resource Group", config.resource_group)
    table.add_row("Location", config.location)
    table.add_row("Repository", config.repo_url)
    if config.repo_tag:
        table.add_row("Tag", config.repo_tag)
    else:
        table.add_row("Branch", config.repo_branch)
    table.add_row("Data Partitions", ", ".join(config.data_partitions))
    table.add_row("Key Vault", config.keyvault_name)
    if config.profile is not Profile.BARE:
        table.add_row("Ingress Mode", config.ingress_mode.value)
        if config.ingress_mode == IngressMode.DNS and config.dns_zone:
            table.add_row("DNS Zone", f"{config.dns_zone} (rg: {config.dns_zone_rg})")

    aad_override = os.environ.get("AAD_CLIENT_ID", "").strip()
    if aad_override:
        table.add_row("AAD Client ID", f"{aad_override} [dim](env override)[/dim]")
    else:
        table.add_row("AAD Client ID", "[dim](default: UAMI client id)[/dim]")

    console.print(table)


def _show_next_steps(config: Config):
    console.print("\n[bold]Deployment initiated. Next steps:[/bold]")

    table = Table(border_style="dim")
    table.add_column("Action", style="cyan")
    table.add_column("Command", style="yellow")

    table.add_row("Watch progress", "kubectl get kustomizations -n osdu-flux --watch")
    if config.profile is not Profile.BARE:
        table.add_row("Check operators", "kubectl get pods -n foundation")
        table.add_row("Check middleware", "kubectl get pods -n platform")
        table.add_row("Check services", "kubectl get pods -n osdu")
    table.add_row("View status", "spi status")
    table.add_row("Cleanup", f"spi down{config.env_flag}")

    console.print(table)


def _trigger_kustomization(name: str, requested_at: str) -> None:
    run_command(
        [
            "kubectl",
            "annotate",
            "--overwrite",
            f"kustomization/{name}",
            "-n",
            "osdu-flux",
            f"reconcile.fluxcd.io/requestedAt={requested_at}",
        ],
        description=f"Trigger Kustomization reconciliation ({name})",
        check=False,
    )


def _kustomization_exists(name: str) -> bool:
    """Report whether a Kustomization is declared on this cluster.

    `--ignore-not-found` keeps genuine absence (a profile that never declares
    the resource) an empty result, while authorization errors, API timeouts,
    and context failures still abort instead of being read as "not present".
    """
    result = run_command(
        [
            "kubectl",
            "get",
            "kustomization",
            name,
            "-n",
            "osdu-flux",
            "--ignore-not-found",
            "-o",
            "name",
        ],
        description=f"Check Kustomization exists ({name})",
        display=False,
    )
    return bool(result.stdout.strip())


def _reconcile_kustomization(name: str) -> None:
    run_command(
        [
            "flux",
            "reconcile",
            "kustomization",
            name,
            "-n",
            "osdu-flux",
            "--timeout",
            "40m",
        ],
        description=f"Trigger and wait for Kustomization reconciliation ({name})",
    )


def _backfill_schema_load_lock(image_branch: str) -> None:
    """Backfill schema-load through the shared image-lock CAS primitive."""
    try:
        changed = apply_schema_load_backfill(branch=image_branch)
    except (ImageResolutionError, PinError) as exc:
        console.print(f"[error]Unable to backfill the schema-load image: {exc}[/error]")
        raise typer.Exit(code=1)
    if changed:
        display_result("osdu-image-lock ConfigMap updated with schema-load")


def _build_config(
    profile: Profile = Profile.CORE,
    env: str = "",
    repo_url: str = "https://github.com/Azure/osdu-spi-stack.git",
    branch: str = "main",
    tag: str = "",
    location: str = "westus3",
    data_partitions: Optional[List[str]] = None,
    ingress_mode: IngressMode = IngressMode.AZURE,
    dns_zone: str = "",
    ingress_prefix: str = "",
    acme_email: str = "",
    name_suffix: str = "",
) -> Config:
    return Config.from_env(
        env=env,
        name_suffix=name_suffix,
        profile=profile,
        repo_url=repo_url,
        repo_branch=branch,
        repo_tag=tag,
        location=location,
        data_partitions=data_partitions or ["opendes"],
        ingress_mode=ingress_mode,
        dns_zone=dns_zone,
        ingress_prefix=ingress_prefix,
        acme_email=acme_email,
    )


def _resolve_name_suffix(
    env: str,
    for_up: bool,
    requested_suffix: str | None = None,
) -> str:
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
        if requested_suffix is not None:
            raise typer.BadParameter("--name-suffix requires --env", param_hint="--name-suffix")
        return ""
    if requested_suffix is not None and requested_suffix != "":
        if not re.fullmatch(r"[a-z0-9]{5}", requested_suffix):
            raise typer.BadParameter(
                "must be exactly five lowercase alphanumeric characters",
                param_hint="--name-suffix",
            )

    from .azure_infra import detect_legacy_keyvault, read_rg_suffix_tag, write_rg_suffix_tag

    rg = f"spi-stack-{env}"
    existing = read_rg_suffix_tag(rg)
    if existing is not None:
        if requested_suffix is not None and requested_suffix != existing:
            raise typer.BadParameter(
                f"does not match the resource group's recorded suffix {existing!r}",
                param_hint="--name-suffix",
            )
        return existing

    # RG missing or RG present without our tag. Distinguish legacy from fresh.
    if detect_legacy_keyvault(rg, env):
        if requested_suffix:
            raise typer.BadParameter(
                "cannot be set on a legacy unsuffixed environment",
                param_hint="--name-suffix",
            )
        if for_up:
            write_rg_suffix_tag(rg, "")
        return ""

    suffix = requested_suffix if requested_suffix is not None else generate_name_suffix()
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


def _resolve_up_context(
    env: str,
    requested_suffix: str | None = None,
) -> Tuple[str, Dict[str, Any], Tuple[str, str]]:
    """Resolve read-only Azure identity state before suffix persistence."""
    from .azure_infra import _get_azure_account, _resolve_deployer_principal

    account = _get_azure_account()
    deployer_principal = _resolve_deployer_principal(account)
    name_suffix = _resolve_name_suffix(
        env,
        for_up=True,
        requested_suffix=requested_suffix,
    )
    return name_suffix, account, deployer_principal


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
        "minimal (middleware only, no OSDU services), or bare "
        "(infra + activated GitOps only; no middleware).",
    ),
    env: str = typer.Option(..., "--env", help="Environment name (required, e.g. dev1, test)"),
    repo_url: str = typer.Option(
        "https://github.com/Azure/osdu-spi-stack.git",
        "--repo",
        help="Git repository URL",
    ),
    branch: Optional[str] = typer.Option(
        None,
        "--branch",
        help="Git branch. Defaults to main when --tag is not used.",
    ),
    tag: Optional[str] = typer.Option(
        None,
        "--tag",
        help="Immutable release tag for the GitOps source, e.g. v0.6.0.",
    ),
    location: str = typer.Option(
        "westus3",
        "--location",
        help="Azure region (eastus2/centralus have shown API Server VNet Integration capacity constraints)",
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
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview Azure PaaS changes via Bicep what-if. Creates the resource group "
        "(required by what-if) but skips AKS, Kubernetes bootstrap, and GitOps.",
    ),
    refresh_images: Optional[bool] = typer.Option(
        None,
        "--refresh-images/--no-refresh-images",
        help="Refresh canonical service images, or preserve an existing image lock by default.",
    ),
    image_branch: str = typer.Option(
        DEFAULT_IMAGE_BRANCH,
        "--image-branch",
        help="OSDU image branch suffix to resolve from the community registry.",
    ),
    name_suffix: Optional[str] = typer.Option(
        None,
        "--name-suffix",
        help="Stable five-character resource-name suffix for a declared environment.",
    ),
):
    """Provision Azure infrastructure and deploy the OSDU SPI stack."""
    if tag and branch is not None:
        raise typer.BadParameter(
            "--tag cannot be combined with an explicit --branch",
            param_hint="--tag",
        )
    if tag and not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise typer.BadParameter(
            "must match vX.Y.Z",
            param_hint="--tag",
        )
    if tag and __version__ == "0.0.0+source":
        raise typer.BadParameter(
            "requires the released spi wheel matching the requested tag",
            param_hint="--tag",
        )
    if tag and tag.removeprefix("v") != __version__:
        raise typer.BadParameter(
            f"requires spi {tag.removeprefix('v')}, but this process is spi {__version__}",
            param_hint="--tag",
        )
    resolved_branch = branch or "main"

    if profile is None:
        profile = Profile.CORE

    if profile is Profile.BARE:
        if ingress_mode is not None:
            raise typer.BadParameter(
                "profile 'bare' deploys no ingress substrate; this option is not supported",
                param_hint="--ingress-mode",
            )
        if dns_zone:
            raise typer.BadParameter(
                "profile 'bare' deploys no ingress substrate; this option is not supported",
                param_hint="--dns-zone",
            )
        resolved_ingress = IngressMode.AZURE
    else:
        resolved_ingress = resolve_ingress_mode(ingress_mode)

    title = "[bold]SPI Stack[/bold] - Azure-native OSDU Software Stack"
    if dry_run:
        title += "\n[warning]DRY RUN: previewing Bicep changes only[/warning]"
    else:
        title += "\nAKS Automatic + Azure PaaS + Flux CD GitOps"

    console.print(Panel(title, border_style="cyan"))
    check_prerequisites(PREREQ_TOOLS)

    # Resolve the deployer before suffix persistence so an identity failure
    # cannot mutate an existing untagged resource group.
    name_suffix, azure_account, deployer_principal = _resolve_up_context(
        env,
        requested_suffix=name_suffix,
    )

    config = _build_config(
        profile=profile,
        env=env,
        repo_url=repo_url,
        branch=resolved_branch,
        tag=tag or "",
        location=location,
        data_partitions=data_partitions,
        ingress_mode=resolved_ingress,
        dns_zone=dns_zone,
        ingress_prefix=ingress_prefix,
        acme_email=resolve_acme_email(acme_email),
        name_suffix=name_suffix,
    )

    _show_config(config)

    try:
        from .deploy import deploy_azure

        deploy_azure(
            config,
            dry_run=dry_run,
            refresh_images=refresh_images,
            image_branch=image_branch,
            azure_account=azure_account,
            deployer_principal=deployer_principal,
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
            if config.repo_tag:
                console.print(
                    f"[dim]Environment is pinned to {config.repo_tag} with maintenance enabled. "
                    "Clear maintenance only after readiness and probes pass.[/dim]\n"
                )
            else:
                console.print(
                    "[dim]Environment is pinned to the resolved branch commit. "
                    "Run 'spi reconcile' to re-apply it when ready.[/dim]\n"
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

    # Read-only lookup so the displayed config table reflects what's in
    # Azure. cleanup_azure itself only deletes the resource group.
    name_suffix = _resolve_name_suffix(env, for_up=False)
    config = _build_config(env=env, name_suffix=name_suffix)
    _show_config(config)

    from .deploy import cleanup_azure

    cleanup_azure(config)


@app.command()
def connect(
    resource_group: str = typer.Option(
        ...,
        "--resource-group",
        help="Resource group containing the AKS cluster.",
    ),
    cluster: str = typer.Option(
        ...,
        "--cluster",
        help="AKS cluster name.",
    ),
):
    """Connect kubectl to an existing SPI Stack cluster."""
    check_prerequisites(["az", "kubectl", "kubelogin"])

    from .azure_infra import connect_cluster

    connect_cluster(resource_group, cluster)
    ctx = verify_spi_cluster()
    console.print(f"[success]Connected to SPI Stack cluster: {ctx}[/success]")


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
    output_json: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
):
    """Show deployment health and reconciliation progress."""
    if watch and output_json:
        raise typer.BadParameter(
            "--watch cannot be combined with --json",
            param_hint="--json",
        )

    ctx = verify_spi_cluster()

    from .status import StatusError, collect_status, render_status, status_exit_code, watch_status

    if watch:
        console.print(f"  [dim]Cluster context: {ctx}[/dim]")
        watch_status()
        return

    try:
        snapshot = collect_status()
    except StatusError as exc:
        if output_json:
            typer.echo(str(exc), err=True)
        else:
            console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=1)

    if output_json:
        print(json.dumps(snapshot.to_dict(), indent=2))
        raise typer.Exit(code=status_exit_code(snapshot))

    console.print(f"  [dim]Cluster context: {ctx}[/dim]")
    render_status(snapshot)


@maintenance_app.command("set")
def maintenance_set():
    """Prevent deploys while lifecycle maintenance is in progress."""
    verify_spi_cluster()

    from .deploy_record import DeployRecordError, set_maintenance

    try:
        set_maintenance(True)
    except DeployRecordError as exc:
        console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=1)
    console.print("[warning]Environment maintenance enabled.[/warning]")


@maintenance_app.command("clear")
def maintenance_clear():
    """Allow deploys after readiness and probes have passed."""
    verify_spi_cluster()

    from .deploy_record import DeployRecordError, set_maintenance

    try:
        set_maintenance(False)
    except DeployRecordError as exc:
        console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=1)
    console.print("[success]Environment maintenance cleared.[/success]")


@app.command()
def reconcile(
    suspend: bool = typer.Option(False, "--suspend", help="Freeze: stop Flux auto-reconciliation"),
    resume: bool = typer.Option(
        False, "--resume", help="Unfreeze: resume Flux auto-reconciliation"
    ),
    refresh_images: bool = typer.Option(
        False,
        "--refresh-images",
        help="Resolve current OSDU master image tags and update osdu-image-lock before reconciling.",
    ),
    image_branch: str = typer.Option(
        DEFAULT_IMAGE_BRANCH,
        "--image-branch",
        help="OSDU image branch suffix to resolve from the community registry.",
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

    ctx = verify_spi_cluster()
    console.print(f"  [dim]Cluster context: {ctx}[/dim]")

    if suspend:
        console.print("\n[bold]Suspending GitRepository...[/bold]")
        run_command(
            [
                "kubectl",
                "patch",
                "gitrepository",
                "osdu-spi-stack-system",
                "-n",
                "osdu-flux",
                "-p",
                '{"spec":{"suspend":true}}',
                "--type=merge",
            ],
            description="Suspend GitRepository (freeze reconciliation)",
        )
        console.print("[warning]GitRepository suspended.[/warning]")
        console.print("[dim]Run 'spi reconcile --resume' to unfreeze.[/dim]")
        return

    # spi-namespaces substitutes ISTIO_REVISION from spi-cluster-config, and
    # that Kustomization gates every layer above it. Refresh the ConfigMap
    # before any commit is applied so a cluster bootstrapped by an older CLI,
    # or one whose managed Istio revision was upgraded since the last deploy,
    # reconciles against the live revision instead of stalling on a missing
    # or stale substitution source.
    console.print("\n[bold]Refreshing cluster config for Flux substitution...[/bold]")
    create_istio_revision_configmap()

    if not refresh_images:
        _backfill_schema_load_lock(image_branch)

    if resume:
        console.print("\n[bold]Resuming GitRepository...[/bold]")
        run_command(
            [
                "kubectl",
                "patch",
                "gitrepository",
                "osdu-spi-stack-system",
                "-n",
                "osdu-flux",
                "-p",
                '{"spec":{"suspend":false}}',
                "--type=merge",
            ],
            description="Resume GitRepository (unfreeze reconciliation)",
        )
        console.print("[success]GitRepository resumed.[/success]")
        return

    if refresh_images:
        console.print("\n[bold]Resolving OSDU service images...[/bold]")
        try:
            resolved = resolve_image_lock(branch=image_branch)
        except ImageResolutionError as exc:
            console.print(f"[error]Unable to resolve OSDU service images: {exc}[/error]")
            raise typer.Exit(code=1)

        for name, image in resolved.items():
            console.print(
                f"  [success]{name}[/success] -> {image.repository.split('/')[-1]}:{image.tag[:12]}"
            )

        try:
            pins = apply_image_lock(
                resolved,
                image_branch,
                description="Refresh the osdu-image-lock ConfigMap",
            )
        except PinError as exc:
            console.print(f"[error]{exc}[/error]")
            console.print(
                "[error]Refusing to refresh the image lock while pin state is "
                "unreadable; a refresh could silently revert an active pin.[/error]"
            )
            raise typer.Exit(code=1)
        display_result("osdu-image-lock ConfigMap updated")
        for name, pin in sorted(pins.items()):
            console.print(
                f"  [warning]{name} stays pinned to MR !{pin.mr} ({pin.tag[:12]}); "
                f"release with 'spi service reset {name}'[/warning]"
            )

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
    console.print("\n[bold]Reconciling...[/bold]")

    run_command(
        [
            "kubectl",
            "annotate",
            "--overwrite",
            "gitrepository/osdu-spi-stack-system",
            "-n",
            "osdu-flux",
            f"reconcile.fluxcd.io/requestedAt={ts}",
        ],
        description="Trigger GitRepository reconciliation",
    )

    for name in [
        "osdu-spi-stack",
        "osdu-spi-stack-system-stack",
        "stack",
    ]:
        _trigger_kustomization(name, ts)

    core_kustomizations = [
        "spi-osdu-services",
        "spi-osdu-schema-load",
        "spi-osdu-reference",
    ]

    if refresh_images:
        # A resolved image tag has to reach schema-service before schema-load
        # is force-recreated against it, and schema-load has to finish before
        # reference re-seeds. Wait for each stage in order, but only for
        # profiles that actually declare these Kustomizations.
        console.print(
            "\n[bold]Waiting for image refresh to propagate in dependency order...[/bold]"
        )
        for name in core_kustomizations:
            if not _kustomization_exists(name):
                console.print(f"  [dim]Skipping {name} (not present in this profile).[/dim]")
                continue
            _reconcile_kustomization(name)
    else:
        for name in core_kustomizations:
            _trigger_kustomization(name, ts)

    console.print("[success]Reconciliation triggered.[/success]")


@service_app.command("pin")
def service_pin(
    service: str = typer.Argument(help="Service name, e.g. schema (see 'spi service list')."),
    mr: Optional[str] = typer.Option(
        None,
        "--mr",
        help="Merge request IID in the service's OSDU GitLab repository.",
    ),
    image: Optional[str] = typer.Option(
        None,
        "--image",
        help="Fork-built GHCR image as <repository>@sha256:<digest>; tags are rejected.",
    ),
    ephemeral: bool = typer.Option(
        False,
        "--ephemeral",
        help="Mark the pin CI-owned so the stale sweep may reclaim it (requires --run-id).",
    ),
    run_id: str = typer.Option(
        "", "--run-id", help="Owning GitHub Actions run id; required with --ephemeral."
    ),
    source_repo: str = typer.Option(
        "", "--source-repo", help="Repository that built the image, as <org>/<repo>."
    ),
    source_sha: str = typer.Option("", "--source-sha", help="Commit the image was built from."),
    source_run_url: str = typer.Option(
        "", "--source-run-url", help="Workflow run URL, recorded for display only."
    ),
):
    """Pin a service to an MR pipeline image or a fork-built GHCR digest."""
    if (mr is None) == (image is None):
        console.print("[error]Provide exactly one of --mr or --image.[/error]")
        raise typer.Exit(code=1)
    provenance = run_id or source_repo or source_sha or source_run_url
    if image is None and (ephemeral or provenance):
        console.print(
            "[error]--ephemeral and --run-id/--source-* apply only to --image pins.[/error]"
        )
        raise typer.Exit(code=1)
    if not ephemeral and provenance:
        console.print("[error]--run-id and --source-* require --ephemeral.[/error]")
        raise typer.Exit(code=1)

    ctx = verify_spi_cluster()
    console.print(f"  [dim]Cluster context: {ctx}[/dim]")

    if image is not None:
        try:
            pin = pin_service_image(
                service,
                image,
                ephemeral=ephemeral,
                run_id=run_id,
                source_repo=source_repo,
                source_sha=source_sha,
                source_run_url=source_run_url,
            )
        except (PinError, ImageResolutionError) as exc:
            console.print(f"[error]{exc}[/error]")
            raise typer.Exit(code=1)
        marker = f" (ephemeral, run {pin.run_id})" if pin.ephemeral else ""
        console.print(f"  [success]{service}[/success] pinned to {pin.digest[:19]}{marker}")
        console.print(f"[dim]Release with: spi service reset {service}[/dim]")
        return

    assert mr is not None
    try:
        results = pin_service(service, mr)
    except (PinError, ImageResolutionError) as exc:
        console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=1)

    for name, pin in results:
        console.print(
            f"  [success]{name}[/success] pinned to MR !{pin.mr} ({pin.branch} @ {pin.tag[:12]})"
        )
    console.print(f"[dim]Release with: spi service reset {service}[/dim]")


@service_app.command("verify")
def service_verify(
    service: str = typer.Argument(help="Service whose Deployment must run the digest."),
    image: str = typer.Option(
        ..., "--image", help="Expected image as <repository>@sha256:<digest>."
    ),
    deployment: Optional[str] = typer.Option(
        None,
        "--deployment",
        envvar="K8S_DEPLOYMENT_NAME",
        help="Deployment name; defaults to osdu-<service>.",
    ),
    container: Optional[str] = typer.Option(
        None,
        "--container",
        envvar="K8S_CONTAINER_NAME",
        help="Container name; defaults to the deployment name.",
    ),
    output_json: bool = typer.Option(
        False, "--json", help="Emit the outcome as a final machine-readable JSON line."
    ),
):
    """Assert the Deployment pod template and a running pod carry the digest.

    Exit 0 when verified, 2 with a typed reason when the workload does not
    provably run the digest, 1 when the cluster is unreachable.
    """
    ctx = _guarded_context(output_json)
    if not output_json:
        console.print(f"  [dim]Cluster context: {ctx}[/dim]")

    try:
        result = verify_service_image(service, image, deployment=deployment, container=container)
    except VerifyError as exc:
        if output_json:
            _emit_outcome("failed", exc.code, str(exc))
        else:
            console.print(f"[error]verify failed ({exc.code}): {exc}[/error]")
        raise typer.Exit(code=2)
    except (PinError, ImageResolutionError) as exc:
        if output_json:
            _emit_outcome("error", None, str(exc))
        else:
            console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=1)

    detail = f"{result.deployment} pod {result.pod} runs {result.image_id}"
    if output_json:
        _emit_outcome(
            "verified",
            None,
            detail,
            deployment=result.deployment,
            container=result.container,
            pod=result.pod,
            imageId=result.image_id,
        )
        return
    console.print(f"  [success]{service}[/success] verified: {detail}")


@service_app.command("reset")
def service_reset(
    service: Optional[str] = typer.Argument(None, help="Pinned service name to release."),
    if_run: str = typer.Option(
        "",
        "--if-run",
        help="Only reset while the live pin belongs to this workflow run; "
        "a non-matching pin is left standing (exit 2).",
    ),
    ephemeral: bool = typer.Option(
        False, "--ephemeral", help="With --stale-only: sweep abandoned ephemeral pins."
    ),
    stale_only: bool = typer.Option(
        False,
        "--stale-only",
        help="With --ephemeral: sweep only pins whose owning run ended or that aged out.",
    ),
    output_json: bool = typer.Option(
        False, "--json", help="Emit the outcome as a final machine-readable JSON line."
    ),
):
    """Release a service pin and restore its recorded canonical image."""

    def usage_error(message: str) -> typer.Exit:
        if output_json:
            _emit_outcome("error", None, message)
        else:
            console.print(f"[error]{message}[/error]")
        return typer.Exit(code=1)

    sweep = ephemeral or stale_only
    if sweep and not (ephemeral and stale_only):
        raise usage_error("--ephemeral and --stale-only must be used together.")
    if sweep and (service is not None or if_run):
        raise usage_error("The stale sweep takes no service argument or --if-run.")
    if not sweep and service is None:
        raise usage_error("Provide a service name, or --ephemeral --stale-only to sweep.")

    ctx = _guarded_context(output_json)
    if not output_json:
        console.print(f"  [dim]Cluster context: {ctx}[/dim]")

    if sweep:
        try:
            outcome = sweep_stale_ephemeral_pins()
        except (PinError, ImageResolutionError) as exc:
            if output_json:
                _emit_outcome("error", None, str(exc))
            else:
                console.print(f"[error]{exc}[/error]")
            raise typer.Exit(code=1)
        if output_json:
            _emit_outcome(
                "swept",
                None,
                f"swept {len(outcome.swept)}, kept {len(outcome.kept)}, "
                f"refresh required {len(outcome.refresh_required)}",
                swept=list(outcome.swept),
                kept=[{"service": name, "reason": why} for name, why in outcome.kept],
                refreshRequired=list(outcome.refresh_required),
            )
            return
        for name in outcome.swept:
            console.print(f"  [success]{name}[/success] stale pin swept; canonical image restored")
        for name, why in outcome.kept:
            console.print(f"  [dim]{name} kept: {why}[/dim]")
        for name in outcome.refresh_required:
            console.print(
                f"  [warning]{name} stale pin removed, but no canonical image was "
                "recorded[/warning]"
            )
        if outcome.refresh_required:
            console.print(
                "[warning]Run 'spi reconcile --refresh-images' now to resolve and apply "
                "canonical images.[/warning]"
            )
        if not (outcome.swept or outcome.kept or outcome.refresh_required):
            console.print("No ephemeral pins to sweep.")
        return

    assert service is not None
    try:
        result = reset_service(service, if_run=if_run)
    except ResetRefusedError as exc:
        if output_json:
            _emit_outcome("refused", exc.code, str(exc))
        else:
            console.print(f"[warning]reset refused ({exc.code}): {exc}[/warning]")
        raise typer.Exit(code=2)
    except (PinError, ImageResolutionError) as exc:
        if output_json:
            _emit_outcome("error", None, str(exc))
        else:
            console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=1)

    if output_json:
        parts = []
        if result.restored:
            parts.append(f"restored {', '.join(result.restored)} to canonical")
        if result.refresh_required:
            parts.append(
                f"removed {', '.join(result.refresh_required)} without a recorded "
                "canonical; run 'spi reconcile --refresh-images'"
            )
        _emit_outcome(
            "reset",
            None,
            "; ".join(parts),
            restored=list(result.restored),
            refreshRequired=list(result.refresh_required),
        )
        return

    for name in result.restored:
        console.print(f"  [success]{name}[/success] restored to canonical image")
    for name in result.refresh_required:
        console.print(
            f"  [warning]{name} pin removed, but no canonical image was recorded[/warning]"
        )
    if result.refresh_required:
        console.print(
            "[warning]Run 'spi reconcile --refresh-images' now to resolve and apply "
            "canonical images.[/warning]"
        )


@service_app.command("list")
def service_list():
    """Show services currently pinned away from their canonical images."""
    ctx = verify_spi_cluster()
    console.print(f"  [dim]Cluster context: {ctx}[/dim]")

    try:
        pins = live_pins()
    except PinError as exc:
        console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=1)
    if not pins:
        console.print("No services are pinned.")
        return

    table = Table(title="Pinned services")
    table.add_column("Service")
    table.add_column("Source")
    table.add_column("Image")
    table.add_column("Ephemeral")
    table.add_column("Pinned at")
    for name, pin in sorted(pins.items()):
        if pin.mr:
            source = f"MR !{pin.mr} ({pin.branch})"
            image_short = pin.tag[:12]
        else:
            source = pin.source_repo or "github"
            if pin.run_id:
                source += f" run {pin.run_id}"
            image_short = pin.digest[:19]
        table.add_row(name, source, image_short, "yes" if pin.ephemeral else "", pin.applied_at)
    console.print(table)


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

    try:
        rc = _update.run_upgrade(installer, wheel_url, display=not silent)
    except _update.UpdateError as exc:
        if silent:
            typer.echo(str(exc), err=True)
        else:
            console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=1)
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
