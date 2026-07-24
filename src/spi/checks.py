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

"""Tool registry and prerequisite checking."""

import json
import platform
import subprocess
from typing import List, Optional, TypedDict

import typer

from .console import console

# ---------------------------------------------------------------------------
# Tool registry -- single source of truth for CLI prerequisites
# ---------------------------------------------------------------------------


class ToolInfo(TypedDict, total=False):
    check_args: list[str]
    check_cmd: list[str]
    description: str
    install: dict[str, str]


TOOL_REGISTRY: dict[str, ToolInfo] = {
    "az": {
        "check_args": ["--version"],
        "description": "Azure CLI",
        "install": {
            "darwin": "brew install azure-cli",
            "linux": "curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash",
            "windows": "winget install Microsoft.AzureCLI",
        },
    },
    "bicep": {
        # Bicep is bundled with recent az and invoked via `az bicep`.
        # check_cmd overrides the default [name] + check_args pattern so we
        # do not require a standalone bicep binary on PATH.
        "check_cmd": ["az", "bicep", "version"],
        "description": "Bicep compiler (bundled with Azure CLI)",
        "install": {
            "darwin": "az bicep install",
            "linux": "az bicep install",
            "windows": "az bicep install",
        },
    },
    "kubectl": {
        "check_args": ["version", "--client"],
        "description": "Kubernetes CLI",
        "install": {
            "darwin": "brew install kubectl",
            "linux": (
                'curl -LO "https://dl.k8s.io/release/'
                "$(curl -sL https://dl.k8s.io/release/stable.txt)"
                '/bin/linux/amd64/kubectl" && chmod +x kubectl'
                " && sudo mv kubectl /usr/local/bin/"
            ),
            "windows": "winget install Kubernetes.kubectl",
        },
    },
    "kubelogin": {
        "check_args": ["--version"],
        "description": "AAD exec plugin for kubectl (required by Azure RBAC for Kubernetes)",
        "install": {
            "darwin": "brew install Azure/kubelogin/kubelogin",
            "linux": "az aks install-cli",
            "windows": "az aks install-cli",
        },
    },
    "flux": {
        "check_args": ["--version"],
        "description": "Flux CD GitOps toolkit",
        "install": {
            "darwin": "brew install fluxcd/tap/flux",
            "linux": "curl -s https://fluxcd.io/install.sh | sudo bash",
            "windows": "winget install FluxCD.Flux",
        },
    },
    "helm": {
        "check_args": ["version", "--short"],
        "description": "Helm package manager",
        "install": {
            "darwin": "brew install helm",
            "linux": "curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash",
            "windows": "winget install Helm.Helm",
        },
    },
}


# All tools are required; Azure is the only target.
PREREQ_TOOLS: List[str] = list(TOOL_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def detect_platform() -> str:
    system = platform.system().lower()
    if system in ("darwin", "linux", "windows"):
        return system
    return "unknown"


def _is_windows() -> bool:
    return platform.system().lower() == "windows"


# ---------------------------------------------------------------------------
# Tool checking
# ---------------------------------------------------------------------------


def check_tool_status(name: str, check_args: Optional[list] = None) -> tuple:
    """Check if a tool is installed and capture version output."""
    info = TOOL_REGISTRY.get(name, {})
    # check_cmd (if present) is a full argv; otherwise build [name] + args
    cmd = info.get("check_cmd")
    if cmd is None:
        args = check_args or info.get("check_args", ["--version"])
        cmd = [name] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            shell=_is_windows(),
        )
        if result.returncode == 0:
            output = result.stdout.strip() or result.stderr.strip()
            version = output.split("\n")[0][:80] if output else "installed"
            return True, version
        return False, ""
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False, ""


def get_install_hint(tool_name: str) -> Optional[str]:
    info = TOOL_REGISTRY.get(tool_name, {})
    install = info.get("install", {})
    plat = detect_platform()
    return install.get(plat) or install.get("*")


# ---------------------------------------------------------------------------
# Pre-flight check used by ``spi up`` / ``spi down``
# ---------------------------------------------------------------------------


def check_prerequisites(tools: List[str]) -> None:
    """Verify that each tool is installed; exit on the first missing one."""
    console.print("\n[bold]Checking prerequisites...[/bold]")

    missing = []
    for tool in tools:
        info = TOOL_REGISTRY.get(tool, {})
        installed, _ = check_tool_status(tool, info.get("check_args"))
        if installed:
            console.print(f"  [success]{tool}[/success]")
        else:
            console.print(f"  [error]{tool} -- NOT FOUND[/error]")
            hint = get_install_hint(tool)
            if hint:
                console.print(f"    [info]Install: {hint}[/info]")
            missing.append(tool)

    if missing:
        console.print(f"\n[error]Missing required tools: {', '.join(missing)}[/error]")
        console.print("[dim]Run 'uv run spi check' for full details.[/dim]")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Full check run (``spi check``)
# ---------------------------------------------------------------------------


def run_checks() -> list:
    results = []
    for name, info in TOOL_REGISTRY.items():
        installed, version = check_tool_status(name, info.get("check_args"))
        entry = {
            "name": name,
            "description": info.get("description", ""),
            "installed": installed,
            "version": version if installed else None,
        }
        if not installed:
            entry["install"] = info.get("install", {})
            hint = get_install_hint(name)
            if hint:
                entry["install_cmd"] = hint
        results.append(entry)
    return results


def results_to_json(results: list) -> str:
    return json.dumps(
        {
            "platform": detect_platform(),
            "total": len(results),
            "installed": sum(1 for r in results if r["installed"]),
            "missing": sum(1 for r in results if not r["installed"]),
            "tools": results,
        },
        indent=2,
    )
