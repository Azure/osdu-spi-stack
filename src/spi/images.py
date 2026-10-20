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

"""OSDU service image resolution from GHCR and image-lock rendering."""

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

GHCR_HOST = "ghcr.io"
GITHUB_API_HOST = "https://api.github.com"
# The template moves this tag to each push build of main.
FORK_MAIN_TAG = "main-snapshot"
IMAGE_LOCK_CONFIGMAP = "osdu-image-lock"
IMAGE_LOCK_NAMESPACE = "osdu-flux"
SCHEMA_SERVICE_NAME = "schema"
SCHEMA_LOAD_SERVICE_NAME = "schema-load"
AZURE_SOURCE_OWNER = "Azure"

_FORK_SHA_TAG_RE = re.compile(r"^sha-([0-9a-f]{12})$")
_NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')
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
class ImageRegistryEntry:
    """The stack file carrying one OSDU image's lock placeholders."""

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
    # The suite image built from the same fork commit; empty when none was published.
    acceptance_digest: str = ""

    @property
    def image(self) -> str:
        return f"{self.repository}:{self.tag}"


# Service name to the stack file carrying its image lock placeholders.
IMAGE_REGISTRY: dict[str, ImageRegistryEntry] = {
    "partition": ImageRegistryEntry("services/partition.yaml"),
    "entitlements": ImageRegistryEntry("services/entitlements.yaml"),
    "legal": ImageRegistryEntry("services/legal.yaml"),
    "schema": ImageRegistryEntry("services/schema.yaml"),
    "schema-load": ImageRegistryEntry("schema-load/job.yaml"),
    "storage": ImageRegistryEntry("services/storage.yaml"),
    "search": ImageRegistryEntry("services/search.yaml"),
    "indexer": ImageRegistryEntry("services/indexer.yaml"),
    "indexer-queue": ImageRegistryEntry("services/indexer-queue.yaml"),
    "file": ImageRegistryEntry("services/file.yaml"),
    "workflow": ImageRegistryEntry("services/workflow.yaml"),
    "crs-conversion": ImageRegistryEntry("services-reference/crs-conversion.yaml"),
    "crs-catalog": ImageRegistryEntry("services-reference/crs-catalog.yaml"),
    "unit": ImageRegistryEntry("services-reference/unit.yaml"),
}


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


def _parse_datetime(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def azure_source(service: str) -> str:
    """The Azure repository whose ``main`` image a service follows when no mirror is recorded."""

    base = SCHEMA_SERVICE_NAME if service == SCHEMA_LOAD_SERVICE_NAME else service
    return f"{AZURE_SOURCE_OWNER}/osdu-spi-{base}"


def canonical_repo(service: str, sources: Mapping[str, str] | None = None) -> str:
    """The repository ``service`` follows: its recorded mirror, else its Azure repository."""

    base = SCHEMA_SERVICE_NAME if service == SCHEMA_LOAD_SERVICE_NAME else service
    return (sources or {}).get(base) or azure_source(base)


def resolve_images(
    names: Iterable[str] | None = None,
    sources: Mapping[str, str] | None = None,
) -> dict[str, ResolvedImage]:
    """Resolve all requested images atomically.

    ``sources`` maps a service to the mirror repository it follows; every other
    service follows its Azure repository. Raises ImageResolutionError if any
    requested image cannot be resolved.
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
        try:
            resolved[name] = resolve_fork_image(name, canonical_repo(name, sources))[0]
        except Exception as exc:
            errors.append(str(exc))

    if schema_load_requested:
        repo = canonical_repo(SCHEMA_SERVICE_NAME, sources)
        try:
            schema_image, commit = resolve_fork_image(SCHEMA_SERVICE_NAME, repo)
        except Exception as exc:
            if SCHEMA_SERVICE_NAME in requested:
                errors.append(str(exc))
                errors.append(f"{SCHEMA_LOAD_SERVICE_NAME}: unable to resolve matching schema tag")
            else:
                errors.append(
                    f"{SCHEMA_LOAD_SERVICE_NAME}: unable to resolve matching schema tag: {exc}"
                )
        else:
            if SCHEMA_SERVICE_NAME in requested:
                resolved[SCHEMA_SERVICE_NAME] = schema_image
            try:
                resolved[SCHEMA_LOAD_SERVICE_NAME] = _resolve_loader(schema_image, repo, commit)
            except Exception as exc:
                errors.append(str(exc))

    if errors:
        raise ImageResolutionError("; ".join(errors))
    return {name: resolved[name] for name in requested}


def _resolve_loader(schema_image: ResolvedImage, repo: str, commit: str) -> ResolvedImage:
    """The loader built from the same commit as ``schema_image``, from the same repository."""

    loader = resolve_fork_loader(schema_image.repository, commit)
    if loader is None:
        raise ImageResolutionError(
            f"{SCHEMA_LOAD_SERVICE_NAME}: {repo} published no loader "
            f"{schema_image.repository}-load:{schema_image.tag}; schema cannot follow "
            f"{repo} until it builds one beside the service image"
        )
    repository, digest = loader
    return ResolvedImage(
        SCHEMA_LOAD_SERVICE_NAME, repository, schema_image.tag, schema_image.created_at, digest
    )


def resolve_image_lock(sources: Mapping[str, str] | None = None) -> dict[str, ResolvedImage]:
    """Resolve the images controlled by the live Flux image lock."""

    return resolve_images(names=image_lock_names(), sources=sources)


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

    owner, _, name = source_repo.lower().partition("/")
    return tuple(dict.fromkeys((f"{GHCR_HOST}/{owner}/{name}", f"{GHCR_HOST}/{owner}/{service}")))


def runs_fork_package(service: str, repository: str, source_repo: str) -> bool:
    """Whether ``repository`` is a package ``source_repo`` publishes for ``service``."""

    loader = service == SCHEMA_LOAD_SERVICE_NAME
    packages = fork_package_repositories(source_repo, SCHEMA_SERVICE_NAME if loader else service)
    return repository in {f"{package}-load" if loader else package for package in packages}


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


def resolve_fork_loader(repository: str, source_sha: str) -> tuple[str, str] | None:
    """Find the loader the fork built beside the schema image at ``source_sha``.

    The template names the loader ``<image>-load`` after the same image name as
    the service and tags both ``sha-<12>`` from one commit (template ADR-042), so
    the loader for ``ghcr.io/<owner>/<image>`` is ``ghcr.io/<owner>/<image>-load``
    at that tag and nothing else. Returns ``(repository, digest)``, or None when
    the fork published no loader for the commit.
    """

    loader_repository = f"{repository}-load"
    digest = resolve_ghcr_tag_digest(loader_repository, f"sha-{source_sha[:12]}")
    return (loader_repository, digest) if digest else None


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


def _ghcr_get(path: str, url_path: str, accept: str, attempts: int = 3) -> tuple[bytes, str]:
    """GET one anonymous GHCR registry path; returns the body and its ``Link`` header."""

    url = f"https://{GHCR_HOST}{url_path}"
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "spi-stack-resolver",
                    "Accept": accept,
                    "Authorization": f"Bearer {_ghcr_pull_token(path)}",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
                return resp.read(), resp.headers.get("Link", "")
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise ImageResolutionError(f"GHCR refused {url}: HTTP {exc.code}") from exc
            last_error = exc
        except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(5 * attempt)
    raise ImageResolutionError(
        f"GHCR unreachable after {attempts} attempts reading {url}: {last_error}"
    )


def ghcr_tags(repository: str) -> tuple[str, ...]:
    """Every tag a public GHCR package lists."""

    path = repository[len(GHCR_HOST) + 1 :]
    tags: list[str] = []
    url_path = f"/v2/{path}/tags/list?n=1000"
    while url_path:
        body, link = _ghcr_get(path, url_path, "application/json")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ImageResolutionError(f"GHCR tag list for {repository} is not JSON") from exc
        listed = payload.get("tags") if isinstance(payload, dict) else None
        tags.extend(tag for tag in listed or () if isinstance(tag, str))
        match = _NEXT_LINK_RE.search(link)
        url_path = match.group(1) if match else ""
    return tuple(tags)


def ghcr_image_created(repository: str, digest: str) -> str:
    """The build time an image's config records, or ``""`` when it cannot be read.

    An index is read through its first platform manifest; every platform in it
    comes from one build.
    """

    path = repository[len(GHCR_HOST) + 1 :]
    try:
        body, _ = _ghcr_get(path, f"/v2/{path}/manifests/{digest}", _MANIFEST_ACCEPT)
        manifest = json.loads(body)
        entries = manifest.get("manifests")
        if isinstance(entries, list) and entries:
            body, _ = _ghcr_get(
                path, f"/v2/{path}/manifests/{entries[0]['digest']}", _MANIFEST_ACCEPT
            )
            manifest = json.loads(body)
        body, _ = _ghcr_get(path, f"/v2/{path}/blobs/{manifest['config']['digest']}", "*/*")
        created = json.loads(body).get("created", "")
    except (ImageResolutionError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return ""
    return created if isinstance(created, str) else ""


def resolve_fork_image(service: str, source_repo: str) -> tuple[ResolvedImage, str]:
    """Resolve the image a fork's ``main`` last published, and the commit that built it.

    ``main-snapshot`` names the digest; the ``sha-<12>`` tag with the same digest
    names the commit, which the lock records as the tag so the image can be paired
    with anything else built from that commit. Everything is read from the public
    packages, so a private fork resolves like a public one. The acceptance image at
    that tag is read in the same pass, since the tag can move later and the image
    carries no provenance. Returns ``(image, commit)`` with the 12-character commit.
    """

    if not _REPO_PATH_RE.match(source_repo):
        raise ImageResolutionError(f"{service}: {source_repo!r} is not <owner>/<name>")
    candidates = fork_package_repositories(source_repo, service)
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
    found: list[tuple[ResolvedImage, str]] = []
    for repository, digest in snapshots.items():
        for tag in sorted(ghcr_tags(repository)):
            match = _FORK_SHA_TAG_RE.match(tag)
            if match and resolve_ghcr_tag_digest(repository, tag) == digest:
                created_at = ghcr_image_created(repository, digest)
                acceptance = resolve_ghcr_tag_digest(f"{repository}-acceptance", tag) or ""
                image = ResolvedImage(service, repository, tag, created_at, digest, acceptance)
                found.append((image, match.group(1)))
                break
    if not found:
        described = " or ".join(f"{r}:{FORK_MAIN_TAG} ({d[:19]})" for r, d in snapshots.items())
        raise ImageResolutionError(f"{service}: {described} matches no sha- tag in its package")
    # Both names can carry main-snapshot after a SERVICE_NAME change; the newest build decides.
    return max(found, key=lambda pair: _parse_datetime(pair[0].created_at))


def resolve_ghcr_manifest(repository: str, digest: str, attempts: int = 3) -> None:
    """Assert the digest's manifest exists in GHCR before it is pinned.

    Uses the anonymous pull-token flow (fork packages are public). A missing
    manifest raises ImageNotFoundError; transient failures retry with a
    short backoff and then raise ImageResolutionError, fail-closed.
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


def image_ref(repository: str, tag: str, digest: str) -> str:
    """Return the digest-first image reference, falling back to the tag."""

    return f"{repository}@{digest}" if digest else f"{repository}:{tag}"


def _yaml_string(value: str) -> str:
    return json.dumps(str(value))


IMAGE_RESOLVED_AT_ANNOTATION = "spi-stack.osdu.dev/resolved-at"


def build_lock_data(
    resolved: dict[str, ResolvedImage],
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
        if image.acceptance_digest:
            data[acceptance_digest_key(name)] = image.acceptance_digest
    return data


def build_lock_annotations(timestamp: str) -> dict[str, str]:
    """Return the base resolved-at annotation for the lock."""

    return {IMAGE_RESOLVED_AT_ANNOTATION: timestamp}


def render_image_lock_configmap(
    resolved: dict[str, ResolvedImage],
    resolved_at: datetime | None = None,
    extra_annotations: Mapping[str, str] | None = None,
    do_not_disrupt: Iterable[str] = (),
) -> str:
    """Render the Flux substitution ConfigMap for service image pins."""

    timestamp = (resolved_at or datetime.now(timezone.utc)).isoformat()
    data = build_lock_data(resolved, timestamp, do_not_disrupt)
    base_annotations = build_lock_annotations(timestamp)

    lines = [
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        f"  name: {IMAGE_LOCK_CONFIGMAP}",
        f"  namespace: {IMAGE_LOCK_NAMESPACE}",
        "  labels:",
        "    app.kubernetes.io/managed-by: osdu-spi-stack",
        "  annotations:",
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
