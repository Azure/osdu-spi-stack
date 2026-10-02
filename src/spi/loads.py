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

"""Named loads: the registry an environment carries, the state its Jobs
record, and the plan `spi load` applies (ADR-040)."""

import re
import time
from dataclasses import dataclass
from enum import Enum
from functools import partial
from typing import Any, Callable, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator
from rich.table import Table
from rich.text import Text

from .console import console, display_yaml
from .pins import _kubectl_read_json
from .shell import gather_reads, kubectl_apply_yaml, run_command, run_process
from .templates import LEGAL_TAG_BASE, load_job, parse_init_values

REGISTRY_CONFIGMAP = "spi-load-registry"
REGISTRY_KEY = "registry.yaml"
REGISTRY_API_VERSION = "spi.osdu.dev/v1"
LOAD_NAMESPACE = "osdu"
ROLE_LABEL = "osdu.spi/role"
LOAD_ROLE = "data-load"
DATASET_LABEL = "osdu.spi/dataset"
PARTITION_LABEL = "osdu.spi/partition"
SOURCE_ANNOTATION = "osdu.spi/source"
OUTCOME_ANNOTATION = "osdu.spi/outcome"
SCHEMA_JOB = "schema-load"
SCHEMA_KUSTOMIZATION = "spi-osdu-schema-load"
SCHEMA_LOCK_SERVICE = "schema-load"
# The Kustomization's own timeout, which exceeds the schema Job's deadline.
SCHEMA_RECONCILE_TIMEOUT = "155m"
FLUX_NAMESPACE = "osdu-flux"
# The Job's deadline plus node provisioning, scheduling, and image pull.
WAIT_SECONDS = 7200 + 900
POLL_SECONDS = 10

ABSENT, RUNNING, COMPLETE, STALE, FAILED = "absent", "running", "complete", "stale", "failed"
# A load across partitions reports the weakest of them.
_WEAKEST_FIRST = (FAILED, ABSENT, RUNNING, STALE, COMPLETE)
SATISFYING = frozenset({COMPLETE, STALE})

_COUNTS_RE = re.compile(r"\btotal=(\d+) loaded=(\d+) skipped=\d+ failed=(\d+)")
_PROGRESS_RE = re.compile(r"PROGRESS: .*|kinds \d+/\d+ registered")


class LoadError(RuntimeError):
    """A load that could not run or did not finish. ``refused`` marks the
    cases where nothing was changed, which `spi load` exits 2 on."""

    def __init__(self, code: str, message: str, refused: bool = False):
        super().__init__(message)
        self.code = code
        self.refused = refused


def _refused(code: str, message: str) -> LoadError:
    return LoadError(code, message, refused=True)


class Source(BaseModel):
    type: Literal["paired-image", "gitlab-archive"]
    service: str = ""
    project: str = ""
    ref: str = ""
    commit: str = ""
    path: str = ""

    @model_validator(mode="after")
    def _complete(self) -> "Source":
        if self.type == "paired-image" and not self.service:
            raise ValueError("a paired-image source names a service")
        if self.type == "gitlab-archive":
            if not self.project or not self.path:
                raise ValueError("a gitlab-archive source names a project and a path")
            if not re.fullmatch(r"[0-9a-f]{40}", self.commit):
                raise ValueError("a gitlab-archive source is pinned by a 40-character commit")
        return self

    @property
    def identity(self) -> str:
        """What a Job records as its source and a later run compares."""
        return f"{self.project}@{self.commit}"


class Load(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    default: bool = False
    runner: Literal["flux", "job"]
    requires: list[str] = []
    source: Source
    loader: Optional[Literal["records"]] = None
    scope: Literal["primary-partition", "each-partition"]

    @model_validator(mode="after")
    def _runner_matches_source(self) -> "Load":
        if self.runner == "job" and (self.source.type != "gitlab-archive" or not self.loader):
            raise ValueError(f"{self.name}: a job load needs a gitlab-archive source and a loader")
        if self.runner == "flux" and self.source.type != "paired-image":
            raise ValueError(f"{self.name}: a flux load needs a paired-image source")
        return self


class Registry(BaseModel):
    apiVersion: Literal["spi.osdu.dev/v1"]
    loads: list[Load]

    @model_validator(mode="after")
    def _consistent(self) -> "Registry":
        names = [load.name for load in self.loads]
        if len(names) != len(set(names)):
            raise ValueError("load names are not unique")
        requires = {load.name: load.requires for load in self.loads}
        for name, needed in requires.items():
            unknown = sorted(set(needed) - set(names))
            if unknown:
                raise ValueError(f"{name} requires unknown loads: {', '.join(unknown)}")

        def reaches(start: str, name: str, seen: frozenset) -> bool:
            return any(
                dep == start or (dep not in seen and reaches(start, dep, seen | {dep}))
                for dep in requires[name]
            )

        cyclic = sorted(name for name in names if reaches(name, name, frozenset()))
        if cyclic:
            raise ValueError(f"requires form a cycle through: {', '.join(cyclic)}")
        return self

    def get(self, name: str) -> Load:
        return next(load for load in self.loads if load.name == name)


def parse_registry(text: str) -> Registry:
    try:
        return Registry.model_validate(yaml.safe_load(text))
    except (yaml.YAMLError, ValidationError) as exc:
        raise _refused(
            "registry_unreadable",
            f"This spi cannot read the environment's load registry; update it. {exc}",
        ) from exc


def read_registry() -> Optional[Registry]:
    """The environment's registry, or None when it delivers none: a profile
    without OSDU services, or a stack version older than the registry."""
    data = _kubectl_read_json(
        ["get", "configmap", REGISTRY_CONFIGMAP, "-n", LOAD_NAMESPACE], "the load registry"
    )
    text = ((data or {}).get("data") or {}).get(REGISTRY_KEY)
    return parse_registry(text) if text else None


def read_load_jobs() -> list[dict]:
    data = _kubectl_read_json(
        ["get", "jobs", "-n", LOAD_NAMESPACE, "-l", f"{ROLE_LABEL}={LOAD_ROLE}"], "the load Jobs"
    )
    items = (data or {}).get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def read_schema_job() -> Optional[dict]:
    return _kubectl_read_json(["get", "job", SCHEMA_JOB, "-n", LOAD_NAMESPACE], "the schema Job")


def job_name(load: Load, partition: str) -> str:
    name = f"load-{load.name}-{partition}-{load.source.commit[:8]}"
    if len(name) > 63:
        raise _refused("name_too_long", f"The Job name {name} exceeds 63 characters.")
    return name


def _condition(job: dict, kind: str) -> bool:
    return any(
        c.get("type") == kind and c.get("status") == "True"
        for c in (job.get("status") or {}).get("conditions") or []
    )


def job_phase(job: dict) -> str:
    """complete, failed, or running. status.failed counts retried pods; only
    the Failed condition means the backoff limit or deadline is exhausted."""
    if (job.get("status") or {}).get("succeeded") or _condition(job, "Complete"):
        return COMPLETE
    if _condition(job, "Failed"):
        return FAILED
    return RUNNING


def _name(job: dict) -> str:
    return (job.get("metadata") or {}).get("name", "")


def _annotation(job: dict, key: str) -> str:
    return ((job.get("metadata") or {}).get("annotations") or {}).get(key, "")


def jobs_for(jobs: list[dict], load: Load, partition: str) -> list[dict]:
    """This load's Jobs for one partition, oldest first."""
    matching = [
        job
        for job in jobs
        if ((job.get("metadata") or {}).get("labels") or {}).get(DATASET_LABEL) == load.name
        and ((job.get("metadata") or {}).get("labels") or {}).get(PARTITION_LABEL) == partition
    ]
    return sorted(matching, key=lambda j: (j.get("metadata") or {}).get("creationTimestamp", ""))


def newest_job(jobs: list[dict], load: Load, partition: str) -> Optional[dict]:
    matching = jobs_for(jobs, load, partition)
    return matching[-1] if matching else None


class Action(str, Enum):
    NONE = "none"
    WAIT = "wait"
    CREATE = "create"
    RECREATE = "recreate"
    SUPERSEDE = "supersede"


def plan_load(load: Load, newest: Optional[dict], force: bool) -> Action:
    """What `spi load` does for one partition, from the newest Job it finds."""
    if newest is None:
        return Action.CREATE
    phase = job_phase(newest)
    if phase == FAILED:
        return Action.RECREATE
    if phase == RUNNING:
        return Action.RECREATE if force else Action.WAIT
    if _annotation(newest, SOURCE_ANNOTATION) != load.source.identity:
        return Action.SUPERSEDE
    return Action.RECREATE if force else Action.NONE


def parse_counts(outcome: str) -> Optional[dict]:
    match = _COUNTS_RE.search(outcome)
    if not match:
        return None
    total, loaded, failed = (int(value) for value in match.groups())
    return {"total": total, "loaded": loaded, "failed": failed}


def _partition_facts(state: str, job: Optional[dict], outcome: str) -> dict:
    facts: dict[str, Any] = {"state": state}
    completed = ((job or {}).get("status") or {}).get("completionTime", "")
    if completed:
        facts["completedAt"] = completed
    counts = parse_counts(outcome)
    if counts:
        facts["records"] = counts
    if state == FAILED and outcome:
        facts["message"] = outcome
    return facts


def weakest(states: list[str]) -> str:
    return next((state for state in _WEAKEST_FIRST if state in states), ABSENT)


def _job_load_facts(
    load: Load, partitions: list[str], jobs: list[dict], outcome_of: Callable[[dict], str]
) -> dict:
    per_partition = {}
    for partition in partitions:
        job = newest_job(jobs, load, partition)
        if job is None:
            per_partition[partition] = {"state": ABSENT}
            continue
        state = job_phase(job)
        if state == COMPLETE and _annotation(job, SOURCE_ANNOTATION) != load.source.identity:
            state = STALE
        outcome = outcome_of(job) if state != RUNNING else ""
        per_partition[partition] = _partition_facts(state, job, outcome)
    return {
        "state": weakest([facts["state"] for facts in per_partition.values()]),
        "source": f"{load.source.project}@{load.source.commit[:12]}",
        "version": load.source.ref,
        "partitions": per_partition,
    }


def _flux_load_facts(
    load: Load, partitions: list[str], job: Optional[dict], lock_services: dict
) -> dict:
    image = lock_services.get(SCHEMA_LOCK_SERVICE) or {}
    digest = image.get("digest", "")
    repository = image.get("repository", "")
    if job is None:
        state = ABSENT
    else:
        state = job_phase(job)
        containers = (((job.get("spec") or {}).get("template") or {}).get("spec") or {}).get(
            "containers"
        ) or [{}]
        # Flux replaces the Job when the lock moves; until it has, the Job
        # that completed ran the previous pair.
        if state == COMPLETE and digest and digest not in containers[0].get("image", ""):
            state = STALE
    return {
        "state": state,
        "source": f"{repository}@{digest}" if repository and digest else repository,
        "version": image.get("tag", ""),
        "partitions": {partition: _partition_facts(state, job, "") for partition in partitions[:1]},
    }


def scope_partitions(load: Load, partitions: list[str]) -> list[str]:
    return partitions[:1] if load.scope == "primary-partition" else list(partitions)


def build_facts(
    registry: Registry,
    partitions: list[str],
    jobs: list[dict],
    schema_job: Optional[dict],
    lock_services: dict,
    outcome_of: Callable[[dict], str],
) -> dict:
    """The `loads` block of `spi info --json`: every registry entry, in order."""
    facts = {}
    for load in registry.loads:
        scoped = scope_partitions(load, partitions)
        if load.runner == "flux":
            facts[load.name] = _flux_load_facts(load, scoped, schema_job, lock_services)
        else:
            facts[load.name] = _job_load_facts(load, scoped, jobs, outcome_of)
    return facts


def read_outcome(job: dict) -> str:
    """The loader's outcome line: the Job's annotation, else the newest pod's
    termination message while the pod still exists."""
    from .status import _read_job_termination_message

    return _annotation(job, OUTCOME_ANNOTATION) or _read_job_termination_message(_name(job))


def collect_facts(partitions: list[str], lock_services: dict) -> dict:
    """The `loads` block for the connected environment.

    Empty without a registry, and for a registry this CLI predates: the
    facts must still render, and `spi load` is where that refusal is named.
    """
    try:
        registry, jobs, schema_job = gather_reads([read_registry, read_load_jobs, read_schema_job])
    except LoadError:
        return {}
    if registry is None:
        return {}
    return build_facts(registry, partitions, jobs, schema_job, lock_services, read_outcome)


@dataclass(frozen=True)
class Target:
    """What a load Job needs from the environment besides the registry."""

    partitions: tuple[str, ...]
    legal_tag_base: str
    entitlements_domain: str
    lock_services: dict


def read_target() -> Target:
    from .bootstrap import read_init_values
    from .info import _osdu_versions, read_entitlements_domain
    from .pins import read_lock

    init_values, domain, lock = gather_reads(
        [read_init_values, read_entitlements_domain, partial(read_lock, required=False)]
    )
    values = parse_init_values(init_values)
    partitions = tuple(p for p in values.get("partitions") or [] if isinstance(p, str))
    if not partitions:
        raise _refused("no_partitions", "The environment declares no partitions.")
    legal_tag = values.get("legalTag")
    return Target(
        partitions=partitions,
        legal_tag_base=legal_tag if isinstance(legal_tag, str) and legal_tag else LEGAL_TAG_BASE,
        entitlements_domain=domain,
        lock_services=_osdu_versions(lock)["services"],
    )


def select_loads(registry: Registry, datasets: list[str]) -> list[Load]:
    """The requested loads in registry order; the default set when none is named."""
    if not datasets:
        return [load for load in registry.loads if load.default]
    known = [load.name for load in registry.loads]
    unknown = [name for name in datasets if name not in known]
    if unknown:
        raise _refused(
            "unknown_dataset",
            f"No load named {', '.join(unknown)} on this environment (has: {', '.join(known)}).",
        )
    return [load for load in registry.loads if load.name in datasets]


def select_partitions(load: Load, target: Target, partition: Optional[str]) -> list[str]:
    scoped = scope_partitions(load, list(target.partitions))
    if not partition:
        return scoped
    if partition not in target.partitions:
        raise _refused(
            "unknown_partition",
            f"No partition '{partition}' on this environment "
            f"(has: {', '.join(target.partitions)}).",
        )
    return [p for p in scoped if p == partition]


def _delete_job(name: str) -> None:
    run_command(
        ["kubectl", "delete", "job", name, "-n", LOAD_NAMESPACE, "--ignore-not-found"],
        description=f"Delete load Job ({name})",
    )


def _legal_tag_seeded(partition: str) -> bool:
    from .info import _legal_tag_seeded as seeded

    return seeded(partition)


def _create_job(load: Load, partition: str, target: Target) -> str:
    name = job_name(load, partition)
    manifest = load_job(
        name=name,
        dataset=load.name,
        partition=partition,
        source=load.source.identity,
        env={
            "PARTITION": partition,
            "LEGAL_TAG": f"{partition}-{target.legal_tag_base}",
            "ENTITLEMENTS_DOMAIN": target.entitlements_domain,
            "SOURCE_PROJECT": load.source.project,
            "SOURCE_COMMIT": load.source.commit,
            "SOURCE_PATH": load.source.path,
        },
    )
    display_yaml(manifest, title=f"Load Job ({name})")
    kubectl_apply_yaml(manifest, f"create load Job {name}")
    return name


def _record_outcome(job: dict) -> str:
    """Copy the pod's outcome line onto a finished Job, where it outlives the pod."""
    outcome = _annotation(job, OUTCOME_ANNOTATION)
    if outcome or job_phase(job) == RUNNING:
        return outcome
    outcome = read_outcome(job)
    if outcome:
        run_command(
            [
                "kubectl",
                "annotate",
                "--overwrite",
                "job",
                _name(job),
                "-n",
                LOAD_NAMESPACE,
                f"{OUTCOME_ANNOTATION}={outcome}",
            ],
            description=f"Record load outcome ({_name(job)})",
            check=False,
        )
    return outcome


def _progress(name: str) -> str:
    result = run_process(
        ["kubectl", "logs", f"job/{name}", "-n", LOAD_NAMESPACE, "--tail", "40"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines = _PROGRESS_RE.findall(result.stdout or "") if result.returncode == 0 else []
    return lines[-1].removeprefix("PROGRESS: ") if lines else ""


def _await_job(load: Load, name: str) -> dict:
    """Poll a Job to a terminal phase, printing the loader's progress lines.

    An unreadable cluster is waited out: one failed read must not abandon a
    Job that is still loading.
    """
    from .pins import PinError

    deadline = time.monotonic() + WAIT_SECONDS
    shown = ""
    while True:
        try:
            job = _kubectl_read_json(["get", "job", name, "-n", LOAD_NAMESPACE], f"Job {name}")
        except PinError:
            job = {}
        if job is None:
            raise LoadError("job_removed", f"{load.name}: Job {name} was deleted while loading.")
        if job and job_phase(job) != RUNNING:
            return job
        line = _progress(name)
        if line and line != shown:
            console.print(f"  {load.name:<16} {line}")
            shown = line
        if time.monotonic() >= deadline:
            raise LoadError(
                "wait_timeout",
                f"{load.name}: Job {name} is still running after {WAIT_SECONDS // 60} minutes; "
                "it keeps loading. Check it with 'spi load --status'.",
            )
        time.sleep(POLL_SECONDS)


def _finish(load: Load, partition: str, name: str, others: list[str]) -> None:
    job = _await_job(load, name)
    outcome = _record_outcome(job)
    if job_phase(job) == FAILED:
        raise LoadError(
            "load_failed", f"{load.name} failed for {partition}: {outcome or f'see Job {name}'}"
        )
    for other in others:
        _delete_job(other)
    counts = parse_counts(outcome)
    detail = f", {counts['loaded']} of {counts['total']} records" if counts else ""
    console.print(f"  [success]{load.name:<16} complete for {partition}{detail}[/success]")


def _run_job_load(
    load: Load, partitions: list[str], target: Target, force: bool, wait: bool
) -> None:
    if not target.entitlements_domain:
        raise _refused(
            "entitlements_missing",
            "The entitlements service is not deployed, so its domain is unknown.",
        )
    unseeded = [p for p in partitions if not _legal_tag_seeded(p)]
    if unseeded:
        raise _refused(
            "legal_tag_missing",
            f"{load.name} needs the default legal tag, which legal-init has not created for "
            f"{', '.join(unseeded)}. Nothing was created.",
        )
    jobs = read_load_jobs()
    pending: list[tuple[str, str, list[str]]] = []
    for partition in partitions:
        existing = jobs_for(jobs, load, partition)
        newest = existing[-1] if existing else None
        action = plan_load(load, newest, force)
        name = job_name(load, partition)
        if action is Action.NONE and newest is not None:
            _record_outcome(newest)
            console.print(f"  {load.name:<16} complete for {partition}, nothing to do")
            continue
        if action is Action.WAIT and newest is not None:
            console.print(f"  {load.name:<16} Job {_name(newest)} is already running")
            pending.append((partition, _name(newest), []))
            continue
        # A Job's template is immutable, so one already holding the name goes first.
        doomed = {name} & {_name(job) for job in existing}
        if action is Action.RECREATE and newest is not None:
            doomed.add(_name(newest))
        for doomed_name in sorted(doomed):
            _delete_job(doomed_name)
        console.print(f"  {load.name:<16} creating Job {name}")
        _create_job(load, partition, target)
        pending.append((partition, name, [_name(j) for j in existing if _name(j) not in doomed]))
    if not wait:
        for partition, name, _others in pending:
            console.print(f"  {load.name:<16} Job {name} left running for {partition}")
        return
    for partition, name, others in pending:
        _finish(load, partition, name, others)


def _run_flux_load(load: Load, facts: dict, force: bool, wait: bool) -> None:
    state = facts["state"]
    if not force:
        version = facts.get("version") or "no version"
        console.print(f"  {load.name:<16} {state}, {version}, nothing to do")
        return
    _delete_job(SCHEMA_JOB)
    if wait:
        run_command(
            [
                "flux",
                "reconcile",
                "kustomization",
                SCHEMA_KUSTOMIZATION,
                "-n",
                FLUX_NAMESPACE,
                "--timeout",
                SCHEMA_RECONCILE_TIMEOUT,
            ],
            description=f"Recreate the {load.name} Job and wait for it",
        )
        console.print(f"  [success]{load.name:<16} complete[/success]")
        return
    run_command(
        [
            "kubectl",
            "annotate",
            "--overwrite",
            f"kustomization/{SCHEMA_KUSTOMIZATION}",
            "-n",
            FLUX_NAMESPACE,
            f"reconcile.fluxcd.io/requestedAt={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        ],
        description=f"Trigger Kustomization reconciliation ({SCHEMA_KUSTOMIZATION})",
    )


def current_facts(registry: Registry, target: Target) -> dict:
    jobs, schema_job = gather_reads([read_load_jobs, read_schema_job])
    return build_facts(
        registry, list(target.partitions), jobs, schema_job, target.lock_services, read_outcome
    )


def run_loads(
    registry: Registry,
    target: Target,
    datasets: list[str],
    partition: Optional[str],
    force: bool,
    wait: bool,
) -> None:
    """Run the selected loads in registry order, each after its requirements.

    A requirement is read again before each load, so one this run completed
    satisfies the loads behind it.
    """
    from .pins import PinError, require_deployable

    selected = select_loads(registry, datasets)
    scoped = {load.name: select_partitions(load, target, partition) for load in selected}
    if any(load.runner == "job" for load in selected):
        try:
            require_deployable()
        except PinError as exc:
            raise _refused("not_deployable", str(exc)) from exc
    for load in selected:
        facts = current_facts(registry, target)
        for needed in load.requires:
            state = facts[needed]["state"]
            if state not in SATISFYING:
                raise _refused(
                    "requires_unmet",
                    f"{load.name} requires {needed}, which is {state}; {load.name} was not started.",
                )
        if not scoped[load.name]:
            console.print(f"  {load.name:<16} does not load into {partition}, nothing to do")
        elif load.runner == "flux":
            _run_flux_load(load, facts[load.name], force, wait)
        else:
            _run_job_load(load, scoped[load.name], target, force, wait)


_STATE_STYLES = {COMPLETE: "ready", STALE: "notready", RUNNING: "notready", FAILED: "failed"}


def loads_table(facts: dict) -> Optional[Table]:
    """One row per load and partition, for `spi load --status` and `spi info`."""
    from .status import age_str

    if not facts:
        return None
    table = Table(title="Loads", border_style="cyan", expand=True)
    for column in ("Load", "Partition", "State", "Source"):
        table.add_column(column)
    table.add_column("Records", justify="right")
    table.add_column("Age", justify="right")
    for name, load in facts.items():
        repository = "/".join(load.get("source", "").split("@")[0].split("/")[-2:])
        source = f"{repository} {load.get('version', '')}".strip()
        for partition, entry in load.get("partitions", {}).items():
            state = entry["state"]
            records = entry.get("records") or {}
            table.add_row(
                name,
                partition,
                Text(state, style=_STATE_STYLES.get(state, "dim")),
                source,
                str(records["loaded"]) if records else "",
                age_str(entry.get("completedAt", "")),
            )
    return table
