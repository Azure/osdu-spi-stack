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

"""Per-service MR image pins on the live osdu-image-lock ConfigMap.

A pin points one service at the container image built by an OSDU GitLab
merge-request pipeline, resolved from the MR's source branch at its head
commit. Pins live in the lock itself: the service's data keys are
overwritten and provenance (MR iid, branch, canonical image, timestamps)
is recorded in one JSON annotation, so `spi reconcile --refresh-images`
and `spi up` can re-render the lock without silently reverting a pin.
"""

from __future__ import annotations

import json
import re
import urllib.error
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .images import (
    GITLAB_HOST,
    IMAGE_LOCK_CONFIGMAP,
    IMAGE_LOCK_NAMESPACE,
    IMAGE_REGISTRY,
    SCHEMA_LOAD_SERVICE_NAME,
    SCHEMA_SERVICE_NAME,
    ImageResolutionError,
    ResolvedImage,
    gitlab_get,
    image_lock_key,
    resolve_image_commit,
)
from .shell import run_command, run_process

PINS_ANNOTATION = "spi-stack.osdu.dev/pins"

# entry.file prefix -> the Flux Kustomization that substitutes those keys.
_FILE_KUSTOMIZATIONS = {
    "services/": "spi-osdu-services",
    "services-reference/": "spi-osdu-reference",
    "schema-load/": "spi-osdu-schema-load",
}


class PinError(RuntimeError):
    """Raised when a pin cannot be resolved, applied, or reset."""


@dataclass(frozen=True)
class ServicePin:
    """Provenance for one pinned service image."""

    mr: str
    branch: str
    repository: str
    tag: str
    canonical_repository: str
    canonical_tag: str
    canonical_created_at: str
    canonical_digest: str
    applied_at: str


def ref_slug(branch: str) -> str:
    """Return the branch's CI_COMMIT_REF_SLUG as GitLab CI computes it."""

    slug = re.sub(r"[^a-z0-9]", "-", branch.lower())
    return slug.strip("-")[:63].rstrip("-")


def fetch_merge_request(project_id: int, mr_iid: str) -> dict:
    """Return the MR metadata needed to resolve its pipeline image."""

    try:
        mr = gitlab_get(f"{GITLAB_HOST}/api/v4/projects/{project_id}/merge_requests/{mr_iid}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise PinError(f"MR !{mr_iid} not found in GitLab project {project_id}.") from exc
        raise PinError(f"MR !{mr_iid}: GitLab API returned HTTP {exc.code}.") from exc
    except ImageResolutionError as exc:
        raise PinError(str(exc)) from exc
    if not isinstance(mr, dict) or "source_branch" not in mr:
        raise PinError(f"MR {mr_iid}: unexpected GitLab API response")
    return mr


def resolve_mr_image(service: str, mr_iid: str) -> tuple[ResolvedImage, dict]:
    """Resolve the image an MR's pipeline built for one service.

    OSDU containerizes protected refs only, so an MR's image usually comes
    from its ``trusted-<branch>`` copy (the ref maintainers create to run the
    privileged pipeline). Only an image tagged with the MR's head commit is
    accepted, so a stale trusted copy cannot silently substitute other code.
    """

    entry = IMAGE_REGISTRY[service]
    mr = fetch_merge_request(entry.project_id, mr_iid)
    source_branch = mr.get("source_branch", "")
    sha = mr.get("sha", "")
    if not ref_slug(source_branch) or not sha:
        raise PinError(f"MR {mr_iid}: missing source branch or head commit in API response")

    errors: list[str] = []
    # GitLab slugs the full ref name, so the trusted copy's slug truncates
    # after the prefix rather than prefixing an already truncated slug.
    for branch in (ref_slug(source_branch), ref_slug(f"trusted-{source_branch}")):
        try:
            return resolve_image_commit(service, entry, branch, sha), mr
        except ImageResolutionError as exc:
            errors.append(str(exc))

    raise PinError(
        f"MR !{mr_iid}: no pipeline image for head commit {sha[:12]} "
        f"({'; '.join(errors)}). The branch or its trusted- copy must run the "
        "containerize pipeline at this commit; ask a maintainer to refresh a "
        "stale trusted- copy before pinning."
    )


def read_lock(required: bool = True) -> dict | None:
    """Return the live osdu-image-lock ConfigMap, or None when absent.

    A missing ConfigMap is only tolerated with ``required=False`` (a cluster
    not yet deployed). Any other read failure raises, so callers can never
    mistake an unreachable cluster for an unpinned one.
    """

    result = run_process(
        [
            "kubectl",
            "get",
            "configmap",
            IMAGE_LOCK_CONFIGMAP,
            "-n",
            IMAGE_LOCK_NAMESPACE,
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
        detail = (result.stderr or "").strip() or "kubectl failed"
        raise PinError(f"Could not read ConfigMap {IMAGE_LOCK_CONFIGMAP}: {detail}")
    if not result.stdout.strip():
        if required:
            raise PinError(
                f"ConfigMap {IMAGE_LOCK_CONFIGMAP} not found in {IMAGE_LOCK_NAMESPACE}; "
                "is this a core-profile cluster?"
            )
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PinError(f"Could not parse ConfigMap {IMAGE_LOCK_CONFIGMAP}: {exc}") from exc


def decode_pins(lock: dict) -> dict[str, ServicePin]:
    """Return the active pins recorded on a lock object.

    A corrupt annotation raises rather than reading as "no pins": treating
    it as empty would let the next refresh silently revert active pins.
    """

    raw = (lock.get("metadata", {}).get("annotations") or {}).get(PINS_ANNOTATION, "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return {name: ServicePin(**fields) for name, fields in parsed.items()}
    except (json.JSONDecodeError, TypeError, AttributeError) as exc:
        raise PinError(
            f"Corrupt {PINS_ANNOTATION} annotation on {IMAGE_LOCK_CONFIGMAP}: {exc}. "
            "Repair or remove the annotation before changing images."
        ) from exc


def encode_pins(pins: dict[str, ServicePin]) -> str:
    return json.dumps({name: asdict(pin) for name, pin in sorted(pins.items())})


def _lock_entry_patch(service: str, repository: str, tag: str, created_at: str, digest: str):
    key = image_lock_key(service)
    return {
        f"{key}_IMAGE": f"{repository}:{tag}",
        f"{key}_IMAGE_REPOSITORY": repository,
        f"{key}_IMAGE_TAG": tag,
        f"{key}_IMAGE_CREATED_AT": created_at,
        f"{key}_IMAGE_DIGEST": digest,
    }


def patch_lock(data: dict[str, str], pins: dict[str, ServicePin], description: str) -> None:
    """Merge-patch the live lock's data keys and pins annotation together."""

    patch = {
        "metadata": {"annotations": {PINS_ANNOTATION: encode_pins(pins) if pins else None}},
        "data": data,
    }
    run_command(
        [
            "kubectl",
            "patch",
            "configmap",
            IMAGE_LOCK_CONFIGMAP,
            "-n",
            IMAGE_LOCK_NAMESPACE,
            "--type=merge",
            "-p",
            json.dumps(patch),
        ],
        description=description,
    )


def reconcile_consumers(services: list[str]) -> None:
    """Ask Flux to re-substitute the Kustomizations consuming changed pins."""

    names = {
        kustomization
        for service in services
        for prefix, kustomization in _FILE_KUSTOMIZATIONS.items()
        if IMAGE_REGISTRY[service].file.startswith(prefix)
    }
    ts = datetime.now(timezone.utc).isoformat()
    for name in sorted(names):
        run_command(
            [
                "kubectl",
                "annotate",
                "--overwrite",
                f"kustomization/{name}",
                "-n",
                IMAGE_LOCK_NAMESPACE,
                f"reconcile.fluxcd.io/requestedAt={ts}",
            ],
            description=f"Trigger {name} reconciliation",
            check=False,
        )


def pin_service(service: str, mr_iid: str) -> list[tuple[str, ServicePin]]:
    """Pin a service (and schema's paired loader) to an MR pipeline image.

    Returns the applied (service, pin) pairs.
    """

    if service not in IMAGE_REGISTRY:
        known = ", ".join(sorted(IMAGE_REGISTRY))
        raise PinError(f"Unknown service {service!r}. Known services: {known}")
    if service == SCHEMA_LOAD_SERVICE_NAME:
        raise PinError("Pin 'schema' instead; the loader follows the schema pin.")

    targets = [service]
    if service == SCHEMA_SERVICE_NAME:
        targets.append(SCHEMA_LOAD_SERVICE_NAME)

    lock = read_lock() or {}
    lock_data = lock.get("data", {}) or {}
    pins = decode_pins(lock)
    applied_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    resolved: list[tuple[str, ResolvedImage, dict]] = []
    released: dict[str, ServicePin] = {}
    for name in targets:
        try:
            image, mr = resolve_mr_image(name, mr_iid)
        except PinError as exc:
            if name != SCHEMA_LOAD_SERVICE_NAME:
                raise
            # The MR may not rebuild the loader image; the service pin alone
            # is still a valid experiment. A loader still pinned by an earlier
            # MR must not survive as a mismatched pair, so release it here.
            stale = pins.pop(SCHEMA_LOAD_SERVICE_NAME, None)
            if stale:
                if not stale.canonical_repository or not stale.canonical_tag:
                    raise PinError(
                        f"{SCHEMA_LOAD_SERVICE_NAME} is pinned to MR !{stale.mr} with no "
                        "canonical image recorded; run 'spi service reset schema' "
                        "before re-pinning."
                    ) from exc
                released[name] = stale
            continue
        resolved.append((name, image, mr))

    data: dict[str, str] = {}
    results: list[tuple[str, ServicePin]] = []
    for name, image, mr in resolved:
        key = image_lock_key(name)
        existing = pins.get(name)
        pin = ServicePin(
            mr=str(mr_iid),
            branch=mr.get("source_branch", ""),
            repository=image.repository,
            tag=image.tag,
            # First pin captures the canonical image; re-pinning keeps it.
            canonical_repository=(
                existing.canonical_repository
                if existing
                else lock_data.get(f"{key}_IMAGE_REPOSITORY", "")
            ),
            canonical_tag=(
                existing.canonical_tag if existing else lock_data.get(f"{key}_IMAGE_TAG", "")
            ),
            canonical_created_at=(
                existing.canonical_created_at
                if existing
                else lock_data.get(f"{key}_IMAGE_CREATED_AT", "")
            ),
            canonical_digest=(
                existing.canonical_digest if existing else lock_data.get(f"{key}_IMAGE_DIGEST", "")
            ),
            applied_at=applied_at,
        )
        pins[name] = pin
        data.update(
            _lock_entry_patch(name, image.repository, image.tag, image.created_at, image.digest)
        )
        results.append((name, pin))

    for name, stale in released.items():
        data.update(
            _lock_entry_patch(
                name,
                stale.canonical_repository,
                stale.canonical_tag,
                stale.canonical_created_at,
                stale.canonical_digest,
            )
        )

    description = f"Pin {', '.join(n for n, _ in results)} to MR !{mr_iid} image"
    if released:
        description += f"; release stale {', '.join(sorted(released))}"
    patch_lock(data, pins, description)
    reconcile_consumers([name for name, _ in results] + sorted(released))
    return results


def reset_service(service: str) -> list[str]:
    """Restore a pinned service (and schema's paired loader) to its canonical image."""

    if service not in IMAGE_REGISTRY:
        known = ", ".join(sorted(IMAGE_REGISTRY))
        raise PinError(f"Unknown service {service!r}. Known services: {known}")

    lock = read_lock() or {}
    pins = decode_pins(lock)
    targets = [name for name in (service, SCHEMA_LOAD_SERVICE_NAME) if name in pins]
    if service != SCHEMA_SERVICE_NAME:
        targets = [name for name in targets if name == service]
    if not targets:
        raise PinError(f"{service} is not pinned.")

    data: dict[str, str] = {}
    for name in targets:
        pin = pins.pop(name)
        if not pin.canonical_repository or not pin.canonical_tag:
            raise PinError(
                f"{name}: pin records no canonical image to restore; "
                "run 'spi reconcile --refresh-images' after reset to re-resolve."
            )
        data.update(
            _lock_entry_patch(
                name,
                pin.canonical_repository,
                pin.canonical_tag,
                pin.canonical_created_at,
                pin.canonical_digest,
            )
        )

    patch_lock(data, pins, f"Reset {', '.join(targets)} to canonical image")
    reconcile_consumers(targets)
    return targets


def live_pins() -> dict[str, ServicePin]:
    """Return active pins from the live cluster, or {} when no lock exists yet.

    Read and decode failures raise PinError: the refresh paths must not
    mistake an unreadable pin state for "no pins" and revert an experiment.
    """

    lock = read_lock(required=False)
    if lock is None:
        return {}
    return decode_pins(lock)


def reapply_pins(pins: dict[str, ServicePin]) -> None:
    """Re-assert active pins after a full lock re-render.

    The refresh paths replace every data key with freshly resolved images;
    re-patching from the recorded pins afterwards keeps a refresh from
    silently reverting an experiment.
    """

    if not pins:
        return
    data: dict[str, str] = {}
    for name, pin in pins.items():
        data.update(_lock_entry_patch(name, pin.repository, pin.tag, "", ""))
    patch_lock(data, pins, f"Preserve pins: {', '.join(sorted(pins))}")
