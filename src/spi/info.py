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

"""Cluster access information and endpoint display.

Reads the `spi-ingress-config` ConfigMap written by the CLI at bootstrap
and renders the right base URL / middleware UI table per ingress mode:

  - ip:    http://<gateway-ip>/api/...                   (no middleware UIs)
  - azure: https://<auto-fqdn>/api/...  +  /kibana/  /airflow/
  - dns:   https://<host-osdu>/api/...  +  <host-kibana>  <host-airflow>
"""

import base64
import json

from rich.panel import Panel
from rich.table import Table

from .azure_infra import _cosmos_sql_name, _sb_name, _storage_name
from .config import BASE_NAME
from .console import console
from .shell import kubectl_json

# OSDU API services exposed via HTTPRoutes. Order preserved for display.
_OSDU_API_PATHS = [
    ("partition", "/api/partition/v1/"),
    ("entitlements", "/api/entitlements/v2/"),
    ("legal", "/api/legal/v1/"),
    ("schema", "/api/schema-service/v1/"),
    ("storage", "/api/storage/v2/"),
    ("search", "/api/search/v2/"),
    ("indexer", "/api/indexer/v2/"),
    ("indexer-queue", "/api/indexer-queue/v1/"),
    ("file", "/api/file/v2/"),
    ("workflow", "/api/workflow/v1/"),
    ("unit", "/api/unit/v3/"),
    ("crs-catalog", "/api/crs/catalog/v2/"),
    ("crs-conversion", "/api/crs/converter/v2/"),
]


def _secret_value(namespace: str, name: str, key: str) -> str:
    """Read a base64-decoded value from a k8s Secret. Empty string on error."""
    data = kubectl_json(["get", "secret", name, "-n", namespace])
    if not data:
        return ""
    raw = data.get("data", {}).get(key, "")
    if not raw:
        return ""
    try:
        return base64.b64decode(raw).decode()
    except (ValueError, UnicodeDecodeError):
        return ""


def _read_ingress_config() -> dict:
    """Read the CLI-written spi-ingress-config ConfigMap. Empty dict if missing."""
    data = kubectl_json(["get", "configmap", "spi-ingress-config", "-n", "osdu-flux"])
    if not data:
        return {}
    return data.get("data", {}) or {}


def _read_osdu_config() -> dict:
    """Read the osdu-config ConfigMap from the osdu namespace. Empty if missing."""
    data = kubectl_json(["get", "configmap", "osdu-config", "-n", "osdu"])
    if not data:
        return {}
    return data.get("data", {}) or {}


def _read_flux_extension_values() -> dict:
    """Read Azure metadata injected by the AKS Flux extension."""
    data = kubectl_json(["get", "configmap", "flux-extension-values", "-n", "osdu-flux"])
    if not data:
        return {}
    return data.get("data", {}) or {}


def _read_partitions_list() -> list:
    """Extract the partition list from the spi-init-values ConfigMap.

    The CLI writes this ConfigMap from the --partition flags during bootstrap
    (see deploy._create_spi_init_values). The Helm chart's per-partition init
    Jobs render from the same source, so this is the authoritative list.

    Returns ["opendes"] as a safe fallback when the ConfigMap is missing — a
    cluster pre-bootstrap or one deployed without the CLI still renders sanely.
    """
    data = kubectl_json(["get", "configmap", "spi-init-values", "-n", "osdu-flux"])
    if not data:
        return []
    values_yaml = (data.get("data") or {}).get("values.yaml", "")
    return _parse_partitions_from_values_yaml(values_yaml)


def _parse_partitions_from_values_yaml(text: str) -> list:
    """Pull the partition names out of the small known-shape values.yaml blob.

    Avoids a yaml dep at the CLI runtime path; the ConfigMap is CLI-written
    so its shape is fixed: ``partitions:\\n  - p1\\n  - p2``.
    """
    in_partitions = False
    out: list = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped == "partitions:":
            in_partitions = True
            continue
        if in_partitions:
            if stripped.startswith("- "):
                out.append(stripped[2:].strip())
            elif stripped and not stripped.startswith("-"):
                # Hit a sibling key; stop collecting.
                break
    return out


def _env_from_resource_group(rg: str) -> str:
    """Extract the --env flag value from the resource group name.

    Naming follows ``{BASE_NAME}-{env}`` (config.from_env). When the user
    deployed without --env the RG is just ``BASE_NAME`` and env is empty.
    """
    if not rg:
        return ""
    prefix = f"{BASE_NAME}-"
    return rg[len(prefix) :] if rg.startswith(prefix) else ""


def _build_partitions_rows(partitions: list, env: str) -> list:
    """(Partition, Cosmos Account, Service Bus Namespace, Storage Account) rows.

    Resource names are derived locally via the same helpers azure_infra.py
    uses to build the Bicep parameters. No Azure round-trip required —
    naming is the contract.
    """
    rows = []
    for i, p in enumerate(partitions):
        label = f"{p} (primary)" if i == 0 else p
        rows.append(
            (
                label,
                _cosmos_sql_name(p, env),
                _sb_name(p, env),
                _storage_name("osdu" + env + p, ""),
            )
        )
    return rows


def _compute_endpoints(cfg: dict) -> tuple:
    """Return (mode, base_url, endpoints_dict, middleware_dict).

    mode: "ip" | "azure" | "dns"
    base_url: full URL for the primary host, or "" if not yet known.
    endpoints_dict: {"partition": "http(s)://...", ...} for all 13 services.
    middleware_dict: {"Kibana": url, "Airflow": url} — empty in ip mode.
    """
    mode = (cfg.get("INGRESS_MODE") or "").lower()

    if mode == "azure":
        fqdn = cfg.get("INGRESS_FQDN", "")
        base = f"https://{fqdn}" if fqdn else ""
        endpoints = {svc: f"{base}{path}" for svc, path in _OSDU_API_PATHS} if base else {}
        middleware = {"Kibana": f"{base}/kibana/", "Airflow": f"{base}/airflow/"} if base else {}
        return mode, base, endpoints, middleware

    if mode == "dns":
        osdu_host = cfg.get("INGRESS_HOST_OSDU", "")
        kibana_host = cfg.get("INGRESS_HOST_KIBANA", "")
        airflow_host = cfg.get("INGRESS_HOST_AIRFLOW", "")
        base = f"https://{osdu_host}" if osdu_host else ""
        endpoints = {svc: f"{base}{path}" for svc, path in _OSDU_API_PATHS} if base else {}
        middleware = {}
        if kibana_host:
            middleware["Kibana"] = f"https://{kibana_host}/"
        if airflow_host:
            middleware["Airflow"] = f"https://{airflow_host}/"
        return mode, base, endpoints, middleware

    # Fallback: ip mode or no ConfigMap yet.
    ip = cfg.get("GATEWAY_IP", "") or _discover_gateway_ip()
    base = f"http://{ip}" if ip else ""
    endpoints = {svc: f"{base}{path}" for svc, path in _OSDU_API_PATHS} if base else {}
    return "ip", base, endpoints, {}


def _discover_gateway_ip() -> str:
    """Fallback: find the Istio ingress LB IP when the ConfigMap is missing."""
    for ns in ["aks-istio-ingress", "istio-system"]:
        data = kubectl_json(["get", "svc", "-n", ns])
        if not data or "items" not in data:
            continue
        for svc in data["items"]:
            if svc.get("spec", {}).get("type") != "LoadBalancer":
                continue
            for ing in svc.get("status", {}).get("loadBalancer", {}).get("ingress", []):
                ip = ing.get("ip") or ing.get("hostname")
                if ip:
                    return ip
    return ""


def _get_live_credentials() -> list:
    """Return [(service, username, password), ...] for the stack's in-cluster
    middleware. Entries with both user and password empty are dropped so the
    table only shows credentials that actually exist on this cluster."""
    pg_user = _secret_value("platform", "postgresql-airflow-credentials", "username")
    pg_pw = _secret_value("platform", "postgresql-airflow-credentials", "password")
    pg_su_user = _secret_value("platform", "postgresql-superuser-credentials", "username")
    pg_su_pw = _secret_value("platform", "postgresql-superuser-credentials", "password")
    elastic_pw = _secret_value("platform", "elasticsearch-es-elastic-user", "elastic")
    redis_pw = _secret_value("platform", "redis-credentials", "password")
    airflow_pw = _secret_value("platform", "airflow-webserver-credentials", "password")

    rows = [
        ("PostgreSQL (Airflow)", pg_user, pg_pw),
        ("PostgreSQL (superuser)", pg_su_user, pg_su_pw),
        ("Elasticsearch", "elastic" if elastic_pw else "", elastic_pw),
        ("Redis", "", redis_pw),
        ("Airflow", "admin" if airflow_pw else "", airflow_pw),
    ]
    return [(svc, u, p) for svc, u, p in rows if u or p]


def _build_endpoints_table(mode: str, base: str, middleware: dict) -> list:
    """Shape (Service, URL, Note) rows for the unified Endpoints table."""
    rows = []
    if not base:
        return rows

    if mode == "ip":
        rows.append(("Gateway (HTTP)", base, "HTTP only; no TLS"))
    else:
        rows.append(("Gateway (HTTPS)", base, "Let's Encrypt"))

    for name, url in middleware.items():
        rows.append((name, url, ""))
    return rows


def _build_internal_services() -> list:
    """(Service, Cluster Address, Port-Forward Command) rows.

    Port-forward commands target the same cluster addresses via kubectl
    port-forward so callers can run them verbatim.
    """
    return [
        (
            "Elasticsearch",
            "elasticsearch-es-http.platform.svc:9200",
            "kubectl port-forward -n platform svc/elasticsearch-es-http 9200:9200",
        ),
        (
            "Redis",
            "redis-master.platform.svc:6380 (TLS)",
            "kubectl port-forward -n platform svc/redis-master 6380:6379",
        ),
        (
            "PostgreSQL",
            "postgresql-rw.platform.svc:5432 (Airflow only)",
            "kubectl cnpg psql postgresql -n platform",
        ),
    ]


def render_info(show_secrets: bool = False, show_apis: bool = False, output_json: bool = False):
    from .guard import get_suspend_status

    cfg = _read_ingress_config()
    osdu = _read_osdu_config()
    azure_ext = _read_flux_extension_values()
    mode, base, endpoints, middleware = _compute_endpoints(cfg)

    rg = azure_ext.get("AZURE_RESOURCE_GROUP", "")
    env = _env_from_resource_group(rg)
    partitions = _read_partitions_list()
    partition_rows = _build_partitions_rows(partitions, env)

    info = {
        "ingress_mode": mode,
        "base_url": base,
        "endpoints": endpoints,
        "middleware_uis": middleware,
        "internal_services": {svc: addr for svc, addr, _hint in _build_internal_services()},
        "azure": {
            "resource_group": rg,
            "region": azure_ext.get("AZURE_REGION", ""),
            "gateway_ip": cfg.get("GATEWAY_IP", ""),
            "fqdn": cfg.get("INGRESS_FQDN", ""),
            "keyvault": osdu.get("KEYVAULT_NAME", ""),
            "cosmos_endpoint": osdu.get("PRIMARY_COSMOSDB_ENDPOINT", ""),
            "storage_account": osdu.get("PRIMARY_STORAGE_ACCOUNT_NAME", ""),
            "servicebus": osdu.get("PRIMARY_SERVICEBUS_NAMESPACE", ""),
        },
        "partitions": [
            {
                "name": partitions[i],
                "primary": i == 0,
                "cosmos_account": cosmos,
                "servicebus_namespace": sb,
                "storage_account": storage,
            }
            for i, (_label, cosmos, sb, storage) in enumerate(partition_rows)
        ],
        "suspended": get_suspend_status(),
    }

    if show_secrets:
        creds_list = _get_live_credentials()
        info["credentials"] = [
            {"service": svc, "username": u, "password": p} for svc, u, p in creds_list
        ]
    else:
        creds_list = []

    if output_json:
        print(json.dumps(info, indent=2))
        return

    # Human-readable display
    console.print(Panel("[bold]SPI Stack Access Info[/bold]", border_style="cyan"))

    if info["suspended"]:
        console.print(
            Panel(
                "[bold yellow]GitRepository is SUSPENDED[/bold yellow] -- "
                "Flux will not auto-reconcile new commits.\n"
                "[dim]Run 'uv run spi reconcile --resume' to unfreeze.[/dim]",
                border_style="yellow",
            )
        )

    console.print(f"\n  [ready]Ingress mode:[/ready] {mode or 'unknown'}")
    if base:
        console.print(f"  [ready]Base URL:    [/ready] {base}")
    else:
        console.print(
            "  [warning]Base URL not yet available -- ingress is still "
            "provisioning. Re-run 'spi info' in a minute.[/warning]"
        )
    if endpoints and not show_apis:
        console.print(
            f"  [ready]OSDU APIs:   [/ready] "
            f"[dim]{len(endpoints)} services at /api/* "
            "(--show-apis to list)[/dim]"
        )
    console.print()

    endpoint_rows = _build_endpoints_table(mode, base, middleware)
    if endpoint_rows:
        table = Table(title="Endpoints", border_style="cyan", expand=True)
        table.add_column("Service", style="bold")
        table.add_column("URL", style="cyan")
        table.add_column("Note", style="dim")
        for name, url, note in endpoint_rows:
            table.add_row(name, url, note)
        console.print(table)
        console.print()

    if endpoints and show_apis:
        table = Table(title="OSDU API Endpoints", border_style="cyan", expand=True)
        table.add_column("Service", style="bold")
        table.add_column("URL", style="cyan")
        for svc, url in endpoints.items():
            table.add_row(svc, url)
        console.print(table)
        console.print()

    az = info["azure"]
    if az["resource_group"] or az["gateway_ip"] or az["keyvault"]:
        lines = []
        if az["resource_group"]:
            region = f" ({az['region']})" if az["region"] else ""
            lines.append(f"[bold]Resource Group:[/bold] {az['resource_group']}{region}")
        if az["gateway_ip"]:
            lines.append(f"[bold]Gateway IP:[/bold] {az['gateway_ip']}")
        if az["fqdn"]:
            lines.append(f"[bold]Gateway FQDN:[/bold] {az['fqdn']}")
        if az["keyvault"]:
            lines.append(f"[bold]Key Vault:[/bold] {az['keyvault']}")
        if az["cosmos_endpoint"]:
            lines.append(f"[bold]Primary Cosmos DB:[/bold] {az['cosmos_endpoint']}")
        if az["storage_account"]:
            lines.append(f"[bold]Common Storage:[/bold] {az['storage_account']}")
        if az["servicebus"]:
            lines.append(f"[bold]Primary Service Bus:[/bold] {az['servicebus']}")
        console.print(Panel("\n".join(lines), title="Azure", border_style="cyan"))
        console.print()

    if partition_rows:
        ptable = Table(title="Partitions", border_style="cyan", expand=True)
        ptable.add_column("Partition", style="bold")
        ptable.add_column("Cosmos Account", style="cyan")
        ptable.add_column("Service Bus Namespace", style="cyan")
        ptable.add_column("Storage Account", style="cyan")
        for label, cosmos, sb, storage in partition_rows:
            ptable.add_row(label, cosmos, sb, storage)
        console.print(ptable)
        console.print()

    table = Table(title="Internal Services (use port-forward)", border_style="cyan", expand=True)
    table.add_column("Service", style="bold")
    table.add_column("Cluster Address", style="cyan")
    table.add_column("Port-Forward Command", style="yellow")
    for name, addr, hint in _build_internal_services():
        table.add_row(name, addr, hint)
    console.print(table)
    console.print()

    if show_secrets:
        console.print(
            Panel(
                "[bold yellow]Dev/Test Notice[/bold yellow]\n"
                "These are live cluster credentials intended for local and non-production use.",
                border_style="yellow",
            )
        )
        console.print()
        if creds_list:
            table = Table(title="Credentials", border_style="cyan", expand=True)
            table.add_column("Service", style="bold")
            table.add_column("Username")
            table.add_column("Password", style="yellow")
            for svc, user, pw in creds_list:
                table.add_row(svc, user, pw)
            console.print(table)
            console.print()
        else:
            console.print("[dim]No credentials found -- secrets may not be deployed yet.[/dim]\n")
    else:
        console.print(
            "[dim]Credentials hidden by default. Re-run with '--show-secrets' "
            "for dev/test access details.[/dim]\n"
        )
