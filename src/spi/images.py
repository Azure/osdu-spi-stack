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
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable

GITLAB_HOST = "https://community.opengroup.org"
GITHUB_API_HOST = "https://api.github.com"
GHCR_HOST = "https://ghcr.io"
DEFAULT_IMAGE_BRANCH = "master"
DEFAULT_GHCR_ORG = "yuchen-osdu"
DEFAULT_GHCR_TAG = "main-snapshot"
IMAGE_LOCK_CONFIGMAP = "osdu-image-lock"
IMAGE_LOCK_NAMESPACE = "osdu-flux"

_SHA_TAG_RE = re.compile(r"^[0-9a-f]{40}$")
_BEARER_PARAMETER_RE = re.compile(r'([a-zA-Z]+)="([^"]*)"')


class ImageSource(str, Enum):
    COMMUNITY = "community"
    GHCR = "ghcr"


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
        return (
            f"{self.repository}@{self.digest}" if self.digest else f"{self.repository}:{self.tag}"
        )


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


def _urlopen_with_retry(
    request: urllib.request.Request,
    *,
    attempts: int = 3,
    timeout: int = 15,
):
    """Open an HTTP request, retrying transient network and server failures."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return urllib.request.urlopen(request, timeout=timeout)  # nosec B310
        except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code < 500:
                raise
            last_error = exc
            if attempt < attempts:
                time.sleep(5 * attempt)
    raise ImageResolutionError(f"Registry request failed after {attempts} attempts: {last_error}")


def gitlab_get(url: str, attempts: int = 3):
    """GET a GitLab API URL and return parsed JSON."""

    req = urllib.request.Request(url, headers={"User-Agent": "spi-stack-resolver"})
    with _urlopen_with_retry(req, attempts=attempts) as resp:
        return json.loads(resp.read())


def github_get(url: str):
    """GET a public GitHub API URL, using an available token when present."""

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "spi-stack-resolver",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with _urlopen_with_retry(req) as resp:
        return json.loads(resp.read())


def _ghcr_manifest_digest(repository: str, tag: str) -> str:
    """Resolve one public GHCR tag to its immutable OCI manifest digest."""

    tag_path = urllib.parse.quote(tag, safe="")
    manifest_url = f"{GHCR_HOST}/v2/{repository}/manifests/{tag_path}"
    headers = {
        "Accept": (
            "application/vnd.oci.image.index.v1+json,"
            "application/vnd.docker.distribution.manifest.list.v2+json,"
            "application/vnd.oci.image.manifest.v1+json,"
            "application/vnd.docker.distribution.manifest.v2+json"
        ),
        "User-Agent": "spi-stack-resolver",
    }

    def request(auth_token: str = ""):
        request_headers = dict(headers)
        if auth_token:
            request_headers["Authorization"] = f"Bearer {auth_token}"
        req = urllib.request.Request(manifest_url, headers=request_headers)
        return _urlopen_with_retry(req)

    try:
        response = request()
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise ImageResolutionError(
                f"{repository}:{tag}: GHCR manifest request failed with HTTP "
                f"{exc.code} {exc.reason}"
            ) from exc
        challenge = exc.headers.get("WWW-Authenticate", "")
        if not challenge.lower().startswith("bearer "):
            raise ImageResolutionError(
                f"GHCR did not return a Bearer challenge for {repository}:{tag}"
            ) from exc
        params = dict(_BEARER_PARAMETER_RE.findall(challenge))
        realm = params.pop("realm", "")
        if not realm:
            raise ImageResolutionError(
                f"GHCR Bearer challenge has no token realm for {repository}:{tag}"
            ) from exc
        token_url = f"{realm}?{urllib.parse.urlencode(params)}"
        token_req = urllib.request.Request(
            token_url,
            headers={"User-Agent": "spi-stack-resolver"},
        )
        with _urlopen_with_retry(token_req) as token_resp:
            token_data = json.loads(token_resp.read())
        auth_token = token_data.get("token") or token_data.get("access_token")
        if not auth_token:
            raise ImageResolutionError(
                f"GHCR did not issue a pull token for {repository}:{tag}"
            ) from exc
        try:
            response = request(auth_token)
        except urllib.error.HTTPError as retry_exc:
            raise ImageResolutionError(
                f"{repository}:{tag}: GHCR manifest request failed with HTTP "
                f"{retry_exc.code} {retry_exc.reason}"
            ) from retry_exc

    with response:
        digest = response.headers.get("Docker-Content-Digest", "")
        response.read()
    if not digest.startswith("sha256:"):
        raise ImageResolutionError(f"GHCR returned no immutable digest for {repository}:{tag}")
    return digest


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


def resolve_ghcr_tag_image(service_name: str, org: str, tag: str) -> ResolvedImage:
    """Resolve an exact public GHCR tag to its immutable manifest digest."""

    registry_path = f"{org.lower()}/{service_name.lower()}"
    digest = _ghcr_manifest_digest(registry_path, tag)
    return ResolvedImage(
        name=service_name,
        repository=f"ghcr.io/{registry_path}",
        tag=tag,
        created_at="",
        digest=digest,
    )


def resolve_ghcr_ref_image(service_name: str, org: str, ref: str) -> ResolvedImage:
    """Resolve an SPI service Git ref to its public GHCR image digest."""

    repo = service_name
    quoted_ref = urllib.parse.quote(ref, safe="")
    commit = github_get(f"{GITHUB_API_HOST}/repos/{org}/{repo}/commits/{quoted_ref}")
    sha = commit.get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ImageResolutionError(
            f"{service_name}: GitHub ref {org}/{repo}@{ref!r} returned no commit SHA"
        )
    tag = f"sha-{sha[:12]}"
    registry_path = f"{org.lower()}/{repo.lower()}"
    digest = _ghcr_manifest_digest(registry_path, tag)
    committed_at = commit.get("commit", {}).get("committer", {}).get("date", "") or commit.get(
        "commit", {}
    ).get("author", {}).get("date", "")
    return ResolvedImage(
        name=service_name,
        repository=f"ghcr.io/{registry_path}",
        tag=tag,
        created_at=committed_at,
        digest=digest,
    )


def resolve_images(
    source: ImageSource = ImageSource.GHCR,
    tag: str | None = None,
    ref: str | None = None,
    org: str = DEFAULT_GHCR_ORG,
    names: Iterable[str] | None = None,
) -> dict[str, ResolvedImage]:
    """Resolve all requested images atomically.

    Raises ImageResolutionError if any requested image cannot be resolved.
    """

    source = ImageSource(source)
    if source == ImageSource.GHCR:
        if tag and ref:
            raise ImageResolutionError("GHCR image tag and Git ref are mutually exclusive")
        resolved_tag = tag or ("" if ref else DEFAULT_GHCR_TAG)
        resolved_ref = ref or ""
    else:
        if tag:
            raise ImageResolutionError("Exact image tags are supported only for GHCR")
        resolved_tag = ""
        resolved_ref = ref or DEFAULT_IMAGE_BRANCH

    requested = list(names or IMAGE_REGISTRY.keys())
    resolved: dict[str, ResolvedImage] = {}
    errors: list[str] = []

    for name in requested:
        entry = IMAGE_REGISTRY[name]
        try:
            if source == ImageSource.GHCR:
                if resolved_ref:
                    resolved[name] = resolve_ghcr_ref_image(name, org, resolved_ref)
                else:
                    resolved[name] = resolve_ghcr_tag_image(name, org, resolved_tag)
            else:
                resolved[name] = resolve_image(name, entry, resolved_ref)
        except Exception as exc:
            message = str(exc)
            errors.append(message if message.startswith(f"{name}:") else f"{name}: {message}")

    if errors:
        raise ImageResolutionError("; ".join(errors))
    return resolved


def resolve_image_lock(
    source: ImageSource = ImageSource.GHCR,
    tag: str | None = None,
    ref: str | None = None,
    org: str = DEFAULT_GHCR_ORG,
) -> dict[str, ResolvedImage]:
    """Resolve the images controlled by the live Flux image lock."""

    return resolve_images(source=source, tag=tag, ref=ref, org=org, names=image_lock_names())


def _yaml_string(value: str) -> str:
    return json.dumps(str(value))


def render_image_lock_configmap(
    resolved: dict[str, ResolvedImage],
    source: ImageSource = ImageSource.GHCR,
    tag: str | None = None,
    ref: str | None = None,
    org: str = DEFAULT_GHCR_ORG,
    resolved_at: datetime | None = None,
) -> str:
    """Render the Flux substitution ConfigMap for service image pins."""

    timestamp = (resolved_at or datetime.now(timezone.utc)).isoformat()
    source = ImageSource(source)
    if source == ImageSource.GHCR:
        if tag and ref:
            raise ImageResolutionError("GHCR image tag and Git ref are mutually exclusive")
        resolved_tag = tag or ("" if ref else DEFAULT_GHCR_TAG)
        resolved_ref = ref or ""
    else:
        if tag:
            raise ImageResolutionError("Exact image tags are supported only for GHCR")
        resolved_tag = ""
        resolved_ref = ref or DEFAULT_IMAGE_BRANCH

    data: dict[str, str] = {
        "IMAGE_SOURCE": source.value,
        "IMAGE_ORG": org if source == ImageSource.GHCR else "",
        "IMAGE_TAG": resolved_tag,
        "IMAGE_REF": resolved_ref,
        "IMAGE_BRANCH": resolved_ref if source == ImageSource.COMMUNITY else "",
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
        f"    spi-stack.osdu.dev/image-source: {_yaml_string(source.value)}",
        f"    spi-stack.osdu.dev/image-tag: {_yaml_string(resolved_tag)}",
        f"    spi-stack.osdu.dev/image-ref: {_yaml_string(resolved_ref)}",
        f"    spi-stack.osdu.dev/resolved-at: {_yaml_string(timestamp)}",
        "data:",
    ]
    for key in sorted(data):
        lines.append(f"  {key}: {_yaml_string(data[key])}")
    return "\n".join(lines) + "\n"
