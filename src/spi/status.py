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

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from typing import List, Optional

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .console import console
from .deploy_record import DeployRecord, DeployRecordError, read_deploy_record
from .pins import PinError, decode_pins
from .shell import gather_reads, kubectl_json, run_process

STATUS_API_VERSION = "spi.osdu.dev/v1"

# The namespaces the stack owns, in layer order, with their dashboard titles.
_STACK_POD_SECTIONS = [
    ("foundation", "Foundation Pods (operators)"),
    ("platform", "Platform Pods (middleware)"),
    ("osdu", "OSDU Pods (services)"),
]
_STACK_NAMESPACES = [namespace for namespace, _title in _STACK_POD_SECTIONS]


class StatusError(RuntimeError):
    """Raised when required status inputs cannot be collected."""


@dataclass(frozen=True)
class StatusReason:
    code: str
    message: str
    resource: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "message": self.message,
            "resource": self.resource,
        }


@dataclass(frozen=True)
class KustomizationState:
    name: str
    ready: bool
    reason: str
    message: str
    # Kustomizations labeled spi-stack.gating: "false" stay visible with their
    # reason but are excluded from the Ready verdict.
    gating: bool = True

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "ready": self.ready,
            "reason": self.reason,
            "message": self.message,
            "gating": self.gating,
        }


@dataclass(frozen=True)
class KustomizationReadiness:
    """Flux convergence, computed once so nothing can disagree with it.

    `spi status` and the pin guard both derive their Ready
    answer from this, rather than each re-reading Kustomizations.
    """

    items: tuple[dict, ...]
    states: tuple[KustomizationState, ...]
    ready: bool
    reason: StatusReason | None


@dataclass(frozen=True)
class StackState:
    ref: str
    resolved_commit: str
    deployed_at: str
    cli_version: str
    profile: str

    @staticmethod
    def from_record(record: DeployRecord | None) -> "StackState":
        if record is None:
            return StackState("", "", "", "", "")
        return StackState(
            ref=record.ref,
            resolved_commit=record.resolved_commit,
            deployed_at=record.deployed_at,
            cli_version=record.cli_version,
            profile=record.profile,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "ref": self.ref,
            "resolvedCommit": self.resolved_commit,
            "deployedAt": self.deployed_at,
            "cliVersion": self.cli_version,
            "profile": self.profile,
        }


@dataclass(frozen=True)
class ImageState:
    branch: str
    resolved_at: str
    count: int
    pinned_services: tuple[str, ...]

    def to_dict(self) -> dict[str, str | int | list[str]]:
        return {
            "branch": self.branch,
            "resolvedAt": self.resolved_at,
            "count": self.count,
            "pinnedServices": list(self.pinned_services),
        }


@dataclass(frozen=True)
class StatusSnapshot:
    ready: bool
    deployable: bool
    reason: StatusReason | None
    suspended: bool
    maintenance: bool
    kustomizations: tuple[KustomizationState, ...]
    stack: StackState
    images: ImageState
    base_url: str
    kustomization_items: tuple[dict, ...]

    def to_dict(self) -> dict:
        not_ready = [item.to_dict() for item in self.kustomizations if not item.ready]
        return {
            "apiVersion": STATUS_API_VERSION,
            "ready": self.ready,
            "deployable": self.deployable,
            "reason": self.reason.to_dict() if self.reason else None,
            "suspended": self.suspended,
            "maintenance": self.maintenance,
            "kustomizations": {
                "total": len(self.kustomizations),
                "ready": sum(1 for item in self.kustomizations if item.ready),
                "notReady": not_ready,
            },
            "stack": self.stack.to_dict(),
            "images": self.images.to_dict(),
            "baseUrl": self.base_url,
        }


def _required_kubectl_json(args: list[str], description: str) -> dict:
    result = run_process(
        ["kubectl", *args, "-o", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or "kubectl failed"
        raise StatusError(f"Could not {description}: {detail}")
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise StatusError(f"Could not parse {description}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise StatusError(f"Could not parse {description}: expected a JSON object")
    return parsed


def _optional_configmap(name: str, namespace: str) -> dict | None:
    result = run_process(
        [
            "kubectl",
            "get",
            "configmap",
            name,
            "-n",
            namespace,
            "--ignore-not-found",
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
        raise StatusError(f"Could not read ConfigMap {name}: {detail}")
    if not result.stdout.strip():
        return None
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise StatusError(f"Could not parse ConfigMap {name}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise StatusError(f"Could not parse ConfigMap {name}: expected a JSON object")
    return parsed


def _ready_condition(item: dict) -> dict:
    conditions = item.get("status", {}).get("conditions", [])
    return next((condition for condition in conditions if condition.get("type") == "Ready"), {})


def collect_kustomization_readiness() -> KustomizationReadiness:
    """Read Flux Kustomizations and derive Ready convergence and its blocker.

    The single predicate for Flux readiness: `collect_status` and the pin
    guard in `pins.py` both call this rather than each
    re-deriving `ready` from `kubectl get kustomizations`.
    """

    try:
        kustomization_data = _required_kubectl_json(
            ["get", "kustomizations", "-n", "osdu-flux"],
            "read Flux Kustomizations",
        )
    except StatusError as exc:
        detail = str(exc).lower()
        if not any(
            marker in detail
            for marker in ("not found", "no matches for kind", "doesn't have a resource type")
        ):
            raise
        kustomization_data = {"items": []}
    raw_items = kustomization_data.get("items")
    if not isinstance(raw_items, list):
        raise StatusError("Flux Kustomization response has no items list")

    items = tuple(
        sorted(
            (item for item in raw_items if isinstance(item, dict)),
            key=lambda item: (
                item.get("metadata", {}).get("labels", {}).get("spi-stack.layer", "9"),
                item.get("metadata", {}).get("name", ""),
            ),
        )
    )
    states = []
    for item in items:
        condition = _ready_condition(item)
        labels = item.get("metadata", {}).get("labels", {})
        states.append(
            KustomizationState(
                name=item.get("metadata", {}).get("name", ""),
                ready=condition.get("status") == "True",
                reason=condition.get("reason", ""),
                message=condition.get("message", ""),
                gating=labels.get("spi-stack.gating", "true") != "false",
            )
        )
    states = tuple(states)
    gating = tuple(state for state in states if state.gating)
    ready = bool(gating) and all(state.ready for state in gating)

    reason: StatusReason | None = None
    if not gating:
        reason = StatusReason(
            code="no_kustomizations",
            message="No gating Flux Kustomizations are visible.",
            resource="kustomizations/osdu-flux",
        )
    else:
        first_not_ready = next((state for state in gating if not state.ready), None)
        if first_not_ready is not None:
            detail = first_not_ready.message or first_not_ready.reason or "not Ready"
            reason = StatusReason(
                code="kustomization_not_ready",
                message=f"{first_not_ready.name}: {detail}",
                resource=f"kustomization/osdu-flux/{first_not_ready.name}",
            )

    return KustomizationReadiness(items=items, states=states, ready=ready, reason=reason)


def _read_deploy_record_result() -> DeployRecord | DeployRecordError | None:
    """Read the deploy record, returning its failure rather than raising.

    ``gather_reads`` resolves in call order, so returning the error keeps a
    Kustomization or GitRepository failure ahead of this one.
    """
    try:
        return read_deploy_record(required=False)
    except DeployRecordError as exc:
        return exc


def collect_status() -> StatusSnapshot:
    from .info import collect_base_url

    # Five independent reads. Ordered results keep the failure a caller sees
    # deterministic: a Kustomization error still outranks a GitRepository one.
    (
        kustomization_readiness,
        git_repository,
        record_result,
        image_lock,
        base_url,
    ) = gather_reads(
        [
            collect_kustomization_readiness,
            lambda: _required_kubectl_json(
                ["get", "gitrepository", "osdu-spi-stack-system", "-n", "osdu-flux"],
                "read the Flux GitRepository",
            ),
            _read_deploy_record_result,
            lambda: _optional_configmap("osdu-image-lock", "osdu-flux"),
            collect_base_url,
        ]
    )

    items = kustomization_readiness.items
    states = kustomization_readiness.states
    ready = kustomization_readiness.ready
    reason = kustomization_readiness.reason

    suspended = bool(git_repository.get("spec", {}).get("suspend", False))

    if isinstance(record_result, DeployRecordError):
        raise StatusError(str(record_result))
    record = record_result

    lock_data = (image_lock or {}).get("data") or {}
    if not isinstance(lock_data, dict):
        raise StatusError("ConfigMap osdu-image-lock has invalid data")
    raw_count = lock_data.get("IMAGE_COUNT", "0")
    if not isinstance(raw_count, str) or not raw_count.isdigit():
        raise StatusError("ConfigMap osdu-image-lock has an invalid IMAGE_COUNT")
    try:
        pins = decode_pins(image_lock) if image_lock is not None else {}
    except PinError as exc:
        raise StatusError(str(exc)) from exc

    maintenance = record.maintenance if record else False
    deployable = ready and record is not None and not maintenance

    # A readiness blocker takes precedence; maintenance and a missing record
    # only matter once Flux has converged.
    if reason is None:
        if maintenance:
            reason = StatusReason(
                code="maintenance",
                message="The environment is in maintenance.",
                resource="configmap/osdu-flux/spi-deploy-record",
            )
        elif record is None:
            reason = StatusReason(
                code="missing_deploy_record",
                message="The environment has no deploy record; rerun spi up.",
                resource="configmap/osdu-flux/spi-deploy-record",
            )

    return StatusSnapshot(
        ready=ready,
        deployable=deployable,
        reason=reason,
        suspended=suspended,
        maintenance=maintenance,
        kustomizations=states,
        stack=StackState.from_record(record),
        images=ImageState(
            branch=str(lock_data.get("IMAGE_BRANCH", "")),
            resolved_at=str(lock_data.get("IMAGE_RESOLVED_AT", "")),
            count=int(raw_count),
            pinned_services=tuple(sorted(pins)),
        ),
        base_url=base_url,
        kustomization_items=items,
    )


def status_exit_code(snapshot: StatusSnapshot) -> int:
    return 0 if snapshot.deployable else 2


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


def get_kustomization_table(items: tuple[dict, ...] | None = None) -> Table:
    table = Table(title="Flux Kustomizations", border_style="cyan", expand=True)
    table.add_column("Name", style="bold")
    table.add_column("Layer", justify="center")
    table.add_column("Status")
    table.add_column("Message")
    table.add_column("Age", justify="right")

    if items is None:
        data = kubectl_json(["get", "kustomizations", "-n", "osdu-flux"])
        if not data or "items" not in data:
            table.add_row("[dim]No kustomizations found[/dim]", "", "", "", "")
            return table
        items = tuple(
            sorted(
                data["items"],
                key=lambda x: (
                    x.get("metadata", {}).get("labels", {}).get("spi-stack.layer", "9"),
                    x.get("metadata", {}).get("name", ""),
                ),
            )
        )

    if not items:
        table.add_row("[dim]No kustomizations found[/dim]", "", "", "", "")
        return table

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
        # History is empty before the first install completes.
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

    cnpg, es = gather_reads(
        [
            lambda: kubectl_json(["get", "clusters.postgresql.cnpg.io", "-n", platform_ns]),
            lambda: kubectl_json(
                ["get", "elasticsearches.elasticsearch.k8s.elastic.co", "-n", platform_ns]
            ),
        ]
    )

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


def get_jobs_table(jobs: List[dict]) -> Optional[Table]:
    """Show non-partition bootstrap Jobs across the stack's namespaces.

    Partition + entitlements init Jobs are pulled out into
    ``get_partition_init_table`` so multi-partition deploys present a
    per-partition view; remaining one-shot Jobs (schema-load, etc.)
    appear here. Takes the same fetched list that table renders from.
    """
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


def get_summary(snapshot: StatusSnapshot | None = None) -> Panel:
    counts = {"ready": 0, "progressing": 0, "failed": 0}
    if snapshot is not None:
        for item in snapshot.kustomizations:
            counts["ready" if item.ready else "progressing"] += 1
    else:
        data = kubectl_json(["get", "kustomizations", "-n", "osdu-flux"])
        if data and "items" in data:
            for item in data["items"]:
                ready = _ready_condition(item)
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
    suspended = snapshot.suspended if snapshot is not None else False
    if snapshot is None:
        from .guard import get_suspend_status

        suspended = get_suspend_status()
    if suspended:
        text += "  [bold yellow]| SUSPENDED[/bold yellow]"
    return Panel(text, title="Summary", border_style="cyan")


def render_status(snapshot: StatusSnapshot | None = None):
    snapshot = snapshot or collect_status()
    console.print(Panel("[bold]SPI Stack Status[/bold]", border_style="cyan"))

    if snapshot.suspended:
        console.print(
            Panel(
                "[bold yellow]GitRepository is SUSPENDED[/bold yellow] -- "
                "Flux will not auto-reconcile new commits.\n"
                "[dim]Run 'spi reconcile --resume' to unfreeze.[/dim]",
                border_style="yellow",
            )
        )

    # The summary and Kustomization tables render from the snapshot; every
    # other section is an independent cluster read, so they go out in one wave.
    helmreleases, custom_resources, jobs, *pod_tables = gather_reads(
        [
            get_helmrelease_table,
            lambda: get_custom_resources(platform_ns="platform"),
            lambda: _fetch_jobs(_STACK_NAMESPACES),
            *[partial(get_pod_table, ns, title) for ns, title in _STACK_POD_SECTIONS],
        ]
    )

    sections = [
        get_summary(snapshot),
        get_kustomization_table(snapshot.kustomization_items),
        helmreleases,
        custom_resources,
    ]

    partition_table = get_partition_init_table(jobs)
    if partition_table:
        sections.append(partition_table)
    jobs_table = get_jobs_table(jobs)
    if jobs_table:
        sections.append(jobs_table)

    sections.extend(pod_tables)

    for section in sections:
        console.print(section)
        console.print()


def watch_status(interval: int = 30):
    console.print(f"[dim]Refreshing every {interval}s. Press Ctrl+C to stop.[/dim]\n")
    try:
        while True:
            console.clear()
            try:
                render_status()
            except StatusError as exc:
                console.print(
                    Panel(
                        f"[warning]Status temporarily unavailable:[/warning] {exc}",
                        border_style="yellow",
                    )
                )
            console.print(f"[dim]Next refresh in {interval}s... (Ctrl+C to stop)[/dim]")
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
