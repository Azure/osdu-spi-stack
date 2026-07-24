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

"""OSDU community image resolution and image-lock rendering."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

GITLAB_HOST = "https://community.opengroup.org"
DEFAULT_IMAGE_BRANCH = "master"
IMAGE_LOCK_CONFIGMAP = "osdu-image-lock"
IMAGE_LOCK_NAMESPACE = "osdu-flux"

_SHA_TAG_RE = re.compile(r"^[0-9a-f]{40}$")


class ImageResolutionError(RuntimeError):
    """Raised when one or more OSDU image tags cannot be resolved."""


@dataclass(frozen=True)
class ImageRegistryEntry:
    """GitLab registry lookup metadata for one OSDU image."""

    project_id: int
    image: str
    file: str
    image_lock: bool = True


@dataclass(frozen=True)
class ResolvedImage:
    """One resolved OSDU image reference."""

    name: str
    repository: str
    tag: str
    created_at: str
    digest: str

    @property
    def image(self) -> str:
        return f"{self.repository}:{self.tag}"


# Service registry: maps service name to GitLab project ID, image base name,
# and the stack YAML file that carries the default image reference.
# Project IDs from community.opengroup.org GitLab.
IMAGE_REGISTRY: dict[str, ImageRegistryEntry] = {
    # Core services (software/stacks/osdu/services/)
    "partition": ImageRegistryEntry(221, "partition", "services/partition.yaml"),
    "entitlements": ImageRegistryEntry(400, "entitlements", "services/entitlements.yaml"),
    "legal": ImageRegistryEntry(74, "legal", "services/legal.yaml"),
    "schema": ImageRegistryEntry(26, "schema-service", "services/schema.yaml"),
    # The schema-load Job is intentionally not part of the live image lock.
    # A completed Kubernetes Job cannot be updated in place, so it remains a
    # Git default that the resolver script can refresh for new deployments.
    "schema-load": ImageRegistryEntry(
        26,
        "schema-service-schema-load",
        "schema-load/job.yaml",
        image_lock=False,
    ),
    "storage": ImageRegistryEntry(44, "storage", "services/storage.yaml"),
    "search": ImageRegistryEntry(19, "search-service", "services/search.yaml"),
    "indexer": ImageRegistryEntry(25, "indexer-service", "services/indexer.yaml"),
    "indexer-queue": ImageRegistryEntry(73, "indexer-queue", "services/indexer-queue.yaml"),
    "file": ImageRegistryEntry(90, "file", "services/file.yaml"),
    "workflow": ImageRegistryEntry(146, "ingestion-workflow", "services/workflow.yaml"),
    # Reference services (software/stacks/osdu/services-reference/)
    "crs-conversion": ImageRegistryEntry(
        22,
        "crs-conversion-service",
        "services-reference/crs-conversion.yaml",
    ),
    "crs-catalog": ImageRegistryEntry(
        21,
        "crs-catalog-service",
        "services-reference/crs-catalog.yaml",
    ),
    "unit": ImageRegistryEntry(5, "unit-service", "services-reference/unit.yaml"),
}


def image_lock_names() -> tuple[str, ...]:
    """Return service names controlled by the generated image lock."""

    return tuple(name for name, entry in IMAGE_REGISTRY.items() if entry.image_lock)


def image_lock_key(service_name: str) -> str:
    """Return the ConfigMap key prefix for one service."""

    return service_name.upper().replace("-", "_")


def gitlab_get(url: str):
    """GET a GitLab API URL and return parsed JSON."""

    req = urllib.request.Request(url, headers={"User-Agent": "spi-stack-resolver"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
        return json.loads(resp.read())


def _registry_repositories(project_id: int, image_name: str) -> list[dict]:
    repos: list[dict] = []
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {
                "per_page": 100,
                "page": page,
                "search": image_name,
            }
        )
        chunk = gitlab_get(
            f"{GITLAB_HOST}/api/v4/projects/{project_id}/registry/repositories?{query}"
        )
        if not chunk:
            break
        repos.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return repos


def _registry_tags(project_id: int, repo_id: int) -> list[dict]:
    tags: list[dict] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        chunk = gitlab_get(
            f"{GITLAB_HOST}/api/v4/projects/{project_id}/registry/repositories/"
            f"{repo_id}/tags?{query}"
        )
        if not chunk:
            break
        tags.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return tags


def _tag_detail(project_id: int, repo_id: int, tag: str) -> dict:
    quoted_tag = urllib.parse.quote(tag, safe="")
    return gitlab_get(
        f"{GITLAB_HOST}/api/v4/projects/{project_id}/registry/repositories/"
        f"{repo_id}/tags/{quoted_tag}"
    )


def _parse_gitlab_datetime(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _newest_immutable_tag(project_id: int, repo_id: int, tags: Iterable[dict]) -> dict | None:
    details = [_tag_detail(project_id, repo_id, tag["name"]) for tag in tags if tag.get("name")]
    immutable = [tag for tag in details if _SHA_TAG_RE.match(tag.get("name", ""))]
    candidates = immutable or details
    if not candidates:
        return None
    return max(candidates, key=lambda tag: _parse_gitlab_datetime(tag.get("created_at", "")))


def resolve_image(service_name: str, entry: ImageRegistryEntry, branch: str) -> ResolvedImage:
    """Resolve the newest immutable image tag for a service."""

    image_name = f"{entry.image}-{branch}"
    repos = _registry_repositories(entry.project_id, image_name)
    repo = next((r for r in repos if r.get("name") == image_name), None)
    if not repo:
        raise ImageResolutionError(f"{service_name}: registry repository {image_name!r} not found")

    tags = _registry_tags(entry.project_id, repo["id"])
    tag = _newest_immutable_tag(entry.project_id, repo["id"], tags)
    if not tag:
        raise ImageResolutionError(f"{service_name}: no tags found in {image_name!r}")

    return ResolvedImage(
        name=service_name,
        repository=repo["location"],
        tag=tag["name"],
        created_at=tag.get("created_at", ""),
        digest=tag.get("digest", ""),
    )


def resolve_images(
    branch: str = DEFAULT_IMAGE_BRANCH,
    names: Iterable[str] | None = None,
) -> dict[str, ResolvedImage]:
    """Resolve all requested images atomically.

    Raises ImageResolutionError if any requested image cannot be resolved.
    """

    requested = list(names or IMAGE_REGISTRY.keys())
    resolved: dict[str, ResolvedImage] = {}
    errors: list[str] = []

    for name in requested:
        entry = IMAGE_REGISTRY[name]
        try:
            resolved[name] = resolve_image(name, entry, branch)
        except Exception as exc:
            errors.append(str(exc))

    if errors:
        raise ImageResolutionError("; ".join(errors))
    return resolved


def resolve_image_lock(branch: str = DEFAULT_IMAGE_BRANCH) -> dict[str, ResolvedImage]:
    """Resolve the images controlled by the live Flux image lock."""

    return resolve_images(branch=branch, names=image_lock_names())


def _yaml_string(value: str) -> str:
    return json.dumps(str(value))


def render_image_lock_configmap(
    resolved: dict[str, ResolvedImage],
    branch: str = DEFAULT_IMAGE_BRANCH,
    resolved_at: datetime | None = None,
) -> str:
    """Render the Flux substitution ConfigMap for service image pins."""

    timestamp = (resolved_at or datetime.now(timezone.utc)).isoformat()
    data: dict[str, str] = {
        "IMAGE_BRANCH": branch,
        "IMAGE_RESOLVED_AT": timestamp,
        "IMAGE_COUNT": str(len(resolved)),
    }
    for name in image_lock_names():
        image = resolved[name]
        key = image_lock_key(name)
        data[f"{key}_IMAGE"] = image.image
        data[f"{key}_IMAGE_REPOSITORY"] = image.repository
        data[f"{key}_IMAGE_TAG"] = image.tag
        data[f"{key}_IMAGE_CREATED_AT"] = image.created_at
        data[f"{key}_IMAGE_DIGEST"] = image.digest

    lines = [
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        f"  name: {IMAGE_LOCK_CONFIGMAP}",
        f"  namespace: {IMAGE_LOCK_NAMESPACE}",
        "  labels:",
        "    app.kubernetes.io/managed-by: osdu-spi-stack",
        "  annotations:",
        f"    spi-stack.osdu.dev/image-branch: {_yaml_string(branch)}",
        f"    spi-stack.osdu.dev/resolved-at: {_yaml_string(timestamp)}",
        "data:",
    ]
    for key in sorted(data):
        lines.append(f"  {key}: {_yaml_string(data[key])}")
    return "\n".join(lines) + "\n"
