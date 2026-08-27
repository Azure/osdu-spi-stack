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
import time
import urllib.error
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable

from .console import console, display_yaml
from .deploy_record import DEPLOY_RECORD_CONFIGMAP, DeployRecordError, read_deploy_record
from .images import (
    DEFAULT_IMAGE_BRANCH,
    GITLAB_HOST,
    IMAGE_LOCK_CONFIGMAP,
    IMAGE_LOCK_NAMESPACE,
    IMAGE_REGISTRY,
    SCHEMA_LOAD_SERVICE_NAME,
    SCHEMA_SERVICE_NAME,
    ImageNotFoundError,
    ImageResolutionError,
    ResolvedImage,
    build_lock_annotations,
    build_lock_data,
    gitlab_get,
    image_lock_key,
    image_lock_missing_schema_load,
    image_ref,
    render_image_lock_configmap,
    resolve_image_commit,
    schema_load_lock_patch,
)
from .shell import run_command, run_process

PINS_ANNOTATION = "spi-stack.osdu.dev/pins"

# entry.file prefix -> the Flux Kustomization that substitutes those keys.
_FILE_KUSTOMIZATIONS = {
    "services/": "spi-osdu-services",
    "services-reference/": "spi-osdu-reference",
    "schema-load/": "spi-osdu-schema-load",
}

# Bounded resourceVersion compare-and-retry for the live image lock.
LOCK_MUTATION_MAX_ATTEMPTS = 5
_LOCK_MUTATION_BASE_DELAY_SECONDS = 1.0
_CONFLICT_MARKERS = (
    "the object has been modified",
    "test operation",
    "test failed",
    "unprocessable entity",
    "conflict",
    "already exists",
)


class PinError(RuntimeError):
    """Raised when a pin cannot be resolved, applied, or reset."""


class MissingPipelineImageError(PinError):
    """Raised when neither MR branch has an image for the MR head commit."""


class LockConflictError(PinError):
    """Raised when the image lock could not be safely mutated after
    repeated concurrent-write conflicts."""


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
    # Optional so a pin encoded before these fields existed keeps decoding.
    created_at: str = ""
    digest: str = ""


@dataclass(frozen=True)
class ResetResult:
    """Services restored immediately and those needing a canonical image refresh."""

    restored: tuple[str, ...]
    refresh_required: tuple[str, ...]


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


def resolve_mr_image(
    service: str, mr_iid: str, mr: dict | None = None
) -> tuple[ResolvedImage, dict]:
    """Resolve the image an MR's pipeline built for one service.

    OSDU containerizes protected refs only, so an MR's image usually comes
    from its ``trusted-<branch>`` copy (the ref maintainers create to run the
    privileged pipeline). Only an image tagged with the MR's head commit is
    accepted, so a stale trusted copy cannot silently substitute other code.
    Related services can share a previously fetched MR snapshot.
    """

    entry = IMAGE_REGISTRY[service]
    if mr is None:
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
        except ImageNotFoundError as exc:
            errors.append(str(exc))

    raise MissingPipelineImageError(
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
        f"{key}_IMAGE_REF": image_ref(repository, tag, digest),
    }


def _resource_version(lock: dict) -> str:
    return str((lock.get("metadata") or {}).get("resourceVersion", ""))


def _is_conflict(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _CONFLICT_MARKERS)


def mutate_lock(
    mutator: Callable[[dict | None], dict],
    description: str,
    max_attempts: int = LOCK_MUTATION_MAX_ATTEMPTS,
) -> dict:
    """Create or update the live image lock with bounded compare-and-retry.

    ``mutator`` is called with the freshly read ConfigMap, or ``None`` when
    it does not exist yet, and must return the complete desired object
    (only its ``data`` and ``metadata.annotations`` are used). It runs again
    on every retry so a concurrent writer's change is recomputed from a
    fresh read instead of clobbered by a stale patch.

    A missing lock is created; losing the create race to another writer
    rereads and falls into the update path. An existing lock is updated
    with a JSON Patch carrying a ``test`` precondition on
    ``metadata.resourceVersion`` ahead of the ``data``/``annotations``
    replacement, so the patch verb stays compatible with future patch-only
    RBAC. Only a resourceVersion test failure or a losing create race is
    retried, with bounded exponential backoff; any other failure raises a
    clear terminal error immediately.
    """

    delay = _LOCK_MUTATION_BASE_DELAY_SECONDS
    for attempt in range(1, max_attempts + 1):
        lock = read_lock(required=False)
        desired = mutator(lock)
        data = desired.get("data") or {}
        annotations = (desired.get("metadata") or {}).get("annotations") or {}

        if lock is None:
            document = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": IMAGE_LOCK_CONFIGMAP,
                    "namespace": IMAGE_LOCK_NAMESPACE,
                    "labels": {"app.kubernetes.io/managed-by": "osdu-spi-stack"},
                    "annotations": annotations,
                },
                "data": data,
            }
            display_yaml(json.dumps(document, indent=2), f"ConfigMap: {IMAGE_LOCK_CONFIGMAP}")
            result = run_process(
                ["kubectl", "create", "-f", "-", "-o", "json"],
                input=json.dumps(document),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0:
                return json.loads(result.stdout) if result.stdout.strip() else document
            stderr = (result.stderr or result.stdout or "").strip()
            if "already exists" in stderr.lower():
                if attempt < max_attempts:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise LockConflictError(
                    f"{description}: exceeded {max_attempts} attempts due to concurrent "
                    f"writers: {stderr}"
                )
            raise PinError(f"{description}: could not create {IMAGE_LOCK_CONFIGMAP}: {stderr}")

        patch = json.dumps(
            [
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": _resource_version(lock),
                },
                {"op": "add", "path": "/data", "value": data},
                {"op": "add", "path": "/metadata/annotations", "value": annotations},
            ]
        )
        result = run_command(
            [
                "kubectl",
                "patch",
                "configmap",
                IMAGE_LOCK_CONFIGMAP,
                "-n",
                IMAGE_LOCK_NAMESPACE,
                "--type=json",
                "-p",
                patch,
                "-o",
                "json",
            ],
            description=description,
            check=False,
        )
        if result.returncode == 0:
            return json.loads(result.stdout) if result.stdout.strip() else lock
        stderr = (result.stderr or result.stdout or "").strip()
        if _is_conflict(stderr):
            if attempt < max_attempts:
                console.print(
                    f"  [warning]{description}: concurrent write detected, retrying "
                    f"(attempt {attempt}/{max_attempts})[/warning]"
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise LockConflictError(
                f"{description}: exceeded {max_attempts} attempts due to concurrent "
                f"writers: {stderr}"
            )
        raise PinError(f"{description}: could not update {IMAGE_LOCK_CONFIGMAP}: {stderr}")

    # Every iteration above returns or raises; this satisfies static analysis
    # that the loop cannot fall through without one of the two.
    raise AssertionError("unreachable: mutate_lock attempt loop exited without result")


# Dependency order for pin consumers: a changed schema image has to reach the
# service before the loader Job is recreated against it, and the loader has to
# finish before reference re-seeds (same sequence as `spi reconcile`).
_CONSUMER_ORDER = ("spi-osdu-services", "spi-osdu-schema-load", "spi-osdu-reference")


def reconcile_consumers(services: list[str]) -> None:
    """Reconcile the Kustomizations consuming changed pins, in dependency order.

    Each stage blocks until Flux reports it ready, and a failed stage aborts
    the sequence, so a paired image change (schema + loader) cannot run the
    loader against the previous service.
    """

    names = {
        kustomization
        for service in services
        for prefix, kustomization in _FILE_KUSTOMIZATIONS.items()
        if IMAGE_REGISTRY[service].file.startswith(prefix)
    }
    for name in (candidate for candidate in _CONSUMER_ORDER if candidate in names):
        run_command(
            [
                "flux",
                "reconcile",
                "kustomization",
                name,
                "-n",
                IMAGE_LOCK_NAMESPACE,
                "--timeout",
                "40m",
            ],
            description=f"Wait for {name} reconciliation",
        )


def _refuse_unless_deployable() -> None:
    """Enforce the ADR-030 deployable rule on pin writes, fail-closed.

    Refuses unless every Kustomization is Ready, the deploy record is
    present, and ``maintenance`` is unset: the same `deployable` rule
    `spi status` reports, computed from the same
    ``collect_kustomization_readiness`` predicate so the CLI and the JSON
    contract cannot disagree on Flux convergence. Deferred import: status.py
    imports from this module, so a module-level import here would cycle.
    """

    from .status import StatusError, collect_kustomization_readiness

    try:
        readiness = collect_kustomization_readiness()
    except StatusError as exc:
        raise PinError(str(exc)) from exc
    if not readiness.ready:
        detail = (
            readiness.reason.message if readiness.reason else "not all Kustomizations are Ready"
        )
        raise PinError(
            f"Environment is not ready ({detail}); retry once 'spi status' reports deployable."
        )

    try:
        record = read_deploy_record(required=False)
    except DeployRecordError as exc:
        raise PinError(str(exc)) from exc
    if record is None:
        raise PinError(
            f"ConfigMap {DEPLOY_RECORD_CONFIGMAP} not found; the environment has no "
            "deploy record. Re-run 'spi up' to write one before pinning."
        )
    if record.maintenance:
        raise PinError(
            "Environment is in maintenance (a lifecycle run is in progress); "
            "retry once 'spi status' reports deployable."
        )


def _post_write_maintenance_check() -> str | None:
    """Return why a lifecycle run intervened during the write, or None.

    Re-reads the deploy record after the lock write closes the ADR-029
    window between `_refuse_unless_deployable`'s pre-check and the CAS write:
    a lifecycle run can set `maintenance` while `pin_service` is doing its
    GitLab round trips and lock mutation. An unreadable or now-absent record
    is treated the same as maintenance, since both mean the environment is
    no longer deployable (ADR-030).

    Deliberately does not recheck Kustomization readiness: `reconcile_consumers`
    runs immediately after this check succeeds and is expected to carry the
    affected Kustomization through a Ready -> not-Ready -> Ready cycle, so
    readiness right after the write is not a signal that anything went wrong.
    Only `maintenance`, which exclusively a concurrent lifecycle run sets,
    distinguishes that from an ordinary write.
    """

    try:
        record = read_deploy_record(required=False)
    except DeployRecordError as exc:
        return f"the post-write deployability recheck failed ({exc})"
    if record is None:
        return "the deploy record disappeared"
    if record.maintenance:
        return "a lifecycle run started maintenance"
    return None


def _revert_pin(written: dict[str, ServicePin], description: str) -> list[str]:
    """Undo exactly the pins ``written`` still holds live, restoring canonical.

    Mirrors `reset_service`'s canonical restore, scoped to the pins this
    call wrote: a name is reverted only when its live pin still matches
    (``mr``, ``applied_at``) what this call recorded, so a pin a concurrent
    `spi service pin` has since replaced is left standing rather than
    reverting someone else's write. Returns the names actually reverted.
    """

    reverted: list[str] = []

    def compute(lock: dict | None) -> dict:
        if lock is None:
            raise PinError(
                f"ConfigMap {IMAGE_LOCK_CONFIGMAP} not found in {IMAGE_LOCK_NAMESPACE} "
                "while reverting a pin."
            )
        nonlocal reverted
        pins = decode_pins(lock)
        data = dict(lock.get("data") or {})
        reverted = []
        for name, pin in written.items():
            live = pins.get(name)
            if live is None or live.mr != pin.mr or live.applied_at != pin.applied_at:
                continue
            pins.pop(name)
            data.update(
                _lock_entry_patch(
                    name,
                    pin.canonical_repository,
                    pin.canonical_tag,
                    pin.canonical_created_at,
                    pin.canonical_digest,
                )
            )
            reverted.append(name)

        annotations = dict((lock.get("metadata") or {}).get("annotations") or {})
        if pins:
            annotations[PINS_ANNOTATION] = encode_pins(pins)
        else:
            annotations.pop(PINS_ANNOTATION, None)
        return {"data": data, "metadata": {"annotations": annotations}}

    mutate_lock(compute, description)
    return reverted


def pin_service(service: str, mr_iid: str) -> list[tuple[str, ServicePin]]:
    """Pin a service (and schema's paired loader) to an MR pipeline image.

    Returns the applied (service, pin) pairs.
    """

    if service not in IMAGE_REGISTRY:
        known = ", ".join(sorted(IMAGE_REGISTRY))
        raise PinError(f"Unknown service {service!r}. Known services: {known}")
    if service == SCHEMA_LOAD_SERVICE_NAME:
        raise PinError("Pin 'schema' instead; the loader follows the schema pin.")
    _refuse_unless_deployable()

    targets = [service]
    if service == SCHEMA_SERVICE_NAME:
        targets.append(SCHEMA_LOAD_SERVICE_NAME)

    # Resolve MR pipeline images once, independent of the lock's state: this
    # is a network round trip and must not repeat on every CAS retry.
    resolved: list[tuple[str, ResolvedImage, dict]] = []
    loader_missing = False
    mr_snapshots: dict[int, dict] = {}
    for name in targets:
        project_id = IMAGE_REGISTRY[name].project_id
        if project_id not in mr_snapshots:
            mr_snapshots[project_id] = fetch_merge_request(project_id, mr_iid)
        try:
            image, mr = resolve_mr_image(name, mr_iid, mr_snapshots[project_id])
        except MissingPipelineImageError:
            if name != SCHEMA_LOAD_SERVICE_NAME:
                raise
            # The MR may not rebuild the loader image; the service pin alone
            # is still a valid experiment.
            loader_missing = True
            continue
        resolved.append((name, image, mr))

    applied_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    results: list[tuple[str, ServicePin]] = []
    released: list[str] = []

    def compute(lock: dict | None) -> dict:
        if lock is None:
            raise PinError(
                f"ConfigMap {IMAGE_LOCK_CONFIGMAP} not found in {IMAGE_LOCK_NAMESPACE}; "
                "is this a core-profile cluster?"
            )
        nonlocal results, released
        lock_data = lock.get("data") or {}
        pins = decode_pins(lock)
        data = dict(lock_data)
        results = []
        released_now: dict[str, ServicePin] = {}

        if loader_missing:
            # A loader still pinned by an earlier MR must not survive as a
            # mismatched pair with the newly pinned schema image; release it.
            stale = pins.pop(SCHEMA_LOAD_SERVICE_NAME, None)
            if stale:
                if not stale.canonical_repository or not stale.canonical_tag:
                    raise PinError(
                        f"{SCHEMA_LOAD_SERVICE_NAME} is pinned to MR !{stale.mr} with no "
                        "canonical image recorded; run 'spi service reset schema' to remove "
                        "the invalid pin, then 'spi reconcile --refresh-images' before re-pinning."
                    )
                released_now[SCHEMA_LOAD_SERVICE_NAME] = stale

        for name, image, mr in resolved:
            key = image_lock_key(name)
            existing = pins.get(name)
            canonical_repository = (
                existing.canonical_repository
                if existing
                else lock_data.get(f"{key}_IMAGE_REPOSITORY", "")
            )
            canonical_tag = (
                existing.canonical_tag if existing else lock_data.get(f"{key}_IMAGE_TAG", "")
            )
            if not canonical_repository or not canonical_tag:
                raise PinError(
                    f"{name}: image lock records no canonical repository or tag; "
                    "run 'spi reconcile --refresh-images' to backfill the lock before pinning."
                )
            pin = ServicePin(
                mr=str(mr_iid),
                branch=mr.get("source_branch", ""),
                repository=image.repository,
                tag=image.tag,
                # First pin captures the canonical image; re-pinning keeps it.
                canonical_repository=canonical_repository,
                canonical_tag=canonical_tag,
                canonical_created_at=(
                    existing.canonical_created_at
                    if existing
                    else lock_data.get(f"{key}_IMAGE_CREATED_AT", "")
                ),
                canonical_digest=(
                    existing.canonical_digest
                    if existing
                    else lock_data.get(f"{key}_IMAGE_DIGEST", "")
                ),
                applied_at=applied_at,
                created_at=image.created_at,
                digest=image.digest,
            )
            pins[name] = pin
            data.update(
                _lock_entry_patch(name, image.repository, image.tag, image.created_at, image.digest)
            )
            results.append((name, pin))

        for name, stale in released_now.items():
            data.update(
                _lock_entry_patch(
                    name,
                    stale.canonical_repository,
                    stale.canonical_tag,
                    stale.canonical_created_at,
                    stale.canonical_digest,
                )
            )
        released = sorted(released_now)

        annotations = dict((lock.get("metadata") or {}).get("annotations") or {})
        if pins:
            annotations[PINS_ANNOTATION] = encode_pins(pins)
        else:
            annotations.pop(PINS_ANNOTATION, None)
        return {"data": data, "metadata": {"annotations": annotations}}

    description = f"Pin {', '.join(targets)} to MR !{mr_iid} image"
    mutate_lock(compute, description)

    written = dict(results)
    blocker = _post_write_maintenance_check()
    if blocker:
        names = ", ".join(sorted(written))
        try:
            reverted = _revert_pin(written, f"Revert pin for {names} ({blocker})")
        except PinError as exc:
            raise PinError(
                f"{blocker} while pinning {names}, and reverting the pin failed: {exc}. "
                f"The pin may still be live; run 'spi service reset {service}' to confirm "
                "and clear it."
            ) from exc
        outcome = (
            f"reverted {', '.join(sorted(reverted))} to its canonical image"
            if reverted
            else "a newer pin had already replaced it, so nothing was reverted"
        )
        raise PinError(f"{blocker} while pinning {names}; {outcome}.")

    reconcile_consumers([name for name, _ in results] + released)
    return results


def reset_service(service: str) -> ResetResult:
    """Release a service pin, restoring its canonical image when one was recorded."""

    if service not in IMAGE_REGISTRY:
        known = ", ".join(sorted(IMAGE_REGISTRY))
        raise PinError(f"Unknown service {service!r}. Known services: {known}")

    targets_all = [service]
    if service == SCHEMA_SERVICE_NAME:
        targets_all.append(SCHEMA_LOAD_SERVICE_NAME)

    restored: list[str] = []
    refresh_required: list[str] = []

    def compute(lock: dict | None) -> dict:
        if lock is None:
            raise PinError(
                f"ConfigMap {IMAGE_LOCK_CONFIGMAP} not found in {IMAGE_LOCK_NAMESPACE}; "
                "is this a core-profile cluster?"
            )
        nonlocal restored, refresh_required
        pins = decode_pins(lock)
        targets = [name for name in targets_all if name in pins]
        if not targets:
            raise PinError(f"{service} is not pinned.")

        data = dict(lock.get("data") or {})
        restored = []
        refresh_required = []
        for name in targets:
            pin = pins.pop(name)
            if not pin.canonical_repository or not pin.canonical_tag:
                refresh_required.append(name)
                continue
            data.update(
                _lock_entry_patch(
                    name,
                    pin.canonical_repository,
                    pin.canonical_tag,
                    pin.canonical_created_at,
                    pin.canonical_digest,
                )
            )
            restored.append(name)

        annotations = dict((lock.get("metadata") or {}).get("annotations") or {})
        if pins:
            annotations[PINS_ANNOTATION] = encode_pins(pins)
        else:
            annotations.pop(PINS_ANNOTATION, None)
        return {"data": data, "metadata": {"annotations": annotations}}

    mutate_lock(compute, f"Reset {service}")
    if restored:
        reconcile_consumers(restored)
    return ResetResult(tuple(restored), tuple(refresh_required))


def live_pins() -> dict[str, ServicePin]:
    """Return active pins from the live cluster, or {} when no lock exists yet.

    Read and decode failures raise PinError: the refresh paths must not
    mistake an unreadable pin state for "no pins" and revert an experiment.
    """

    lock = read_lock(required=False)
    if lock is None:
        return {}
    return decode_pins(lock)


def render_lock_with_pins(
    resolved: dict[str, ResolvedImage],
    branch: str,
    pins: dict[str, ServicePin],
) -> str:
    """Render the image lock with active pins overlaid, as one document.

    The refresh paths build the pinned entries and annotation into the
    rendered ConfigMap so the lock is replaced in a single apply: there is
    no window where the live lock holds canonical images while an
    experiment is active, and a failure between steps cannot revert a pin.
    """

    overlaid = dict(resolved)
    for name, pin in pins.items():
        if name in overlaid:
            overlaid[name] = ResolvedImage(
                name, pin.repository, pin.tag, pin.created_at, pin.digest
            )
    extra = {PINS_ANNOTATION: encode_pins(pins)} if pins else None
    return render_image_lock_configmap(overlaid, branch=branch, extra_annotations=extra)


def apply_image_lock(
    resolved: dict[str, ResolvedImage],
    branch: str,
    description: str = "Update the osdu-image-lock ConfigMap",
    max_attempts: int = LOCK_MUTATION_MAX_ATTEMPTS,
) -> dict[str, ServicePin]:
    """Create or refresh the live image lock, preserving active pins.

    The reusable compare-and-retry entry point for ``spi up
    --refresh-images``, first-lock creation, and ``spi reconcile
    --refresh-images``. Active pins are decoded from a freshly read lock on
    every retry, so a pin or reset applied by a concurrent writer survives
    the refresh. Returns the pins that were preserved.
    """

    active_pins: dict[str, ServicePin] = {}

    def compute(lock: dict | None) -> dict:
        nonlocal active_pins
        active_pins = decode_pins(lock) if lock is not None else {}
        overlaid = dict(resolved)
        for name, pin in active_pins.items():
            if name in overlaid:
                overlaid[name] = ResolvedImage(
                    name, pin.repository, pin.tag, pin.created_at, pin.digest
                )
        timestamp = datetime.now(timezone.utc).isoformat()
        data = build_lock_data(overlaid, branch, timestamp)
        annotations = build_lock_annotations(branch, timestamp)
        if active_pins:
            annotations[PINS_ANNOTATION] = encode_pins(active_pins)
        return {"data": data, "metadata": {"annotations": annotations}}

    mutate_lock(compute, description, max_attempts=max_attempts)
    return active_pins


class _NoBackfillNeeded(Exception):
    """Internal sentinel: the lock is absent or already carries schema-load."""


def apply_schema_load_backfill(
    branch: str = DEFAULT_IMAGE_BRANCH,
    description: str = "Backfill schema-load into the osdu-image-lock ConfigMap",
    max_attempts: int = LOCK_MUTATION_MAX_ATTEMPTS,
) -> bool:
    """Add schema-load's lock entries to a lock that predates them.

    Returns False without writing anything when the lock does not exist yet
    (minimal/bare profiles never create one) or already carries schema-load's
    keys, including its composed ref. Recomputes the patch from the schema
    tag the fresh lock records on every retry, so backfilling never races a
    concurrent schema pin/reset/refresh.
    """

    backfilled = False

    def compute(lock: dict | None) -> dict:
        if lock is None:
            raise _NoBackfillNeeded
        lock_data = lock.get("data") or {}
        if not image_lock_missing_schema_load(lock_data):
            raise _NoBackfillNeeded
        nonlocal backfilled
        patch = schema_load_lock_patch(lock_data, branch=branch)
        data = dict(lock_data)
        data.update(patch)
        backfilled = True
        annotations = dict((lock.get("metadata") or {}).get("annotations") or {})
        return {"data": data, "metadata": {"annotations": annotations}}

    try:
        mutate_lock(compute, description, max_attempts=max_attempts)
    except _NoBackfillNeeded:
        return False
    return backfilled
