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

"""OSDU community and fork image resolution and image-lock rendering."""

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
GITHUB_API_HOST = "https://api.github.com"
DEFAULT_IMAGE_BRANCH = "master"
# The template moves this tag to each push build of main.
FORK_MAIN_TAG = "main-snapshot"
FORK_MAIN_BRANCH = "main"
# How many main commits are searched for the one whose build main-snapshot names.
FORK_COMMIT_SEARCH_DEPTH = 30
IMAGE_LOCK_CONFIGMAP = "osdu-image-lock"
IMAGE_LOCK_NAMESPACE = "osdu-flux"

_SHA_TAG_RE = re.compile(r"^[0-9a-f]{40}$")
_MANIFEST_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPO_PATH_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/(?!\.\.?$)[A-Za-z0-9_.-]+$")
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
class CompanionImage:
    """An image built and advanced with a primary service image."""

    name: str
    package_suffix: str
    canonical_required: bool = True
    pin_required: bool = True
    runtime_enabled_key: str = ""
    runtime_replica_count_key: str = ""
    runtime_enabled_replicas: int = 1


@dataclass(frozen=True)
class ImageRegistryEntry:
    """GitLab registry lookup metadata for one OSDU image."""

    project_id: int
    image: str
    file: str
    image_lock: bool = True
    companion: CompanionImage | None = None


@dataclass(frozen=True)
class ResolvedImage:
    """One resolved OSDU image reference."""

    name: str
    repository: str
    tag: str
    created_at: str
    digest: str
    # The suite image built from the same fork commit; empty when none was published.
    acceptance_digest: str = ""

    @property
    def image(self) -> str:
        return f"{self.repository}:{self.tag}"


# Service name to community GitLab project ID, image base name, and the stack
# file carrying the default image reference.
IMAGE_REGISTRY: dict[str, ImageRegistryEntry] = {
    "partition": ImageRegistryEntry(221, "partition", "services/partition.yaml"),
    "entitlements": ImageRegistryEntry(400, "entitlements", "services/entitlements.yaml"),
    "legal": ImageRegistryEntry(74, "legal", "services/legal.yaml"),
    "schema": ImageRegistryEntry(
        26,
        "schema-service",
        "services/schema.yaml",
        companion=CompanionImage(
            name="schema-load",
            package_suffix="-load",
            pin_required=False,
        ),
    ),
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
    "seismic": ImageRegistryEntry(
        395,
        "seismic-store-service",
        "services-ddms/seismic.yaml",
        companion=CompanionImage(
            name="seismic-restore",
            package_suffix="-restore",
            canonical_required=False,
        ),
    ),
    "seismic-restore": ImageRegistryEntry(
        395,
        "seismic-store-service-restore",
        "services-ddms/seismic-restore.yaml",
    ),
    "workflow": ImageRegistryEntry(146, "ingestion-workflow", "services/workflow.yaml"),
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


def companion_image(service_name: str) -> CompanionImage | None:
    """Return the companion declared by a primary service, if any."""

    entry = IMAGE_REGISTRY.get(service_name)
    return entry.companion if entry else None


def companion_primary(service_name: str) -> str:
    """Return the primary service governing ``service_name``."""

    for name, entry in IMAGE_REGISTRY.items():
        if entry.companion and entry.companion.name == service_name:
            return name
    return service_name


def is_companion_image(service_name: str) -> bool:
    """Whether ``service_name`` is owned by another registry entry."""

    return companion_primary(service_name) != service_name


def service_image_names() -> tuple[str, ...]:
    """Return image names operators may manage directly."""

    return tuple(name for name in IMAGE_REGISTRY if not is_companion_image(name))


def paired_image_names(service_name: str) -> tuple[str, ...]:
    """Return the primary image followed by its declared companion."""

    primary = companion_primary(service_name)
    companion = companion_image(primary)
    return (primary, companion.name) if companion else (primary,)


def companion_runtime_patch(service_name: str, enabled: bool) -> dict[str, str]:
    """Return registry-declared runtime controls for a companion image."""

    primary = companion_primary(service_name)
    companion = companion_image(primary)
    if companion is None or companion.name != service_name or not companion.runtime_enabled_key:
        return {}
    patch = {companion.runtime_enabled_key: str(enabled).lower()}
    if companion.runtime_replica_count_key:
        patch[companion.runtime_replica_count_key] = str(
            companion.runtime_enabled_replicas if enabled else 0
        )
    return patch


def image_lock_names() -> tuple[str, ...]:
    """Return service names controlled by the generated image lock."""

    return tuple(name for name, entry in IMAGE_REGISTRY.items() if entry.image_lock)


def image_lock_key(service_name: str) -> str:
    """Return the ConfigMap key prefix for one service."""

    return service_name.upper().replace("-", "_")


def acceptance_digest_key(service_name: str) -> str:
    """Return the lock key recording the acceptance image paired with a fork canonical."""

    return f"{image_lock_key(service_name)}_ACCEPTANCE_DIGEST"


def do_not_disrupt_key(service_name: str) -> str:
    """Return the lock key the service's HelmRelease reads for its Karpenter annotation."""

    return f"{image_lock_key(service_name)}_DO_NOT_DISRUPT"


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
            # HTTPError is a URLError subclass; only network failures and 5xx
            # are transient.
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
    sources: Mapping[str, str] | None = None,
) -> dict[str, ResolvedImage]:
    """Resolve all requested images atomically.

    ``sources`` maps a service to the fork repository it follows; every other
    service resolves from the community registry on ``branch``. Raises
    ImageResolutionError if any requested image cannot be resolved.
    """

    requested = list(names or IMAGE_REGISTRY.keys())
    forks = dict(sources or {})
    resolved: dict[str, ResolvedImage] = {}
    errors: list[str] = []

    for primary in dict.fromkeys(companion_primary(name) for name in requested):
        companion = companion_image(primary)
        companion_requested = bool(companion and companion.name in requested)
        fork = forks.get(primary)
        try:
            if fork:
                primary_image, commit = resolve_fork_image(primary, fork)
            else:
                primary_image = resolve_image(primary, IMAGE_REGISTRY[primary], branch)
                commit = ""
        except Exception as exc:
            if primary in requested:
                errors.append(str(exc))
            if companion_requested:
                message = f"{companion.name}: unable to resolve matching {primary} tag"
                errors.append(message if primary in requested else f"{message}: {exc}")
            continue

        if primary in requested:
            resolved[primary] = primary_image
        if not companion_requested or companion is None:
            continue
        try:
            resolved[companion.name] = _resolve_companion(
                primary, companion, branch, primary_image, fork, commit
            )
        except Exception as exc:
            errors.append(str(exc))

    if errors:
        raise ImageResolutionError("; ".join(errors))
    return {name: resolved[name] for name in requested}


def _resolve_companion(
    primary_name: str,
    companion: CompanionImage,
    branch: str,
    primary_image: ResolvedImage,
    fork: str | None,
    commit: str,
) -> ResolvedImage:
    """Resolve the companion built from the primary image's commit and source."""

    if not fork:
        try:
            return resolve_image_tag(
                companion.name,
                IMAGE_REGISTRY[companion.name],
                branch,
                primary_image.tag,
            )
        except ImageResolutionError:
            if companion.canonical_required:
                raise
            return ResolvedImage(
                companion.name,
                primary_image.repository,
                primary_image.tag,
                primary_image.created_at,
                primary_image.digest,
            )
    matched = resolve_fork_companion(primary_image.repository, commit, companion)
    if matched is None:
        raise ImageResolutionError(
            f"{companion.name}: {fork} published no companion "
            f"{primary_image.repository}{companion.package_suffix}:{primary_image.tag}; "
            f"{primary_name} cannot follow the fork until it builds one beside the service image"
        )
    repository, digest = matched
    return ResolvedImage(
        companion.name, repository, primary_image.tag, primary_image.created_at, digest
    )


def resolve_image_lock(
    branch: str = DEFAULT_IMAGE_BRANCH, sources: Mapping[str, str] | None = None
) -> dict[str, ResolvedImage]:
    """Resolve the images controlled by the live Flux image lock."""

    return resolve_images(branch=branch, names=image_lock_names(), sources=sources)


def parse_image_digest_ref(ref: str) -> tuple[str, str]:
    """Split an image reference into (repository, digest), digest required.

    Tags are rejected outright: a fork deploy's identity is the manifest
    digest, and GHCR's ``sha-*`` tags are pruned after 30 days, so
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
    # A mixed-case repository resolves at the registry but fails the
    # containerd pull once written to a Deployment.
    return repository.lower(), digest


def _ghcr_pull_token(path: str) -> str:
    """Fetch GHCR's anonymous pull token for one repository path."""

    token_url = f"https://{GHCR_HOST}/token?service={GHCR_HOST}&scope=" + urllib.parse.quote(
        f"repository:{path}:pull", safe=":"
    )
    token_req = urllib.request.Request(token_url, headers={"User-Agent": "spi-stack-resolver"})
    with urllib.request.urlopen(token_req, timeout=15) as resp:  # nosec B310
        payload = json.loads(resp.read())
    # A non-object payload reads as no token and fails closed on the 401 that
    # follows.
    token = payload.get("token", "") if isinstance(payload, dict) else ""
    return token if isinstance(token, str) else ""


def ghcr_index_child_digests(repository: str, digest: str) -> tuple[str, ...]:
    """Return the platform manifest digests inside an OCI index, or () for a
    single manifest, a non-GHCR repository, or any fetch failure.

    A container runtime can record the platform manifest it selected from a
    pinned index, not the index digest the pod was pulled by, in a pod's
    imageID; verification accepts either. A failure here only narrows
    matching back to the index digest itself.
    """

    if not repository.startswith(f"{GHCR_HOST}/"):
        return ()
    path = repository[len(GHCR_HOST) + 1 :]
    manifest_url = f"https://{GHCR_HOST}/v2/{path}/manifests/{digest}"
    try:
        manifest_req = urllib.request.Request(
            manifest_url,
            headers={
                "User-Agent": "spi-stack-resolver",
                "Accept": _MANIFEST_ACCEPT,
                "Authorization": f"Bearer {_ghcr_pull_token(path)}",
            },
        )
        with urllib.request.urlopen(manifest_req, timeout=15) as resp:  # nosec B310
            payload = json.loads(resp.read())
    except (TimeoutError, urllib.error.URLError, ConnectionError, json.JSONDecodeError):
        return ()
    entries = payload.get("manifests") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return ()
    return tuple(
        entry["digest"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("digest"), str)
    )


# One path component of a distribution repository name (lowercase alphanumerics
# joined by single separators), which keeps URL-reserved characters out.
_REPOSITORY_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")


def require_ghcr_repository(repository: str) -> None:
    """Require a pin's repository to be a GHCR package, ``ghcr.io/<owner>/<name>``."""

    parts = repository.lower().split("/")
    if (
        len(parts) != 3
        or parts[0] != GHCR_HOST
        or not all(_REPOSITORY_COMPONENT_RE.match(part) for part in parts[1:])
    ):
        raise ImageResolutionError(
            f"repository {repository!r} is not a GHCR package; expected {GHCR_HOST}/<owner>/<name>"
        )


def fork_package_repositories(source_repo: str, service: str) -> tuple[str, ...]:
    """The GHCR packages a fork at ``source_repo`` may publish for ``service``.

    The template names the image after the repository, so ``Azure/osdu-spi-partition``
    publishes ``ghcr.io/azure/osdu-spi-partition``. A fork whose ``SERVICE_NAME``
    variable names the service publishes ``ghcr.io/<owner>/<service>`` instead.
    """

    primary = companion_primary(service)
    companion = companion_image(primary)
    suffix = companion.package_suffix if companion and companion.name == service else ""
    owner, _, name = source_repo.lower().partition("/")
    return tuple(
        dict.fromkeys(
            (
                f"{GHCR_HOST}/{owner}/{name}{suffix}",
                f"{GHCR_HOST}/{owner}/{primary}{suffix}",
            )
        )
    )


def runs_fork_package(service: str, repository: str, source_repo: str) -> bool:
    """Whether ``repository`` is a package ``source_repo`` publishes for ``service``."""

    return repository in fork_package_repositories(source_repo, service)


def resolve_ghcr_tag_digest(repository: str, tag: str, attempts: int = 3) -> str | None:
    """Return the manifest digest a GHCR tag points at, or None when it is not published.

    GHCR answers 403 on the pull token for a package that does not exist (or is
    private) and 404 for a missing tag; both read as "not published". Transient
    failures retry like ``resolve_ghcr_manifest`` and then raise
    ImageResolutionError, fail-closed.
    """

    path = repository[len(GHCR_HOST) + 1 :]
    manifest_url = f"https://{GHCR_HOST}/v2/{path}/manifests/{tag}"
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            token = _ghcr_pull_token(path)
            manifest_req = urllib.request.Request(
                manifest_url,
                method="HEAD",
                headers={
                    "User-Agent": "spi-stack-resolver",
                    "Accept": _MANIFEST_ACCEPT,
                    "Authorization": f"Bearer {token}",
                },
            )
            with urllib.request.urlopen(manifest_req, timeout=15) as resp:  # nosec B310
                digest = resp.headers.get("Docker-Content-Digest", "")
            if not _MANIFEST_DIGEST_RE.match(digest):
                raise ImageResolutionError(
                    f"GHCR returned no manifest digest for {repository}:{tag}"
                )
            return digest
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                return None
            if exc.code < 500 and exc.code != 429:
                raise ImageResolutionError(
                    f"GHCR refused the tag lookup for {repository}:{tag}: HTTP {exc.code}"
                ) from exc
            last_error = exc
        except (TimeoutError, urllib.error.URLError, ConnectionError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(5 * attempt)
    raise ImageResolutionError(
        f"GHCR unreachable after {attempts} attempts resolving {repository}:{tag}: {last_error}"
    )


def resolve_fork_companion(
    repository: str,
    source_sha: str,
    companion: CompanionImage,
) -> tuple[str, str] | None:
    """Find the companion the fork built beside its primary image at ``source_sha``.

    The template names a companion by appending its declared package suffix to
    the primary package and tags both ``sha-<12>`` from one commit. Returns
    ``(repository, digest)``, or None when the fork published no companion for
    the commit.
    """

    companion_repository = f"{repository}{companion.package_suffix}"
    digest = resolve_ghcr_tag_digest(companion_repository, f"sha-{source_sha[:12]}")
    return (companion_repository, digest) if digest else None


def _github_read(path: str, accept: str) -> bytes:
    from .update import resolve_github_token

    headers = {"User-Agent": "spi-stack-resolver", "Accept": accept}
    token = resolve_github_token(None)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{GITHUB_API_HOST}/{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise ImageResolutionError(f"GitHub API {path}: HTTP {exc.code}") from exc
    except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
        raise ImageResolutionError(f"GitHub API {path} unreachable: {exc}") from exc


def github_get(path: str):
    """GET a GitHub REST path and return parsed JSON, authenticated as the env or gh user."""

    body = _github_read(path, "application/vnd.github+json")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ImageResolutionError(f"GitHub API {path} unreachable: {exc}") from exc


def github_file(repository: str, path: str, ref: str) -> bytes:
    """One file's raw bytes from ``repository`` at ``ref``."""

    quoted = urllib.parse.quote(path)
    return _github_read(
        f"repos/{repository}/contents/{quoted}?ref={ref}", "application/vnd.github.raw"
    )


def resolve_fork_image(service: str, source_repo: str) -> tuple[ResolvedImage, str]:
    """Resolve the image a fork's ``main`` last published, and the commit that built it.

    ``main-snapshot`` names the digest; the ``sha-<12>`` tag with the same digest
    names the commit, which the lock records as the tag so the image can be paired
    with anything else built from that commit. The acceptance image at that tag is
    read in the same pass, since the tag can move later and the image carries no
    provenance. Returns ``(image, commit)``.
    """

    if not _REPO_PATH_RE.match(source_repo):
        raise ImageResolutionError(f"{service}: {source_repo!r} is not <owner>/<name>")
    candidates = fork_package_repositories(source_repo, service)
    # Both names can carry main-snapshot after a SERVICE_NAME change; the newest commit decides.
    snapshots = {
        repository: digest
        for repository in candidates
        if (digest := resolve_ghcr_tag_digest(repository, FORK_MAIN_TAG))
    }
    if not snapshots:
        raise ImageResolutionError(
            f"{service}: {source_repo} publishes no {FORK_MAIN_TAG} image at "
            f"{' or '.join(candidates)}; the package must be public and main must have built"
        )
    commits = github_get(
        f"repos/{source_repo}/commits?sha={FORK_MAIN_BRANCH}&per_page={FORK_COMMIT_SEARCH_DEPTH}"
    )
    for commit in commits if isinstance(commits, list) else []:
        sha = str((commit or {}).get("sha", ""))
        if not _SHA_TAG_RE.match(sha):
            continue
        tag = f"sha-{sha[:12]}"
        for repository, digest in snapshots.items():
            if resolve_ghcr_tag_digest(repository, tag) == digest:
                committer = (commit.get("commit") or {}).get("committer") or {}
                created_at = str(committer.get("date", ""))
                acceptance = resolve_ghcr_tag_digest(f"{repository}-acceptance", tag) or ""
                return (
                    ResolvedImage(service, repository, tag, created_at, digest, acceptance),
                    sha,
                )
    described = " or ".join(f"{r}:{FORK_MAIN_TAG} ({d[:19]})" for r, d in snapshots.items())
    raise ImageResolutionError(
        f"{service}: {described} matches no sha- tag among the "
        f"last {FORK_COMMIT_SEARCH_DEPTH} commits on {source_repo} {FORK_MAIN_BRANCH}"
    )


def resolve_ghcr_manifest(repository: str, digest: str, attempts: int = 3) -> None:
    """Assert the digest's manifest exists in GHCR before it is pinned.

    Uses the anonymous pull-token flow (fork packages are public). A missing
    manifest raises ImageNotFoundError; transient failures retry like
    ``gitlab_get`` and then raise ImageResolutionError, fail-closed.
    """

    path = repository[len(GHCR_HOST) + 1 :]
    manifest_url = f"https://{GHCR_HOST}/v2/{path}/manifests/{digest}"
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            token = _ghcr_pull_token(path)
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
            # 429 is anonymous-pull rate limiting, retried like a 5xx.
            if exc.code < 500 and exc.code != 429:
                raise ImageResolutionError(
                    f"GHCR refused the manifest check for {repository}@{digest}: HTTP {exc.code}"
                ) from exc
            last_error = exc
        except (TimeoutError, urllib.error.URLError, ConnectionError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(5 * attempt)
    raise ImageResolutionError(
        f"GHCR unreachable after {attempts} attempts checking {repository}@{digest}: {last_error}"
    )


def image_lock_missing_companion(
    lock_data: Mapping[str, str],
    service_name: str,
) -> bool:
    """Report whether an existing lock predates a companion's inclusion.

    A lock missing the composed ref counts as missing even when it already
    carries repository/tag because consumers may substitute ``_IMAGE_REF``.
    """

    key = image_lock_key(service_name)
    return not (
        lock_data.get(f"{key}_IMAGE_REPOSITORY")
        and (lock_data.get(f"{key}_IMAGE_TAG") or lock_data.get(f"{key}_IMAGE_DIGEST"))
        and lock_data.get(f"{key}_IMAGE_REF")
    )


def image_ref(repository: str, tag: str, digest: str) -> str:
    """Return the digest-first image reference, falling back to the tag."""

    return f"{repository}@{digest}" if digest else f"{repository}:{tag}"


def companion_lock_patch(
    lock_data: Mapping[str, str],
    primary_name: str,
    branch: str = DEFAULT_IMAGE_BRANCH,
    *,
    primary_repository: str = "",
    primary_tag: str = "",
    primary_created_at: str = "",
    source_repo: str = "",
) -> dict[str, str]:
    """Return companion entries missing from an existing image lock.

    The companion resolves from the recorded canonical primary, including the
    restore target hidden by an active pin. This keeps both images on one
    source commit instead of jumping the companion to the newest build.
    """

    companion = companion_image(primary_name)
    if companion is None:
        raise ImageResolutionError(f"{primary_name}: registry declares no companion image")
    key = image_lock_key(companion.name)
    existing_repository = lock_data.get(f"{key}_IMAGE_REPOSITORY", "")
    existing_tag = lock_data.get(f"{key}_IMAGE_TAG", "")
    existing_digest = lock_data.get(f"{key}_IMAGE_DIGEST", "")
    if existing_repository and (existing_tag or existing_digest):
        return {
            f"{key}_IMAGE_REF": image_ref(
                existing_repository,
                existing_tag,
                existing_digest,
            )
        }

    primary_key = image_lock_key(primary_name)
    primary_repository = primary_repository or lock_data.get(f"{primary_key}_IMAGE_REPOSITORY", "")
    primary_tag = primary_tag or lock_data.get(f"{primary_key}_IMAGE_TAG", "")
    primary_created_at = primary_created_at or lock_data.get(f"{primary_key}_IMAGE_CREATED_AT", "")
    if not primary_tag:
        raise ImageResolutionError(
            f"{companion.name}: image lock records no {primary_name} image tag to match"
        )

    if source_repo and runs_fork_package(primary_name, primary_repository, source_repo):
        repository = f"{primary_repository}{companion.package_suffix}"
        digest = resolve_ghcr_tag_digest(repository, primary_tag)
        if not digest:
            raise ImageResolutionError(
                f"{companion.name}: {source_repo} published no companion {repository}:{primary_tag}"
            )
        image = ResolvedImage(
            companion.name,
            repository,
            primary_tag,
            primary_created_at,
            digest,
        )
    else:
        image = resolve_image_tag(
            companion.name,
            IMAGE_REGISTRY[companion.name],
            lock_data.get("IMAGE_BRANCH") or branch,
            primary_tag,
        )

    patch = {
        f"{key}_IMAGE": image.image,
        f"{key}_IMAGE_REPOSITORY": image.repository,
        f"{key}_IMAGE_TAG": image.tag,
        f"{key}_IMAGE_CREATED_AT": image.created_at,
        f"{key}_IMAGE_DIGEST": image.digest,
        f"{key}_IMAGE_REF": image_ref(image.repository, image.tag, image.digest),
    }
    patch.update(companion_runtime_patch(companion.name, True))
    count = lock_data.get("IMAGE_COUNT", "")
    if count.isdigit() and not existing_repository and not existing_tag:
        patch["IMAGE_COUNT"] = str(int(count) + 1)
    return patch


def _yaml_string(value: str) -> str:
    return json.dumps(str(value))


IMAGE_BRANCH_ANNOTATION = "spi-stack.osdu.dev/image-branch"
IMAGE_RESOLVED_AT_ANNOTATION = "spi-stack.osdu.dev/resolved-at"


def build_lock_data(
    resolved: dict[str, ResolvedImage],
    branch: str,
    timestamp: str,
    do_not_disrupt: Iterable[str] = (),
) -> dict[str, str]:
    """Return the complete image-lock ConfigMap ``data`` for a resolved image set.

    Shared by the YAML renderer below and the live-cluster compare-and-retry
    mutation in ``pins.py``, so both build the same keys from one place.
    ``do_not_disrupt`` names the services whose pods Karpenter must not
    voluntarily disrupt; every other service's flag is written as false.
    """

    protected = frozenset(do_not_disrupt)
    data: dict[str, str] = {
        "IMAGE_BRANCH": branch,
        "IMAGE_RESOLVED_AT": timestamp,
        "IMAGE_COUNT": str(len(resolved)),
    }
    for name in image_lock_names():
        image = resolved[name]
        key = image_lock_key(name)
        # A digest pin has no tag; the digest ref avoids a dangling "repository:".
        data[f"{key}_IMAGE"] = (
            image.image if image.tag else image_ref(image.repository, image.tag, image.digest)
        )
        data[f"{key}_IMAGE_REPOSITORY"] = image.repository
        data[f"{key}_IMAGE_TAG"] = image.tag
        data[f"{key}_IMAGE_CREATED_AT"] = image.created_at
        data[f"{key}_IMAGE_DIGEST"] = image.digest
        data[f"{key}_IMAGE_REF"] = image_ref(image.repository, image.tag, image.digest)
        data[do_not_disrupt_key(name)] = str(name in protected).lower()
        primary_name = companion_primary(name)
        primary_image = resolved.get(primary_name)
        runtime_available = primary_image is None or (
            image.repository,
            image.tag,
            image.digest,
        ) != (
            primary_image.repository,
            primary_image.tag,
            primary_image.digest,
        )
        data.update(companion_runtime_patch(name, runtime_available))
        if image.acceptance_digest:
            data[acceptance_digest_key(name)] = image.acceptance_digest
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
    do_not_disrupt: Iterable[str] = (),
) -> str:
    """Render the Flux substitution ConfigMap for service image pins."""

    timestamp = (resolved_at or datetime.now(timezone.utc)).isoformat()
    data = build_lock_data(resolved, branch, timestamp, do_not_disrupt)
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
