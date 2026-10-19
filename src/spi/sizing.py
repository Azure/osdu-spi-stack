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

"""Sizing keys Flux substitutes into the service and middleware manifests.

Each manifest carries `${KEY:=default}` for every value here, and the defaults
must match: the CLI checks overrides against them without reading the stack.
A declaration's or stack file's `resources:` section becomes those keys here,
and `spi up` writes them to the `spi-sizing` ConfigMap once they pass the checks.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator, Mapping

import yaml

from spi.console import console, display_result, display_yaml
from spi.images import IMAGE_LOCK_NAMESPACE, IMAGE_REGISTRY, image_lock_key
from spi.shell import kubectl_apply_yaml, run_command, run_process

SIZING_CONFIGMAP = "spi-sizing"

_RESOURCE_FIELDS = ("CPU_REQUEST", "CPU_LIMIT", "MEMORY_REQUEST", "MEMORY_LIMIT")

_SERVICE_RESOURCES = ("300m", "1", "896Mi", "1536Mi")
_SERVICE_REPLICAS = {"partition": "2", "entitlements": "2", "legal": "2"}


def _resources(prefix: str, values: tuple[str, str, str, str]) -> dict[str, str]:
    return {f"{prefix}_{field}": value for field, value in zip(_RESOURCE_FIELDS, values)}


def sizing_services() -> tuple[str, ...]:
    """Return the services deployed from the shared service chart."""

    return tuple(
        name
        for name, entry in IMAGE_REGISTRY.items()
        if entry.file.startswith(("services/", "services-reference/"))
    )


def _service_defaults(service: str) -> dict[str, str]:
    prefix = image_lock_key(service)
    return {
        f"{prefix}_REPLICAS": _SERVICE_REPLICAS.get(service, "1"),
        **_resources(prefix, _SERVICE_RESOURCES),
    }


MIDDLEWARE_DEFAULTS: dict[str, dict[str, str]] = {
    "elasticsearch": {
        "ES_COUNT": "3",
        **_resources("ES", ("500m", "2", "2Gi", "4Gi")),
        "ES_STORAGE": "128Gi",
    },
    "postgres": {
        "PG_INSTANCES": "3",
        **_resources("PG", ("500m", "2", "1Gi", "2Gi")),
        "PG_STORAGE": "10Gi",
        "PG_WAL_STORAGE": "4Gi",
    },
    "redis": {
        "REDIS_REPLICAS": "2",
        **_resources("REDIS", ("250m", "1", "256Mi", "1Gi")),
        "REDIS_STORAGE": "8Gi",
    },
    "airflow": {
        **_resources("AIRFLOW_API_SERVER", ("250m", "1", "512Mi", "2Gi")),
        **_resources("AIRFLOW_SCHEDULER", ("250m", "1", "512Mi", "1Gi")),
        **_resources("AIRFLOW_DAG_PROCESSOR", ("100m", "500m", "256Mi", "512Mi")),
        **_resources("AIRFLOW_TRIGGERER", ("100m", "500m", "256Mi", "512Mi")),
    },
}


def sizing_defaults() -> dict[str, dict[str, str]]:
    """Return every sizable component mapped to its substitution keys and defaults."""

    return {
        **{service: _service_defaults(service) for service in sizing_services()},
        **MIDDLEWARE_DEFAULTS,
    }


class SizingError(ValueError):
    """A `resources:` section that cannot be written to the cluster as given."""


_RESOURCE_PATHS = {
    ("cpu", "request"): "CPU_REQUEST",
    ("cpu", "limit"): "CPU_LIMIT",
    ("memory", "request"): "MEMORY_REQUEST",
    ("memory", "limit"): "MEMORY_LIMIT",
}
_MIDDLEWARE_PATHS: dict[str, tuple[str, dict[tuple[str, ...], str]]] = {
    "elasticsearch": ("ES", {("count",): "COUNT", ("storage",): "STORAGE"}),
    "postgres": (
        "PG",
        {("instances",): "INSTANCES", ("storage",): "STORAGE", ("walStorage",): "WAL_STORAGE"},
    ),
    "redis": ("REDIS", {("replicas",): "REPLICAS", ("storage",): "STORAGE"}),
}
_AIRFLOW_COMPONENTS = {
    "apiServer": "API_SERVER",
    "scheduler": "SCHEDULER",
    "dagProcessor": "DAG_PROCESSOR",
    "triggerer": "TRIGGERER",
}

_COUNT_SUFFIXES = ("_REPLICAS", "_COUNT", "_INSTANCES")
_STORAGE_SUFFIX = "_STORAGE"
_CPU_RE = re.compile(r"^(?:[1-9][0-9]*m|[0-9]+(?:\.[0-9]{1,3})?)$")
_BYTES_RE = re.compile(r"^([1-9][0-9]*)(Ki|Mi|Gi|Ti)$")
_UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}
_DECIMAL_UNITS = {"": 1, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}

# Must equal the limits on the platform and osdu NodePools in software/components/nodepools.
POOL_LIMITS = {"platform": (64_000, 256 * 2**30), "osdu": (64_000, 256 * 2**30)}
# The largest node each pool's sku-cpu allows; the D family carries 4GiB per vCPU.
NODE_SIZES = {"platform": (8_000, 32 * 2**30), "osdu": (16_000, 64 * 2**30)}
# Each of these requires a node per pod through hostname anti-affinity.
_ONE_PER_NODE = {"ES": "ES_COUNT", "PG": "PG_INSTANCES", "REDIS": "REDIS_REPLICAS"}
MIN_CPU_REQUEST = 100
# The Postgres cluster keeps one synchronous replica, which needs a second instance.
MIN_PG_INSTANCES = 2
# Twice max_slot_wal_keep_size in software/components/postgres/cluster.yaml.
MIN_PG_WAL_STORAGE = "4Gi"


def field_paths(component: str) -> dict[tuple[str, ...], str]:
    """Each field a `resources:` entry for the component takes, mapped to its key."""

    if component == "airflow":
        return {
            (part, *path): f"AIRFLOW_{name}_{suffix}"
            for part, name in _AIRFLOW_COMPONENTS.items()
            for path, suffix in _RESOURCE_PATHS.items()
        }
    if component in _MIDDLEWARE_PATHS:
        prefix, extra = _MIDDLEWARE_PATHS[component]
    else:
        prefix, extra = image_lock_key(component), {("replicas",): "REPLICAS"}
    return {path: f"{prefix}_{suffix}" for path, suffix in {**_RESOURCE_PATHS, **extra}.items()}


def _leaves(value: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    if isinstance(value, Mapping) and value:
        for key, child in value.items():
            yield from _leaves(child, (*path, str(key)))
    else:
        yield path, value


def _value(key: str, label: str, value: Any) -> str:
    if key.endswith(_COUNT_SUFFIXES):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise SizingError(f"{label}: {value!r} must be a whole number of at least 1")
        return str(value)
    if "_CPU_" in key:
        text = (
            str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value
        )
        if not isinstance(text, str) or not _CPU_RE.fullmatch(text):
            raise SizingError(f"{label}: {value!r} must be a CPU quantity such as 500m or 2")
        return text
    if not isinstance(value, str) or not _BYTES_RE.fullmatch(value):
        raise SizingError(
            f"{label}: {value!r} must be a whole number with a unit, Ki, Mi, Gi, or Ti, such as 2Gi"
        )
    return value


def overrides(resources: Mapping[str, Any]) -> dict[str, str]:
    """The ConfigMap keys a `resources:` section sets, after every check that needs no cluster."""

    known = sizing_defaults()
    values: dict[str, str] = {}
    for component, fields in resources.items():
        if component not in known:
            raise SizingError(
                f"resources: unknown component {component!r}; known components: "
                + ", ".join(sorted(known))
            )
        paths = field_paths(component)
        for path, value in _leaves(fields):
            label = ".".join(("resources", component, *path))
            key = paths.get(path)
            if key is None and value == {} and any(p[: len(path)] == path for p in paths):
                continue
            if key is None:
                raise SizingError(
                    f"{label}: unknown field; {component} takes "
                    + ", ".join(".".join(p) for p in paths)
                )
            values[key] = _value(key, label, value)
    check(values)
    return values


def millicores(quantity: str) -> int:
    return int(quantity[:-1]) if quantity.endswith("m") else round(float(quantity) * 1000)


def to_bytes(quantity: str) -> int:
    """A Kubernetes memory or storage quantity in bytes, binary or decimal suffix."""

    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|k|M|G|T|)", quantity)
    if not match:
        raise SizingError(f"cannot read quantity {quantity!r}")
    number, unit = match.groups()
    return round(float(number) * (_UNITS.get(unit) or _DECIMAL_UNITS[unit]))


def effective(values: Mapping[str, str]) -> dict[str, str]:
    """Every key with the value the manifests render: the override, else the default."""

    merged = {key: value for keys in sizing_defaults().values() for key, value in keys.items()}
    merged.update(values)
    return merged


def _pods(sized: Mapping[str, str]) -> dict[str, list[tuple[str, int]]]:
    """Each pool's sized workloads as a key prefix and its pod count."""

    return {
        "platform": [
            ("ES", int(sized["ES_COUNT"])),
            ("PG", int(sized["PG_INSTANCES"])),
            # One primary beside the replicas.
            ("REDIS", int(sized["REDIS_REPLICAS"]) + 1),
            *((f"AIRFLOW_{name}", 1) for name in _AIRFLOW_COMPONENTS.values()),
        ],
        "osdu": [
            (image_lock_key(s), int(sized[f"{image_lock_key(s)}_REPLICAS"]))
            for s in sizing_services()
        ],
    }


def field_name(key: str) -> str:
    """The `resources:` field that sets a key, as an operator writes it."""

    for component in sizing_defaults():
        for path, candidate in field_paths(component).items():
            if candidate == key:
                return ".".join((component, *path))
    return key


def _said(sized: Mapping[str, str], key: str) -> str:
    return f"{field_name(key)} {sized[key]}"


def check(values: Mapping[str, str]) -> None:
    """Refuse values the cluster would reject or could not schedule, all problems at once."""

    sized = effective(values)
    problems: list[str] = []
    for key in sorted(k for k in sized if k.endswith("_CPU_REQUEST")):
        prefix = key.removesuffix("_CPU_REQUEST")
        memory = f"{prefix}_MEMORY_REQUEST"
        if millicores(sized[key]) < MIN_CPU_REQUEST:
            problems.append(
                f"{_said(sized, key)} is below the {MIN_CPU_REQUEST}m AKS Automatic admits"
            )
        if millicores(sized[key]) > millicores(sized[f"{prefix}_CPU_LIMIT"]):
            problems.append(f"{_said(sized, key)} exceeds {_said(sized, f'{prefix}_CPU_LIMIT')}")
        if to_bytes(sized[memory]) > to_bytes(sized[f"{prefix}_MEMORY_LIMIT"]):
            problems.append(
                f"{_said(sized, memory)} exceeds {_said(sized, f'{prefix}_MEMORY_LIMIT')}"
            )
    if int(sized["PG_INSTANCES"]) < MIN_PG_INSTANCES:
        problems.append(
            f"{_said(sized, 'PG_INSTANCES')} is below {MIN_PG_INSTANCES}; "
            "Postgres keeps one synchronous replica"
        )
    if to_bytes(sized["PG_WAL_STORAGE"]) < to_bytes(MIN_PG_WAL_STORAGE):
        problems.append(
            f"{_said(sized, 'PG_WAL_STORAGE')} is below {MIN_PG_WAL_STORAGE}; a replication "
            "slot alone may keep 2GB of WAL"
        )
    for pool, pods in _pods(sized).items():
        node_cpu, node_memory = NODE_SIZES[pool]
        nodes = min(POOL_LIMITS[pool][0] // node_cpu, POOL_LIMITS[pool][1] // node_memory)
        for prefix, count in pods:
            node = f"one {pool} node of {node_cpu // 1000} CPU and {node_memory // 2**30}Gi"
            if millicores(sized[f"{prefix}_CPU_REQUEST"]) >= node_cpu:
                problems.append(f"{_said(sized, f'{prefix}_CPU_REQUEST')} does not fit {node}")
            if to_bytes(sized[f"{prefix}_MEMORY_REQUEST"]) >= node_memory:
                problems.append(f"{_said(sized, f'{prefix}_MEMORY_REQUEST')} does not fit {node}")
            if prefix in _ONE_PER_NODE and count > nodes:
                problems.append(
                    f"{_said(sized, _ONE_PER_NODE[prefix])} needs {count} {pool} nodes, one "
                    f"per pod, and the pool holds at most {nodes}"
                )
        cpu = sum(millicores(sized[f"{p}_CPU_REQUEST"]) * n for p, n in pods)
        memory = sum(to_bytes(sized[f"{p}_MEMORY_REQUEST"]) * n for p, n in pods)
        cpu_limit, memory_limit = POOL_LIMITS[pool]
        if cpu > cpu_limit or memory > memory_limit:
            problems.append(
                f"requests on the {pool} NodePool total {cpu}m CPU and {memory // 2**20}Mi "
                f"memory, past its {cpu_limit // 1000} CPU and {memory_limit // 2**30}Gi limit"
            )
    if problems:
        raise SizingError("; ".join(problems))


# Each storage key, with the labels that select the claims it sized.
_STORAGE_CLAIMS = {
    "ES_STORAGE": {"elasticsearch.k8s.elastic.co/cluster-name": "elasticsearch"},
    "PG_STORAGE": {"cnpg.io/cluster": "postgresql", "cnpg.io/pvcRole": "PG_DATA"},
    "PG_WAL_STORAGE": {"cnpg.io/cluster": "postgresql", "cnpg.io/pvcRole": "PG_WAL"},
    "REDIS_STORAGE": {"app.kubernetes.io/name": "redis"},
}
# A StatefulSet's claim template is immutable, so the chart cannot resize these.
_FIXED_STORAGE = {"REDIS_STORAGE"}
STORAGE_NAMESPACE = "platform"


def live_storage(claims: list[dict]) -> dict[str, str]:
    """The largest size each storage key's claims request, for the keys with claims."""

    sizes: dict[str, str] = {}
    for key, selector in _STORAGE_CLAIMS.items():
        requested = [
            claim["spec"]["resources"]["requests"]["storage"]
            for claim in claims
            if selector.items() <= (claim["metadata"].get("labels") or {}).items()
        ]
        if requested:
            sizes[key] = max(requested, key=to_bytes)
    return sizes


def check_live(values: Mapping[str, str], live: Mapping[str, str]) -> None:
    """Refuse a storage size the volumes already on the cluster cannot take."""

    sized = effective(values)
    problems = []
    for key, current in sorted(live.items()):
        wanted = sized[key]
        if key in _FIXED_STORAGE and to_bytes(wanted) != to_bytes(current):
            problems.append(
                f"{_said(sized, key)} differs from the {current} its volumes were created with; "
                "it is set only when the environment is created"
            )
        elif to_bytes(wanted) < to_bytes(current):
            problems.append(
                f"{_said(sized, key)} is smaller than the {current} its volumes have; "
                "storage only grows"
            )
    if problems:
        raise SizingError("; ".join(problems))


def _read(args: list[str]) -> str:
    result = run_process(
        ["kubectl", *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or "kubectl failed"
        raise SizingError(f"could not read {args[1]}: {detail}")
    return result.stdout


def read_claims() -> list[dict]:
    return json.loads(_read(["get", "pvc", "-n", STORAGE_NAMESPACE, "-o", "json"]))["items"]


def sizing_configmap(values: Mapping[str, str]) -> str:
    return yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": SIZING_CONFIGMAP,
                "namespace": IMAGE_LOCK_NAMESPACE,
                "labels": {
                    "app.kubernetes.io/managed-by": "osdu-spi-stack",
                    # Flux re-reconciles the Kustomizations that read it on change.
                    "reconcile.fluxcd.io/watch": "Enabled",
                },
            },
            "data": dict(sorted(values.items())),
        },
        sort_keys=False,
    )


def check_storage(values: Mapping[str, str]) -> None:
    """Refuse storage the cluster's existing claims cannot take; run before any cluster write."""

    check_live(values, live_storage(read_claims()))


def apply_sizing(values: Mapping[str, str]) -> None:
    """Make `spi-sizing` hold these overrides; with none, remove it."""

    if values:
        console.print("\n[bold]Writing environment sizing...[/bold]")
        content = sizing_configmap(values)
        display_yaml(content, f"ConfigMap: {SIZING_CONFIGMAP}")
        kubectl_apply_yaml(content, f"apply {SIZING_CONFIGMAP} ConfigMap")
        display_result(f"{SIZING_CONFIGMAP} ConfigMap sets {len(values)} sizing keys")
        return
    present = _read(
        [
            "get",
            "configmap",
            SIZING_CONFIGMAP,
            "-n",
            IMAGE_LOCK_NAMESPACE,
            "--ignore-not-found",
            "-o",
            "name",
        ]
    )
    if present.strip():
        run_command(
            ["kubectl", "delete", "configmap", SIZING_CONFIGMAP, "-n", IMAGE_LOCK_NAMESPACE],
            description="remove sizing overrides; every key returns to its default",
        )
        display_result(f"{SIZING_CONFIGMAP} ConfigMap removed")
