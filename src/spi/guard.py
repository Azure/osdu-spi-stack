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

"""Cluster identity guard.

Verifies that the current kubectl context points at an spi-stack cluster
before status/info/reconcile modify it. Set ``SPI_SKIP_GUARD=1`` to bypass.
"""

import os
import subprocess
from typing import Any, Dict, Optional

import typer

from .config import BASE_NAME
from .console import console
from .shell import kubectl_json, resolve_command

SPI_GITREPOSITORY = "osdu-spi-stack-system"
# flux.bicep declares the Flux config in flux-system, but a stack may place its
# Flux resources (GitRepository, Kustomizations, HelmReleases) in a dedicated
# namespace such as osdu-flux. resolve_flux_namespace() reads the live cluster;
# this constant is only the fallback when the GitRepository cannot be located.
DEFAULT_FLUX_NAMESPACE = "osdu-flux"


def find_spi_gitrepository() -> Optional[Dict[str, Any]]:
    """Return the osdu-spi-stack-system GitRepository object, searched across all
    namespaces, or None if absent.

    Namespace-agnostic so the CLI works whether the stack's Flux resources live
    in flux-system or a dedicated namespace such as osdu-flux.
    """
    data = kubectl_json(["get", "gitrepository", "-A", "-o", "json"])
    if not data:
        return None
    for item in data.get("items", []):
        if item.get("metadata", {}).get("name") == SPI_GITREPOSITORY:
            return item
    return None


def resolve_flux_namespace(default: str = DEFAULT_FLUX_NAMESPACE) -> str:
    """Namespace where the SPI Stack's Flux resources live.

    Read from the live GitRepository, falling back to ``default`` when it cannot
    be located (e.g. before the Flux CRDs are installed).
    """
    gr = find_spi_gitrepository()
    if gr:
        ns = gr.get("metadata", {}).get("namespace")
        if ns:
            return ns
    return default


def _get_current_context() -> str:
    """Return the current kubectl context name, or empty string on failure."""
    result = subprocess.run(
        resolve_command(["kubectl", "config", "current-context"]),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _is_spi_context(context: str) -> bool:
    return context.startswith(BASE_NAME)


def _has_spi_fingerprint() -> bool:
    """Check if the cluster has the osdu-spi-stack-system GitRepository.

    Falls back to the AKS Flux configuration via ``az`` when the Flux CRDs
    are not yet installed (e.g., right after ``spi up`` and before the
    extension has installed them).
    """
    if find_spi_gitrepository() is not None:
        return True

    ctx = _get_current_context()
    cluster_name = ctx if ctx else ""
    if not cluster_name:
        return False
    # Resource group matches cluster name for spi-stack deployments
    result = subprocess.run(
        resolve_command(
            [
                "az",
                "k8s-configuration",
                "flux",
                "show",
                "--resource-group",
                cluster_name,
                "--cluster-name",
                cluster_name,
                "--cluster-type",
                "managedClusters",
                "--name",
                "osdu-spi-stack-system",
                "--query",
                "provisioningState",
                "--output",
                "tsv",
            ]
        ),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def verify_spi_cluster() -> str:
    """Verify the current kubectl context points to an spi-stack cluster.

    Returns the context name on success. Exits with an error if the cluster
    does not appear to be an spi-stack deployment. Set ``SPI_SKIP_GUARD=1``
    to bypass this check.
    """
    if os.environ.get("SPI_SKIP_GUARD", "") == "1":
        ctx = _get_current_context() or "unknown"
        console.print(
            f"  [warning]Cluster guard bypassed (SPI_SKIP_GUARD=1), context: {ctx}[/warning]"
        )
        return ctx

    ctx = _get_current_context()
    if not ctx:
        console.print("[error]Cannot determine kubectl context.[/error]")
        console.print("[dim]Make sure your kubeconfig is set and the cluster is running.[/dim]")
        raise typer.Exit(code=1)

    if not _is_spi_context(ctx):
        console.print(
            f"[error]Current context '{ctx}' does not look like an spi-stack cluster.[/error]"
        )
        console.print(f"[dim]Expected a context starting with '{BASE_NAME}'.[/dim]")
        console.print("[dim]If this is intentional, set SPI_SKIP_GUARD=1 to bypass.[/dim]")
        raise typer.Exit(code=1)

    if not _has_spi_fingerprint():
        console.print(
            f"[error]Context '{ctx}' is set, but the cluster has no spi-stack deployment.[/error]"
        )
        console.print(
            "[dim]The osdu-spi-stack-system GitRepository was not found on the cluster.[/dim]"
        )
        console.print(
            "[dim]Run 'uv run spi up' to deploy, or set SPI_SKIP_GUARD=1 to bypass.[/dim]"
        )
        raise typer.Exit(code=1)

    return ctx


def get_suspend_status() -> bool:
    """Check if the Flux GitRepository source is suspended."""
    gr = find_spi_gitrepository()
    if not gr:
        return False
    return bool(gr.get("spec", {}).get("suspend", False))
