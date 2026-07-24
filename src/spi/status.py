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

"""Deployment status dashboard."""

import time
from datetime import datetime, timezone
from typing import List, Optional

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .console import console
from .shell import kubectl_json


def status_icon(ready: bool, message: str = "") -> Text:
    if ready:
        return Text("Ready", style="ready")
    if "progress" in message.lower() or "reconcil" in message.lower():
        return Text("Progressing", style="notready")
    if message:
        return Text(message[:40], style="failed")
    return Text("Not Ready", style="notready")


def age_str(timestamp: str) -> str:
    if not timestamp:
        return ""
    seconds = age_seconds(timestamp)
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


def age_seconds(timestamp: str) -> Optional[int]:
    if not timestamp:
        return None
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - ts).total_seconds())
    except Exception:
        return None


STUCK_PHASES = {"Pending", "ContainerCreating", "PodInitializing"}
STUCK_THRESHOLD_SECONDS = 300


def _duration(start: str, end: str) -> str:
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return _fmt_seconds(int((e - s).total_seconds()))
    except Exception:
        return ""


def _fmt_seconds(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


def short_image(image: str) -> str:
    """Extract short image name:tag from a full image reference."""
    return image.rsplit("/", 1)[-1]


def get_kustomization_table() -> Table:
    table = Table(title="Flux Kustomizations", border_style="cyan", expand=True)
    table.add_column("Name", style="bold")
    table.add_column("Layer", justify="center")
    table.add_column("Status")
    table.add_column("Message")
    table.add_column("Age", justify="right")

    data = kubectl_json(["get", "kustomizations", "-n", "osdu-flux"])
    if not data or "items" not in data:
        table.add_row("[dim]No kustomizations found[/dim]", "", "", "", "")
        return table

    items = sorted(
        data["items"],
        key=lambda x: (
            x.get("metadata", {}).get("labels", {}).get("spi-stack.layer", "9"),
            x.get("metadata", {}).get("name", ""),
        ),
    )

    for item in items:
        name = item.get("metadata", {}).get("name", "")
        labels = item.get("metadata", {}).get("labels", {})
        layer = labels.get("spi-stack.layer", "-")

        conditions = item.get("status", {}).get("conditions", [])
        ready_cond = next((c for c in conditions if c.get("type") == "Ready"), {})
        is_ready = ready_cond.get("status") == "True"
        message = ready_cond.get("message", "")
        reason = ready_cond.get("reason", "")

        if len(message) > 60:
            message = message[:57] + "..."

        table.add_row(
            name,
            f"L{layer}" if layer != "-" else "-",
            status_icon(is_ready, reason),
            message,
            age_str(ready_cond.get("lastTransitionTime", "")),
        )
    return table


def get_helmrelease_table() -> Table:
    table = Table(title="Helm Releases", border_style="cyan", expand=True)
    table.add_column("Name", style="bold")
    table.add_column("Chart")
    table.add_column("Version")
    table.add_column("Status")
    table.add_column("Message")

    data = kubectl_json(["get", "helmreleases", "-A"])
    if not data or "items" not in data:
        table.add_row("[dim]No HelmReleases found[/dim]", "", "", "", "")
        return table

    for item in sorted(data["items"], key=lambda x: x["metadata"]["name"]):
        name = item["metadata"]["name"]
        history = item.get("status", {}).get("history") or []
        last = history[0] if history else {}
        spec_chart = item.get("spec", {}).get("chart", {}).get("spec", {})
        # Prefer the resolved chart name / version from the most recent Helm
        # release history entry. Falls back to spec for releases that haven't
        # completed a first install yet (where history is empty).
        chart = last.get("chartName") or spec_chart.get("chart", "")
        version = last.get("chartVersion") or spec_chart.get("version", "")

        conditions = item.get("status", {}).get("conditions", [])
        ready_cond = next((c for c in conditions if c.get("type") == "Ready"), {})
        is_ready = ready_cond.get("status") == "True"
        message = ready_cond.get("message", "")
        reason = ready_cond.get("reason", "")
        if len(message) > 50:
            message = message[:47] + "..."

        table.add_row(name, chart, version, status_icon(is_ready, reason), message)
    return table


def get_custom_resources(platform_ns: str = "platform") -> Table:
    table = Table(title="Key Resources", border_style="cyan", expand=True)
    table.add_column("Resource", style="bold")
    table.add_column("Namespace")
    table.add_column("Status")
    table.add_column("Details")

    cnpg = kubectl_json(["get", "clusters.postgresql.cnpg.io", "-n", platform_ns])
    if cnpg and cnpg.get("items"):
        for item in cnpg["items"]:
            name = item["metadata"]["name"]
            phase = item.get("status", {}).get("phase", "Unknown")
            instances = item.get("status", {}).get("readyInstances", 0)
            target = item.get("spec", {}).get("instances", 1)
            is_ready = phase == "Cluster in healthy state" or (
                instances == target and instances > 0
            )
            table.add_row(
                f"pg/{name}",
                platform_ns,
                status_icon(is_ready, phase),
                f"{instances}/{target} instances" if target else phase,
            )

    es = kubectl_json(["get", "elasticsearches.elasticsearch.k8s.elastic.co", "-n", platform_ns])
    if es and es.get("items"):
        for item in es["items"]:
            name = item["metadata"]["name"]
            phase = item.get("status", {}).get("health", "unknown")
            avail = item.get("status", {}).get("availableNodes", 0)
            desired = item.get("status", {}).get(
                "expectedNodes",
                sum(ns.get("count", 0) for ns in item.get("spec", {}).get("nodeSets", [])),
            )
            is_ready = phase == "green" and avail == desired
            table.add_row(
                f"es/{name}",
                platform_ns,
                status_icon(is_ready, phase),
                f"{avail}/{desired} nodes, health={phase}",
            )

    if not table.rows:
        table.add_row("[dim]No custom resources found yet[/dim]", "", "", "")
    return table


_PARTITION_INIT_COMPONENTS = {"partition-init", "entitlements-init"}


def _job_status_cell(status_obj: dict) -> Text:
    succeeded = status_obj.get("succeeded", 0)
    failed = status_obj.get("failed", 0)
    active = status_obj.get("active", 0)
    if succeeded > 0:
        return Text("Complete", style="ready")
    if active > 0:
        return Text("Running", style="notready")
    if failed > 0:
        return Text(f"Failed ({failed})", style="failed")
    return Text("Pending", style="notready")


def _job_duration(status_obj: dict) -> str:
    start = status_obj.get("startTime", "")
    completion = status_obj.get("completionTime", "")
    if start and completion:
        return _duration(start, completion)
    if start:
        try:
            ts = datetime.fromisoformat(start.replace("Z", "+00:00"))
            elapsed = int((datetime.now(timezone.utc) - ts).total_seconds())
            return _fmt_seconds(elapsed) + "..."
        except Exception:
            return ""
    return ""


def get_partition_init_table(jobs: List[dict]) -> Optional[Table]:
    """Show partition + entitlements bootstrap Jobs grouped by partition.

    Filters by ``app.kubernetes.io/component in (partition-init,
    entitlements-init)`` and groups by the ``osdu.spi/partition`` label
    emitted by software/charts/osdu-spi-init.
    """
    rows = []
    for job in jobs:
        labels = job.get("metadata", {}).get("labels", {}) or {}
        component = labels.get("app.kubernetes.io/component", "")
        partition = labels.get("osdu.spi/partition", "")
        if component not in _PARTITION_INIT_COMPONENTS or not partition:
            continue
        rows.append((partition, component, job))

    if not rows:
        return None

    table = Table(title="Partition Bootstrap", border_style="cyan", expand=True)
    table.add_column("Partition", style="bold")
    table.add_column("Component")
    table.add_column("Status")
    table.add_column("Duration", justify="right")
    table.add_column("Age", justify="right")

    for partition, component, job in sorted(rows, key=lambda r: (r[0], r[1])):
        status_obj = job.get("status", {})
        created = job.get("metadata", {}).get("creationTimestamp", "")
        table.add_row(
            partition,
            component,
            _job_status_cell(status_obj),
            _job_duration(status_obj),
            age_str(created),
        )
    return table


def _fetch_jobs(namespaces: List[str]) -> List[dict]:
    data = kubectl_json(["get", "jobs", "-A"])
    if not data or not data.get("items"):
        return []
    target = set(namespaces)
    return [j for j in data["items"] if j["metadata"].get("namespace") in target]


def get_jobs_table(namespaces: List[str]) -> Optional[Table]:
    """Show non-partition bootstrap Jobs across the stack's namespaces.

    Partition + entitlements init Jobs are pulled out into
    ``get_partition_init_table`` so multi-partition deploys present a
    per-partition view; remaining one-shot Jobs (schema-load, etc.)
    appear here.
    """
    jobs = _fetch_jobs(namespaces)
    generic = [
        j
        for j in jobs
        if (j.get("metadata", {}).get("labels") or {}).get("app.kubernetes.io/component")
        not in _PARTITION_INIT_COMPONENTS
    ]
    if not generic:
        return None

    table = Table(title="Bootstrap Jobs", border_style="cyan", expand=True)
    table.add_column("Job", style="bold")
    table.add_column("Namespace", style="dim")
    table.add_column("Status")
    table.add_column("Duration", justify="right")
    table.add_column("Age", justify="right")

    for job in sorted(generic, key=lambda j: j["metadata"]["name"]):
        name = job["metadata"]["name"]
        ns = job["metadata"]["namespace"]
        status_obj = job.get("status", {})
        created = job["metadata"].get("creationTimestamp", "")
        table.add_row(
            name,
            ns,
            _job_status_cell(status_obj),
            _job_duration(status_obj),
            age_str(created),
        )
    return table if table.rows else None


def get_pod_table(namespace: str, title: str) -> Table:
    table = Table(title=title, border_style="cyan", expand=True)
    table.add_column("Pod", style="bold")
    table.add_column("Image", style="dim")
    table.add_column("Ready", justify="center")
    table.add_column("Status")
    table.add_column("Restarts", justify="right")
    table.add_column("Age", justify="right")

    data = kubectl_json(["get", "pods", "-n", namespace])
    if not data or not data.get("items"):
        table.add_row(f"[dim]No pods in {namespace}[/dim]", "", "", "", "", "")
        return table

    for pod in sorted(data["items"], key=lambda p: p["metadata"]["name"]):
        meta = pod["metadata"]
        spec = pod.get("spec", {})
        status = pod.get("status", {})
        name = meta["name"]

        containers = spec.get("containers", [])
        image = short_image(containers[0]["image"]) if containers else ""

        container_statuses = status.get("containerStatuses", [])
        ready_count = sum(1 for cs in container_statuses if cs.get("ready"))
        total_count = len(container_statuses)
        ready_str = f"{ready_count}/{total_count}" if total_count else "0/0"

        phase = status.get("phase", "Unknown")
        if phase == "Succeeded":
            pod_status = "Completed"
        else:
            pod_status = phase
        for cs in container_statuses:
            waiting = cs.get("state", {}).get("waiting", {})
            if waiting:
                pod_status = waiting.get("reason", pod_status)
                break

        created = meta.get("creationTimestamp", "")
        is_terminating = meta.get("deletionTimestamp") is not None

        if pod_status in ("Completed", "Succeeded"):
            style = "ready"
        elif pod_status == "Running" and ready_count == total_count and total_count > 0:
            style = "ready"
        elif pod_status in STUCK_PHASES or pod_status.startswith("Init:"):
            age = age_seconds(created) or 0
            if not is_terminating and age > STUCK_THRESHOLD_SECONDS:
                style = "failed"
            else:
                style = "notready"
        elif pod_status == "Running":
            style = "notready"
        else:
            style = "failed"

        restarts = sum(cs.get("restartCount", 0) for cs in container_statuses)

        table.add_row(
            name, image, ready_str, Text(pod_status, style=style), str(restarts), age_str(created)
        )
    return table


def get_summary() -> Panel:
    from .guard import get_suspend_status

    counts = {"ready": 0, "progressing": 0, "failed": 0}
    data = kubectl_json(["get", "kustomizations", "-n", "osdu-flux"])
    if data and "items" in data:
        for item in data["items"]:
            conditions = item.get("status", {}).get("conditions", [])
            ready = next((c for c in conditions if c.get("type") == "Ready"), {})
            if ready.get("status") == "True":
                counts["ready"] += 1
            else:
                counts["progressing"] += 1

    total = sum(counts.values())
    if total == 0:
        return Panel("[dim]No Flux resources found[/dim]", title="Summary", border_style="cyan")

    parts = []
    if counts["ready"]:
        parts.append(f"[ready]{counts['ready']} ready[/ready]")
    if counts["progressing"]:
        parts.append(f"[notready]{counts['progressing']} progressing[/notready]")

    text = f"Kustomizations: {' / '.join(parts)}  ({counts['ready']}/{total} complete)"
    if get_suspend_status():
        text += "  [bold yellow]| SUSPENDED[/bold yellow]"
    return Panel(text, title="Summary", border_style="cyan")


def render_status():
    from .guard import get_suspend_status

    console.print(Panel("[bold]SPI Stack Status[/bold]", border_style="cyan"))

    if get_suspend_status():
        console.print(
            Panel(
                "[bold yellow]GitRepository is SUSPENDED[/bold yellow] -- "
                "Flux will not auto-reconcile new commits.\n"
                "[dim]Run 'uv run spi reconcile --resume' to unfreeze.[/dim]",
                border_style="yellow",
            )
        )

    sections = [
        get_summary(),
        get_kustomization_table(),
        get_helmrelease_table(),
        get_custom_resources(platform_ns="platform"),
    ]

    jobs = _fetch_jobs(namespaces=["foundation", "platform", "osdu"])
    partition_table = get_partition_init_table(jobs)
    if partition_table:
        sections.append(partition_table)
    jobs_table = get_jobs_table(namespaces=["foundation", "platform", "osdu"])
    if jobs_table:
        sections.append(jobs_table)

    sections.append(get_pod_table("foundation", "Foundation Pods (operators)"))
    sections.append(get_pod_table("platform", "Platform Pods (middleware)"))
    sections.append(get_pod_table("osdu", "OSDU Pods (services)"))

    for section in sections:
        console.print(section)
        console.print()


def watch_status(interval: int = 30):
    console.print(f"[dim]Refreshing every {interval}s. Press Ctrl+C to stop.[/dim]\n")
    try:
        while True:
            console.clear()
            render_status()
            console.print(f"[dim]Next refresh in {interval}s... (Ctrl+C to stop)[/dim]")
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
