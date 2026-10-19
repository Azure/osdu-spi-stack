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
"""

from spi.images import IMAGE_REGISTRY, image_lock_key

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
