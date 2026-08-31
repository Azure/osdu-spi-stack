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
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

GITLAB_HOST = "https://community.opengroup.org"
GHCR_HOST = "ghcr.io"
# Fork deploys may pin only images published under these GHCR owners.
GHCR_ALLOWED_OWNERS = ("azure",)
DEFAULT_IMAGE_BRANCH = "master"
IMAGE_LOCK_CONFIGMAP = "osdu-image-lock"
IMAGE_LOCK_NAMESPACE = "osdu-flux"
SCHEMA_SERVICE_NAME = "schema"
SCHEMA_LOAD_SERVICE_NAME = "schema-load"

_SHA_TAG_RE = re.compile(r"^[0-9a-f]{40}$")
_MANIFEST_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class ImageResolutionError(RuntimeError):
    """Raised when one or more OSDU image tags cannot be resolved."""


class ImageNotFoundError(ImageResolutionError):
    """Raised when a requested registry repository or tag does not exist."""


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
    "schema-load": ImageRegistryEntry(
        26,
        "schema-service-schema-load",
        "schema-load/job.yaml",
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


def gitlab_get(url: str, attempts: int = 3):
    """GET a GitLab API URL and return parsed JSON.

    Retries transient network failures (timeouts, connection resets) with a
    short backoff: the community registry intermittently times out, and a
    single blip should not abort an entire `spi up` run.
    """

    req = urllib.request.Request(url, headers={"User-Agent": "spi-stack-resolver"})
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
                return json.loads(resp.read())
        except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
            # HTTP error responses (4xx/5xx) are URLError subclasses but
            # indicate a server-side answer, not a transient network blip;
            # retry only non-HTTP failures and 5xx.
            if isinstance(exc, urllib.error.HTTPError) and exc.code < 500:
                raise
            last_error = exc
            if attempt < attempts:
                time.sleep(5 * attempt)
    raise ImageResolutionError(f"GitLab API unreachable after {attempts} attempts: {last_error}")


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


def _registry_repository(project_id: int, image_name: str) -> dict | None:
    repos = _registry_repositories(project_id, image_name)
    return next((repo for repo in repos if repo.get("name") == image_name), None)


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
    repo = _registry_repository(entry.project_id, image_name)
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


def resolve_image_tag(
    service_name: str,
    entry: ImageRegistryEntry,
    branch: str,
    tag: str,
) -> ResolvedImage:
    """Resolve a service image only if the exact tag exists."""

    image_name = f"{entry.image}-{branch}"
    repo = _registry_repository(entry.project_id, image_name)
    if not repo:
        raise ImageResolutionError(f"{service_name}: registry repository {image_name!r} not found")

    try:
        detail = _tag_detail(entry.project_id, repo["id"], tag)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ImageResolutionError(
                f"{service_name}: tag {tag!r} not found in {image_name!r}"
            ) from exc
        raise

    return ResolvedImage(
        name=service_name,
        repository=repo["location"],
        tag=detail["name"],
        created_at=detail.get("created_at", ""),
        digest=detail.get("digest", ""),
    )


def resolve_image_commit(
    service_name: str,
    entry: ImageRegistryEntry,
    branch: str,
    sha: str,
) -> ResolvedImage:
    """Resolve a service image only if a tag matches the given commit.

    Pipeline tags are commit SHAs of varying length (full or CI short SHA),
    so a tag matches when it equals the commit or is a prefix of it.
    """

    image_name = f"{entry.image}-{branch}"
    repo = _registry_repository(entry.project_id, image_name)
    if not repo:
        raise ImageNotFoundError(f"{service_name}: registry repository {image_name!r} not found")

    tags = _registry_tags(entry.project_id, repo["id"])
    matches = [
        tag["name"]
        for tag in tags
        if tag.get("name") and len(tag["name"]) >= 7 and sha.startswith(tag["name"])
    ]
    if not matches:
        raise ImageNotFoundError(f"{service_name}: no tag for commit {sha[:12]} in {image_name!r}")

    tag = max(matches, key=len)
    try:
        detail = _tag_detail(entry.project_id, repo["id"], tag)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ImageNotFoundError(
                f"{service_name}: tag {tag!r} not found in {image_name!r}"
            ) from exc
        raise
    return ResolvedImage(
        name=service_name,
        repository=repo["location"],
        tag=detail["name"],
        created_at=detail.get("created_at", ""),
        digest=detail.get("digest", ""),
    )


def resolve_images(
    branch: str = DEFAULT_IMAGE_BRANCH,
    names: Iterable[str] | None = None,
) -> dict[str, ResolvedImage]:
    """Resolve all requested images atomically.

    Raises ImageResolutionError if any requested image cannot be resolved.
    """

    requested = list(names or IMAGE_REGISTRY.keys())
    schema_load_requested = SCHEMA_LOAD_SERVICE_NAME in requested
    resolved: dict[str, ResolvedImage] = {}
    errors: list[str] = []

    for name in requested:
        if name == SCHEMA_LOAD_SERVICE_NAME or (
            name == SCHEMA_SERVICE_NAME and schema_load_requested
        ):
            continue
        entry = IMAGE_REGISTRY[name]
        try:
            resolved[name] = resolve_image(name, entry, branch)
        except Exception as exc:
            errors.append(str(exc))

    if schema_load_requested:
        try:
            schema_image = resolve_image(
                SCHEMA_SERVICE_NAME,
                IMAGE_REGISTRY[SCHEMA_SERVICE_NAME],
                branch,
            )
            if SCHEMA_SERVICE_NAME in requested:
                resolved[SCHEMA_SERVICE_NAME] = schema_image
        except Exception as exc:
            if SCHEMA_SERVICE_NAME in requested:
                errors.append(str(exc))
                errors.append(f"{SCHEMA_LOAD_SERVICE_NAME}: unable to resolve matching schema tag")
            else:
                errors.append(
                    f"{SCHEMA_LOAD_SERVICE_NAME}: unable to resolve matching schema tag: {exc}"
                )
        else:
            try:
                resolved[SCHEMA_LOAD_SERVICE_NAME] = resolve_image_tag(
                    SCHEMA_LOAD_SERVICE_NAME,
                    IMAGE_REGISTRY[SCHEMA_LOAD_SERVICE_NAME],
                    branch,
                    schema_image.tag,
                )
            except Exception as exc:
                errors.append(str(exc))

    if errors:
        raise ImageResolutionError("; ".join(errors))
    return {name: resolved[name] for name in requested}


def resolve_image_lock(branch: str = DEFAULT_IMAGE_BRANCH) -> dict[str, ResolvedImage]:
    """Resolve the images controlled by the live Flux image lock."""

    return resolve_images(branch=branch, names=image_lock_names())


def parse_image_digest_ref(ref: str) -> tuple[str, str]:
    """Split an image reference into (repository, digest), digest required.

    Tags are rejected outright: a fork deploy's identity is the manifest
    digest (ADR-031), and GHCR's ``sha-*`` tags are pruned after 30 days, so
    a tag reference would go stale under a live pin.
    """

    ref = ref.strip()
    if "@" not in ref:
        raise ImageResolutionError(
            f"image reference {ref!r} carries no digest; use "
            "<repository>@sha256:<digest> (tags are not accepted)"
        )
    repository, _, digest = ref.rpartition("@")
    if not _MANIFEST_DIGEST_RE.match(digest):
        raise ImageResolutionError(
            f"image reference {ref!r}: {digest!r} is not a sha256 manifest digest"
        )
    if not repository or "/" not in repository:
        raise ImageResolutionError(f"image reference {ref!r}: missing repository path")
    if ":" in repository.rsplit("/", 1)[-1]:
        raise ImageResolutionError(
            f"image reference {ref!r} carries a tag alongside the digest; "
            "pin by digest alone as <repository>@sha256:<digest>"
        )
    return repository, digest


def require_ghcr_repository(repository: str) -> None:
    """Enforce the ADR-031 GHCR owner allow-list on a pin's repository."""

    parts = repository.lower().split("/")
    if len(parts) < 3 or parts[0] != GHCR_HOST or parts[1] not in GHCR_ALLOWED_OWNERS:
        allowed = ", ".join(f"{GHCR_HOST}/{owner}" for owner in GHCR_ALLOWED_OWNERS)
        raise ImageResolutionError(
            f"repository {repository!r} is not an allow-listed fork image source; "
            f"expected an image under {allowed}"
        )


def resolve_ghcr_manifest(repository: str, digest: str, attempts: int = 3) -> None:
    """Assert the digest's manifest exists in GHCR before it is pinned.

    Uses the anonymous pull-token flow (fork packages are public). A missing
    manifest raises ImageNotFoundError; transient failures retry like
    ``gitlab_get`` and then raise ImageResolutionError, fail-closed.
    """

    path = repository[len(GHCR_HOST) + 1 :]
    token_url = f"https://{GHCR_HOST}/token?service={GHCR_HOST}&scope=" + urllib.parse.quote(
        f"repository:{path}:pull", safe=":"
    )
    manifest_url = f"https://{GHCR_HOST}/v2/{path}/manifests/{digest}"
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            token_req = urllib.request.Request(
                token_url, headers={"User-Agent": "spi-stack-resolver"}
            )
            with urllib.request.urlopen(token_req, timeout=15) as resp:  # nosec B310
                token = json.loads(resp.read()).get("token", "")
            manifest_req = urllib.request.Request(
                manifest_url,
                method="HEAD",
                headers={
                    "User-Agent": "spi-stack-resolver",
                    "Accept": _MANIFEST_ACCEPT,
                    "Authorization": f"Bearer {token}",
                },
            )
            with urllib.request.urlopen(manifest_req, timeout=15):  # nosec B310
                return
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ImageNotFoundError(
                    f"manifest {digest} not found in {repository}; confirm the run "
                    "pushed this digest and the package is public"
                ) from exc
            if exc.code < 500:
                raise ImageResolutionError(
                    f"GHCR refused the manifest check for {repository}@{digest}: HTTP {exc.code}"
                ) from exc
            last_error = exc
        except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(5 * attempt)
    raise ImageResolutionError(
        f"GHCR unreachable after {attempts} attempts checking {repository}@{digest}: {last_error}"
    )


def image_lock_missing_schema_load(lock_data: Mapping[str, str]) -> bool:
    """Report whether an existing lock predates schema-load's inclusion.

    A lock missing the composed ref counts as missing even when it already
    carries repository/tag: the schema-load Job substitutes the single
    ``_IMAGE_REF`` key (ADR-013), so a lock recorded before that key existed
    still needs backfilling.
    """

    key = image_lock_key(SCHEMA_LOAD_SERVICE_NAME)
    return not (
        lock_data.get(f"{key}_IMAGE_REPOSITORY")
        and lock_data.get(f"{key}_IMAGE_TAG")
        and lock_data.get(f"{key}_IMAGE_REF")
    )


def image_ref(repository: str, tag: str, digest: str) -> str:
    """Return the digest-first image reference, falling back to the tag."""

    return f"{repository}@{digest}" if digest else f"{repository}:{tag}"


def schema_load_lock_patch(
    lock_data: Mapping[str, str],
    branch: str = DEFAULT_IMAGE_BRANCH,
) -> dict[str, str]:
    """Return the loader entries missing from an existing image lock.

    Locks generated before schema-load joined the live lock carry a schema pin
    but no loader keys, and the Job requires them (ADR-013). The loader is
    resolved from the schema tag the lock already records, so the backfill
    keeps the loader on the running service's commit instead of jumping to the
    newest master build.
    """

    key = image_lock_key(SCHEMA_LOAD_SERVICE_NAME)
    existing_repository = lock_data.get(f"{key}_IMAGE_REPOSITORY", "")
    existing_tag = lock_data.get(f"{key}_IMAGE_TAG", "")
    if existing_repository and existing_tag:
        existing_digest = lock_data.get(f"{key}_IMAGE_DIGEST", "")
        return {
            f"{key}_IMAGE_REF": image_ref(
                existing_repository,
                existing_tag,
                existing_digest,
            )
        }

    schema_tag = lock_data.get(f"{image_lock_key(SCHEMA_SERVICE_NAME)}_IMAGE_TAG", "")
    if not schema_tag:
        raise ImageResolutionError(
            f"{SCHEMA_LOAD_SERVICE_NAME}: image lock records no schema image tag to match"
        )

    image = resolve_image_tag(
        SCHEMA_LOAD_SERVICE_NAME,
        IMAGE_REGISTRY[SCHEMA_LOAD_SERVICE_NAME],
        lock_data.get("IMAGE_BRANCH") or branch,
        schema_tag,
    )

    patch = {
        f"{key}_IMAGE": image.image,
        f"{key}_IMAGE_REPOSITORY": image.repository,
        f"{key}_IMAGE_TAG": image.tag,
        f"{key}_IMAGE_CREATED_AT": image.created_at,
        f"{key}_IMAGE_DIGEST": image.digest,
        f"{key}_IMAGE_REF": image_ref(image.repository, image.tag, image.digest),
    }
    count = lock_data.get("IMAGE_COUNT", "")
    if count.isdigit() and not existing_repository and not existing_tag:
        patch["IMAGE_COUNT"] = str(int(count) + 1)
    return patch


def _yaml_string(value: str) -> str:
    return json.dumps(str(value))


IMAGE_BRANCH_ANNOTATION = "spi-stack.osdu.dev/image-branch"
IMAGE_RESOLVED_AT_ANNOTATION = "spi-stack.osdu.dev/resolved-at"


def build_lock_data(
    resolved: dict[str, ResolvedImage], branch: str, timestamp: str
) -> dict[str, str]:
    """Return the complete image-lock ConfigMap ``data`` for a resolved image set.

    Shared by the YAML renderer below and the live-cluster compare-and-retry
    mutation in ``pins.py``, so both build the same keys from one place.
    """

    data: dict[str, str] = {
        "IMAGE_BRANCH": branch,
        "IMAGE_RESOLVED_AT": timestamp,
        "IMAGE_COUNT": str(len(resolved)),
    }
    for name in image_lock_names():
        image = resolved[name]
        key = image_lock_key(name)
        # A digest pin carries no tag; fall back to the digest ref so the
        # informational _IMAGE key never renders a dangling "repository:".
        data[f"{key}_IMAGE"] = (
            image.image if image.tag else image_ref(image.repository, image.tag, image.digest)
        )
        data[f"{key}_IMAGE_REPOSITORY"] = image.repository
        data[f"{key}_IMAGE_TAG"] = image.tag
        data[f"{key}_IMAGE_CREATED_AT"] = image.created_at
        data[f"{key}_IMAGE_DIGEST"] = image.digest
        data[f"{key}_IMAGE_REF"] = image_ref(image.repository, image.tag, image.digest)
    return data


def build_lock_annotations(branch: str, timestamp: str) -> dict[str, str]:
    """Return the base image-branch/resolved-at annotations for the lock."""

    return {
        IMAGE_BRANCH_ANNOTATION: branch,
        IMAGE_RESOLVED_AT_ANNOTATION: timestamp,
    }


def render_image_lock_configmap(
    resolved: dict[str, ResolvedImage],
    branch: str = DEFAULT_IMAGE_BRANCH,
    resolved_at: datetime | None = None,
    extra_annotations: Mapping[str, str] | None = None,
) -> str:
    """Render the Flux substitution ConfigMap for service image pins."""

    timestamp = (resolved_at or datetime.now(timezone.utc)).isoformat()
    data = build_lock_data(resolved, branch, timestamp)
    base_annotations = build_lock_annotations(branch, timestamp)

    lines = [
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        f"  name: {IMAGE_LOCK_CONFIGMAP}",
        f"  namespace: {IMAGE_LOCK_NAMESPACE}",
        "  labels:",
        "    app.kubernetes.io/managed-by: osdu-spi-stack",
        "  annotations:",
        f"    {IMAGE_BRANCH_ANNOTATION}: {_yaml_string(base_annotations[IMAGE_BRANCH_ANNOTATION])}",
        f"    {IMAGE_RESOLVED_AT_ANNOTATION}: "
        f"{_yaml_string(base_annotations[IMAGE_RESOLVED_AT_ANNOTATION])}",
    ]
    annotations = dict(extra_annotations or {})
    for key in sorted(annotations):
        lines.append(f"    {key}: {_yaml_string(annotations[key])}")
    lines.append("data:")
    for key in sorted(data):
        lines.append(f"  {key}: {_yaml_string(data[key])}")
    return "\n".join(lines) + "\n"
