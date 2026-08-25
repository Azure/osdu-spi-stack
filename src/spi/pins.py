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
    resolve_image,
    resolve_image_tag,
)
from .shell import kubectl_json, run_command

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

    mr = gitlab_get(f"{GITLAB_HOST}/api/v4/projects/{project_id}/merge_requests/{mr_iid}")
    if not isinstance(mr, dict) or "source_branch" not in mr:
        raise PinError(f"MR {mr_iid}: unexpected GitLab API response")
    return mr


def resolve_mr_image(service: str, mr_iid: str) -> tuple[ResolvedImage, dict]:
    """Resolve the image an MR's pipeline built for one service.

    OSDU containerizes protected refs only, so an MR's image usually comes
    from its ``trusted-<branch>`` copy (the ref maintainers create to run the
    privileged pipeline). Resolution prefers the exact MR head commit on the
    source branch, then on the trusted copy, then the newest immutable tag on
    the trusted copy for when that ref was pushed ahead of the MR view.
    """

    entry = IMAGE_REGISTRY[service]
    mr = fetch_merge_request(entry.project_id, mr_iid)
    slug = ref_slug(mr["source_branch"])
    sha = mr.get("sha", "")
    if not slug or not sha:
        raise PinError(f"MR {mr_iid}: missing source branch or head commit in API response")

    errors: list[str] = []
    for branch in (slug, f"trusted-{slug}"):
        try:
            return resolve_image_tag(service, entry, branch, sha), mr
        except ImageResolutionError as exc:
            errors.append(str(exc))
    try:
        return resolve_image(service, entry, f"trusted-{slug}"), mr
    except ImageResolutionError as exc:
        errors.append(str(exc))

    raise PinError(
        f"MR !{mr_iid}: no pipeline image for head commit {sha[:12]} "
        f"({'; '.join(errors)}). The branch or its trusted-{slug} copy must run "
        "the containerize pipeline before it can be pinned."
    )


def read_lock() -> dict:
    """Return the live osdu-image-lock ConfigMap object."""

    lock = kubectl_json(["get", "configmap", IMAGE_LOCK_CONFIGMAP, "-n", IMAGE_LOCK_NAMESPACE])
    if not lock:
        raise PinError(
            f"ConfigMap {IMAGE_LOCK_CONFIGMAP} not found in {IMAGE_LOCK_NAMESPACE}; "
            "is this a core-profile cluster?"
        )
    return lock


def decode_pins(lock: dict) -> dict[str, ServicePin]:
    """Return the active pins recorded on a lock object."""

    raw = (lock.get("metadata", {}).get("annotations") or {}).get(PINS_ANNOTATION, "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    pins: dict[str, ServicePin] = {}
    for name, fields in parsed.items():
        try:
            pins[name] = ServicePin(**fields)
        except TypeError:
            continue
    return pins


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

    lock = read_lock()
    lock_data = lock.get("data", {}) or {}
    pins = decode_pins(lock)
    applied_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    resolved: list[tuple[str, ResolvedImage, dict]] = []
    for name in targets:
        try:
            image, mr = resolve_mr_image(name, mr_iid)
        except PinError:
            if name == SCHEMA_LOAD_SERVICE_NAME:
                # The MR may not rebuild the loader image; the service pin
                # alone is still a valid experiment.
                continue
            raise
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

    patch_lock(data, pins, f"Pin {', '.join(n for n, _ in results)} to MR !{mr_iid} image")
    reconcile_consumers([name for name, _ in results])
    return results


def reset_service(service: str) -> list[str]:
    """Restore a pinned service (and schema's paired loader) to its canonical image."""

    if service not in IMAGE_REGISTRY:
        known = ", ".join(sorted(IMAGE_REGISTRY))
        raise PinError(f"Unknown service {service!r}. Known services: {known}")

    lock = read_lock()
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
    """Return active pins from the live cluster, or {} when unreachable."""

    try:
        return decode_pins(read_lock())
    except Exception:
        return {}


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
