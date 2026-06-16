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

"""Ingress / DNS resolution and the spi-ingress-config ConfigMap.

Owns every decision that depends on the ingress mode:
  - CLI flag + env var precedence for ``--ingress-mode``/``--acme-email``.
  - Azure-mode FQDN (deterministic, computed from dns_label + region).
  - DNS-mode zone auto-discovery from the current subscription.
  - The ``spi-ingress-config`` ConfigMap that the Flux ingress profile
    consumes via postBuild substituteFrom.
"""

import json
import os
import subprocess
from typing import Optional

import typer

from .config import Config, IngressMode
from .console import console, display_result, display_yaml
from .shell import kubectl_apply_yaml, kubectl_json, resolve_command

ISTIO_INGRESS_NAMESPACE = "aks-istio-ingress"
# Istio with gatewayClassName=istio provisions a LoadBalancer Service
# named "<gateway-name>-istio" per Gateway CR. Our Gateway is "spi-gateway".
ISTIO_INGRESS_SERVICE = "spi-gateway-istio"


def resolve_ingress_mode(cli_flag: Optional[IngressMode]) -> IngressMode:
    """Resolve the ingress mode. Precedence: --flag > SPI_INGRESS_MODE env > default (azure)."""
    if cli_flag is not None:
        return cli_flag
    env_val = os.environ.get("SPI_INGRESS_MODE", "").strip().lower()
    if env_val in {m.value for m in IngressMode}:
        return IngressMode(env_val)
    if env_val:
        console.print(
            f"[warning]Invalid SPI_INGRESS_MODE '{env_val}'; falling back to 'azure'.[/warning]"
        )
    return IngressMode.AZURE


def resolve_acme_email(cli_value: str) -> str:
    """Precedence: --acme-email > SPI_ACME_EMAIL env > empty (later auto-derived)."""
    return cli_value or os.environ.get("SPI_ACME_EMAIL", "")


def discover_dns_zone() -> tuple:
    """Return (zone_name, resource_group) from the current Azure subscription.

    Lists zones; returns the single one if exactly one exists. Raises
    typer.Exit on zero or multiple (with an instructive message).
    """
    result = subprocess.run(
        resolve_command(["az", "network", "dns", "zone", "list", "-o", "json"]),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(
            "[error]Failed to list DNS zones. "
            "Check that 'az login' is current and you have reader rights.[/error]"
        )
        raise typer.Exit(code=1)
    try:
        zones = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        zones = []

    if not zones:
        console.print(
            "[error]No Azure DNS zones found in the current subscription.[/error]\n"
            "[info]Create a zone, or re-run with --ingress-mode azure to use "
            "the auto-FQDN mode (no DNS zone required).[/info]"
        )
        raise typer.Exit(code=1)
    if len(zones) > 1:
        names = ", ".join(z.get("name", "?") for z in zones)
        console.print(
            f"[error]Multiple DNS zones found in current subscription: {names}[/error]\n"
            "[info]Pass --dns-zone <name> to pick one.[/info]"
        )
        raise typer.Exit(code=1)
    return zones[0]["name"], zones[0]["resourceGroup"]


def compute_ingress_fqdn(dns_label: str, location: str) -> str:
    """Return the deterministic Azure-assigned FQDN for the Istio LB's PIP.

    The DNS label is applied by the Azure cloud controller when it sees the
    ``service.beta.kubernetes.io/azure-dns-label-name`` annotation on the
    LoadBalancer Service, which is set via the Gateway's
    ``spec.infrastructure.annotations`` (propagated by Istio to the
    generated Service) in the single-host TLS overlay. The AKS-provisioned
    PIPs live in the locked-down node resource group and cannot be patched
    directly via the deployer identity.
    """
    return f"{dns_label}.{location}.cloudapp.azure.com"


def get_ingress_ip() -> str:
    """Return the current IP (or hostname) of the Istio ingress LB. Empty if unresolved."""
    data = kubectl_json(["-n", ISTIO_INGRESS_NAMESPACE, "get", "svc", ISTIO_INGRESS_SERVICE])
    if not data:
        return ""
    ingresses = data.get("status", {}).get("loadBalancer", {}).get("ingress", [])
    if ingresses:
        return ingresses[0].get("ip", "") or ingresses[0].get("hostname", "")
    return ""


def resolve_post_deploy_inputs(config: Config) -> None:
    """Populate ingress_fqdn (azure) or dns_zone (dns) once the cluster is up.

    Mutates ``config`` in place. No-op in ip mode.
    """
    if config.ingress_mode == IngressMode.AZURE:
        config.ingress_fqdn = compute_ingress_fqdn(
            dns_label=config.dns_label,
            location=config.location,
        )
        display_result(f"Azure FQDN target: {config.ingress_fqdn}")
    elif config.ingress_mode == IngressMode.DNS:
        if not config.dns_zone:
            zone, rg = discover_dns_zone()
            config.dns_zone = zone
            config.dns_zone_rg = rg
            display_result(f"Using DNS zone: {zone} (rg: {rg})")


def create_ingress_config(
    config: Config, external_dns_client_id: str, tenant_id: str, gateway_ip: str
) -> None:
    """Write the spi-ingress-config ConfigMap in osdu-flux.

    The ConfigMap is consumed by Flux Kustomizations in the
    software/stacks/osdu/ingress/<mode>/ profile via postBuild substituteFrom.
    Keys vary by ingress mode; irrelevant keys are omitted to keep the
    ConfigMap self-documenting.
    """
    prefix = config.resolved_ingress_prefix
    data = {
        "INGRESS_MODE": config.ingress_mode.value,
        "GATEWAY_IP": gateway_ip or "",
        "TXT_OWNER_ID": config.cluster_name,
        "AZURE_TENANT_ID": tenant_id or "",
    }

    if config.ingress_mode == IngressMode.AZURE:
        data["INGRESS_FQDN"] = config.ingress_fqdn
        data["DNS_LABEL"] = config.dns_label
        data["ACME_EMAIL"] = config.acme_email or f"admin@{config.ingress_fqdn}"
    elif config.ingress_mode == IngressMode.DNS:
        data["DNS_ZONE"] = config.dns_zone
        data["DNS_ZONE_RG"] = config.dns_zone_rg
        data["INGRESS_PREFIX"] = prefix
        data["EXTERNAL_DNS_CLIENT_ID"] = external_dns_client_id or ""
        data["ACME_EMAIL"] = config.acme_email or f"admin@{config.dns_zone}"
        data["INGRESS_HOST_OSDU"] = f"{prefix}.{config.dns_zone}"
        data["INGRESS_HOST_KIBANA"] = f"{prefix}-kibana.{config.dns_zone}"
        data["INGRESS_HOST_AIRFLOW"] = f"{prefix}-airflow.{config.dns_zone}"
    # IP mode: only the four base keys above; no hostnames, no ACME.

    yaml_lines = [
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        "  name: spi-ingress-config",
        "  namespace: osdu-flux",
        "  labels:",
        "    app.kubernetes.io/managed-by: osdu-spi-stack",
        "data:",
    ]
    for key, value in sorted(data.items()):
        # Quote values that might look YAML-special (spaces, colons, etc).
        yaml_lines.append(f'  {key}: "{value}"')
    yaml_content = "\n".join(yaml_lines) + "\n"

    display_yaml(yaml_content, "ConfigMap: spi-ingress-config")
    kubectl_apply_yaml(yaml_content, "apply spi-ingress-config ConfigMap")
    display_result("spi-ingress-config ConfigMap created")
