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

"""Per-service image pins on the live osdu-image-lock ConfigMap.

A pin points one service at a container image other than its canonical:
either the image built by an OSDU GitLab merge-request pipeline, resolved
from the MR's source branch at its head commit, or a fork-built GHCR image
identified by manifest digest (ADR-031). Pins live in the lock itself: the
service's data keys are overwritten and provenance (origin, canonical
image, owning workflow run, timestamps) is recorded in one JSON
annotation, so `spi reconcile --refresh-images` and `spi up` can re-render
the lock without silently reverting a pin.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
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
    parse_image_digest_ref,
    render_image_lock_configmap,
    require_ghcr_repository,
    resolve_ghcr_manifest,
    resolve_image_commit,
    schema_load_lock_patch,
)
from .shell import run_command, run_process

PINS_ANNOTATION = "spi-stack.osdu.dev/pins"

# Pin origins recorded in the annotation (fork-deployment.md schema).
GITLAB_MR_ORIGIN = "gitlab-mr"
GITHUB_ORIGIN = "github"

# OSDU workloads land in this namespace; Flux names each Helm release
# <targetNamespace>-<name>, so the Deployment and container for a service
# default to "osdu-<service>".
WORKLOAD_NAMESPACE = "osdu"

GITHUB_API_HOST = "https://api.github.com"

# kustomize-controller re-reconciles the lock's substituteFrom consumers when
# an object carrying this label changes; without it a lock write waits for the
# next periodic reconciliation.
_FLUX_WATCH_LABEL = "reconcile.fluxcd.io/watch"

# The stale sweep may reclaim an ephemeral pin on age alone only past this
# threshold; it must exceed any deploy-plus-test budget.
STALE_EPHEMERAL_PIN_AGE_HOURS = 6

_RUN_ID_RE = re.compile(r"^[0-9]+$")
# Only these repositories may record or answer a pin's owning-run state; the
# fork-written source_run_url is display-only and never fetched.
_FORK_SOURCE_REPO_RE = re.compile(r"^azure/osdu-spi-[a-z0-9._-]+$", re.IGNORECASE)

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


class VerifyError(PinError):
    """Typed verification failure: the workload does not provably run the digest."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ResetRefusedError(PinError):
    """Typed no-op refusal: the live pin is not the caller's to reset."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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
    # Fork-deploy provenance; the empty defaults read an older
    # annotation as a non-ephemeral operator pin.
    origin: str = ""
    ephemeral: bool = False
    run_id: str = ""
    source_repo: str = ""
    source_sha: str = ""
    source_run_url: str = ""


@dataclass(frozen=True)
class ResetResult:
    """Services restored immediately and those needing a canonical image refresh."""

    restored: tuple[str, ...]
    refresh_required: tuple[str, ...]


@dataclass(frozen=True)
class VerifyResult:
    """The workload state that proved the expected digest is running."""

    deployment: str
    container: str
    pod: str
    image_id: str


@dataclass(frozen=True)
class SweepResult:
    """Stale ephemeral pins swept, pins kept with the reason, and refresh debts."""

    swept: tuple[str, ...]
    kept: tuple[tuple[str, str], ...]
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
        # A digest pin carries no tag; the informational _IMAGE key falls
        # back to the digest ref rather than a dangling "repository:".
        f"{key}_IMAGE": f"{repository}:{tag}" if tag else image_ref(repository, tag, digest),
        f"{key}_IMAGE_REPOSITORY": repository,
        f"{key}_IMAGE_TAG": tag,
        f"{key}_IMAGE_CREATED_AT": created_at,
        f"{key}_IMAGE_DIGEST": digest,
        f"{key}_IMAGE_REF": image_ref(repository, tag, digest),
    }


def _lock_entry_keys(service: str) -> tuple[str, ...]:
    """The lock keys one service entry owns, read off the patch itself."""

    return tuple(_lock_entry_patch(service, "", "", "", ""))


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
    ``metadata.resourceVersion`` ahead of the ``data``/``annotations``/
    ``labels`` replacement, so the patch verb stays compatible with future
    patch-only RBAC. Only a resourceVersion test failure or a losing create race is
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
                    "labels": {
                        "app.kubernetes.io/managed-by": "osdu-spi-stack",
                        _FLUX_WATCH_LABEL: "Enabled",
                    },
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

        # Re-asserting the labels heals a lock created before the watch label
        # existed, so the first pin write restores label-driven reconciliation.
        labels = dict(((lock.get("metadata") or {}).get("labels")) or {})
        labels[_FLUX_WATCH_LABEL] = "Enabled"
        patch = json.dumps(
            [
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": _resource_version(lock),
                },
                {"op": "add", "path": "/data", "value": data},
                {"op": "add", "path": "/metadata/annotations", "value": annotations},
                {"op": "add", "path": "/metadata/labels", "value": labels},
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


@dataclass(frozen=True)
class _LockMutation:
    """One lock write's outcome and the state it replaced, per service.

    ``written`` maps each mutated service to the pin the write left there,
    or ``None`` for one it released. ``prior_pins`` and ``prior_entries``
    hold the annotation and data-key state those same names carried
    beforehand, captured rather than re-derived so a rollback replays what
    was actually there.
    """

    written: dict[str, ServicePin | None]
    prior_pins: dict[str, ServicePin | None]
    prior_entries: dict[str, dict[str, str]]


class _MutationSuperseded(Exception):
    """A rollback found its write already replaced; never leaves `_revert_pin`."""


def _revert_pin(mutation: _LockMutation, description: str) -> list[str]:
    """Undo this call's lock write, restoring exactly the state it replaced.

    All or nothing: if any mutated name's live pin no longer holds what this
    call left there, a concurrent `spi service pin` owns the entry now, and
    the whole mutation is left standing. Reverting only part of a schema and
    loader pair would produce the very mismatch the pair exists to prevent.
    Restoring replays the captured annotation and data keys verbatim, which
    a pin encoded before `created_at` and `digest` existed cannot survive
    being re-derived from: ADR-017's digest keys are load-bearing and a
    restore must not blank them. Returns the names reverted, empty when the
    mutation no longer stands.

    Residual: a predecessor pin restored here may itself belong to a
    concurrent call that also failed, which the lock cannot distinguish from
    one that succeeded. ADR-031's recorded owning run is what closes that.
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

        for name, pin in mutation.written.items():
            live = pins.get(name)
            if pin is None:
                stands = live is None
            else:
                stands = live is not None and (
                    live.mr,
                    live.run_id,
                    live.digest,
                    live.applied_at,
                ) == (pin.mr, pin.run_id, pin.digest, pin.applied_at)
            if not stands:
                raise _MutationSuperseded

        for name in mutation.written:
            prior = mutation.prior_pins.get(name)
            if prior is None:
                pins.pop(name, None)
            else:
                pins[name] = prior
            entry = mutation.prior_entries.get(name, {})
            for key in _lock_entry_keys(name):
                if key in entry:
                    data[key] = entry[key]
                else:
                    data.pop(key, None)
            reverted.append(name)

        annotations = dict((lock.get("metadata") or {}).get("annotations") or {})
        if pins:
            annotations[PINS_ANNOTATION] = encode_pins(pins)
        else:
            annotations.pop(PINS_ANNOTATION, None)
        return {"data": data, "metadata": {"annotations": annotations}}

    try:
        mutate_lock(compute, description)
    except _MutationSuperseded:
        return []
    return reverted


def _captured_canonical(
    service: str, existing: ServicePin | None, lock_data: dict
) -> tuple[str, str, str, str]:
    """The restore target a pin must record: captured on the first pin from
    the lock's live entry, kept verbatim on a re-pin."""

    key = image_lock_key(service)
    repository = (
        existing.canonical_repository if existing else lock_data.get(f"{key}_IMAGE_REPOSITORY", "")
    )
    tag = existing.canonical_tag if existing else lock_data.get(f"{key}_IMAGE_TAG", "")
    if not repository or not tag:
        raise PinError(
            f"{service}: image lock records no canonical repository or tag; "
            "run 'spi reconcile --refresh-images' to backfill the lock before pinning."
        )
    created_at = (
        existing.canonical_created_at if existing else lock_data.get(f"{key}_IMAGE_CREATED_AT", "")
    )
    digest = existing.canonical_digest if existing else lock_data.get(f"{key}_IMAGE_DIGEST", "")
    return repository, tag, created_at, digest


def _abort_if_maintenance_intervened(mutation: _LockMutation, service: str) -> None:
    """Roll this call's lock write back if a lifecycle run intervened (ADR-029)."""

    blocker = _post_write_maintenance_check()
    if not blocker:
        return
    names = ", ".join(sorted(mutation.written))
    try:
        reverted = _revert_pin(mutation, f"Revert pin for {names} ({blocker})")
    except PinError as exc:
        raise PinError(
            f"{blocker} while pinning {names}, and reverting the pin failed: {exc}. "
            f"The pin may still be live; run 'spi service reset {service}' to confirm "
            "and clear it."
        ) from exc
    outcome = (
        f"restored {', '.join(sorted(reverted))} to its pre-pin image"
        if reverted
        else "a newer pin had already replaced it, so nothing was reverted"
    )
    raise PinError(f"{blocker} while pinning {names}; {outcome}.")


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
    prior_pins: dict[str, ServicePin | None] = {}
    prior_entries: dict[str, dict[str, str]] = {}

    def compute(lock: dict | None) -> dict:
        if lock is None:
            raise PinError(
                f"ConfigMap {IMAGE_LOCK_CONFIGMAP} not found in {IMAGE_LOCK_NAMESPACE}; "
                "is this a core-profile cluster?"
            )
        nonlocal results, released, prior_pins, prior_entries
        lock_data = lock.get("data") or {}
        pins = decode_pins(lock)
        data = dict(lock_data)
        results = []
        released_now: dict[str, ServicePin] = {}
        prior_pins = {}
        prior_entries = {}

        def capture(name: str) -> None:
            prior_pins[name] = pins.get(name)
            prior_entries[name] = {
                key: lock_data[key] for key in _lock_entry_keys(name) if key in lock_data
            }

        if loader_missing:
            # A loader still pinned by an earlier MR must not survive as a
            # mismatched pair with the newly pinned schema image; release it.
            stale = pins.get(SCHEMA_LOAD_SERVICE_NAME)
            if stale:
                if not stale.canonical_repository or not stale.canonical_tag:
                    raise PinError(
                        f"{SCHEMA_LOAD_SERVICE_NAME} is pinned to MR !{stale.mr} with no "
                        "canonical image recorded; run 'spi service reset schema' to remove "
                        "the invalid pin, then 'spi reconcile --refresh-images' before re-pinning."
                    )
                capture(SCHEMA_LOAD_SERVICE_NAME)
                pins.pop(SCHEMA_LOAD_SERVICE_NAME)
                released_now[SCHEMA_LOAD_SERVICE_NAME] = stale

        for name, image, mr in resolved:
            existing = pins.get(name)
            capture(name)
            canonical = _captured_canonical(name, existing, lock_data)
            pin = ServicePin(
                mr=str(mr_iid),
                branch=mr.get("source_branch", ""),
                repository=image.repository,
                tag=image.tag,
                canonical_repository=canonical[0],
                canonical_tag=canonical[1],
                canonical_created_at=canonical[2],
                canonical_digest=canonical[3],
                applied_at=applied_at,
                created_at=image.created_at,
                digest=image.digest,
                origin=GITLAB_MR_ORIGIN,
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

    description_targets = [name for name, _, _ in resolved] if loader_missing else targets
    description = f"Pin {', '.join(description_targets)} to MR !{mr_iid} image"
    mutate_lock(compute, description)

    written: dict[str, ServicePin | None] = dict(results)
    written.update({name: None for name in released})
    mutation = _LockMutation(written=written, prior_pins=prior_pins, prior_entries=prior_entries)
    _abort_if_maintenance_intervened(mutation, service)

    reconcile_consumers([name for name, _ in results] + released)
    return results


def pin_service_image(
    service: str,
    image: str,
    *,
    ephemeral: bool = False,
    run_id: str = "",
    source_repo: str = "",
    source_sha: str = "",
    source_run_url: str = "",
) -> ServicePin:
    """Pin a service to a fork-built GHCR image by manifest digest (ADR-031).

    Unlike an MR pin the target is exactly the named service: a fork build
    ships one image, so a schema pin never pins the loader, and a loader
    left pinned by an earlier MR is released to its canonical image so no
    mismatched pair survives. An ephemeral pin returns right after the lock
    write: reconciliation follows the lock's watch label, and ``verify`` is
    the deploy gate. Returns the applied pin.
    """

    if service not in IMAGE_REGISTRY:
        known = ", ".join(sorted(IMAGE_REGISTRY))
        raise PinError(f"Unknown service {service!r}. Known services: {known}")
    if service == SCHEMA_LOAD_SERVICE_NAME:
        raise PinError(
            f"{SCHEMA_LOAD_SERVICE_NAME} cannot be pinned directly; fork builds "
            "ship the schema service image, and the loader stays canonical."
        )

    try:
        repository, digest = parse_image_digest_ref(image)
        require_ghcr_repository(repository)
    except ImageResolutionError as exc:
        raise PinError(str(exc)) from exc

    if ephemeral and not (run_id and source_repo and source_sha):
        raise PinError(
            "--ephemeral requires --run-id, --source-repo, and --source-sha: the "
            "sweep proves a pin stale from its owning run, which these identify."
        )
    if run_id and not _RUN_ID_RE.match(run_id):
        raise PinError(f"--run-id must be a numeric GitHub Actions run id, got {run_id!r}.")
    if source_repo and not _FORK_SOURCE_REPO_RE.match(source_repo):
        raise PinError(
            f"--source-repo {source_repo!r} is not an allow-listed fork repository "
            "(Azure/osdu-spi-*); the sweep could never query this pin's owning run."
        )

    _refuse_unless_deployable()
    try:
        resolve_ghcr_manifest(repository, digest)
    except ImageResolutionError as exc:
        raise PinError(str(exc)) from exc

    applied_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    applied: ServicePin | None = None
    released: list[str] = []
    prior_pins: dict[str, ServicePin | None] = {}
    prior_entries: dict[str, dict[str, str]] = {}

    def compute(lock: dict | None) -> dict:
        if lock is None:
            raise PinError(
                f"ConfigMap {IMAGE_LOCK_CONFIGMAP} not found in {IMAGE_LOCK_NAMESPACE}; "
                "is this a core-profile cluster?"
            )
        nonlocal applied, released, prior_pins, prior_entries
        lock_data = lock.get("data") or {}
        pins = decode_pins(lock)
        data = dict(lock_data)
        existing = pins.get(service)
        prior_pins = {service: existing}
        prior_entries = {
            service: {key: lock_data[key] for key in _lock_entry_keys(service) if key in lock_data}
        }

        released_now: dict[str, ServicePin] = {}
        if service == SCHEMA_SERVICE_NAME:
            # A loader still pinned by an earlier MR must not survive as a
            # mismatched pair with the fork-pinned schema image; release it.
            stale = pins.get(SCHEMA_LOAD_SERVICE_NAME)
            if stale:
                if not stale.canonical_repository or not stale.canonical_tag:
                    raise PinError(
                        f"{SCHEMA_LOAD_SERVICE_NAME} is pinned to MR !{stale.mr} with no "
                        "canonical image recorded; run 'spi service reset schema' to remove "
                        "the invalid pin, then 'spi reconcile --refresh-images' before re-pinning."
                    )
                prior_pins[SCHEMA_LOAD_SERVICE_NAME] = stale
                prior_entries[SCHEMA_LOAD_SERVICE_NAME] = {
                    key: lock_data[key]
                    for key in _lock_entry_keys(SCHEMA_LOAD_SERVICE_NAME)
                    if key in lock_data
                }
                pins.pop(SCHEMA_LOAD_SERVICE_NAME)
                released_now[SCHEMA_LOAD_SERVICE_NAME] = stale

        canonical = _captured_canonical(service, existing, lock_data)
        applied = ServicePin(
            mr="",
            branch="",
            repository=repository,
            tag="",
            canonical_repository=canonical[0],
            canonical_tag=canonical[1],
            canonical_created_at=canonical[2],
            canonical_digest=canonical[3],
            applied_at=applied_at,
            digest=digest,
            origin=GITHUB_ORIGIN,
            ephemeral=ephemeral,
            run_id=run_id,
            source_repo=source_repo,
            source_sha=source_sha,
            source_run_url=source_run_url,
        )
        pins[service] = applied
        data.update(_lock_entry_patch(service, repository, "", "", digest))
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
        annotations[PINS_ANNOTATION] = encode_pins(pins)
        return {"data": data, "metadata": {"annotations": annotations}}

    mutate_lock(compute, f"Pin {service} to {digest[:19]}")
    assert applied is not None
    written: dict[str, ServicePin | None] = {service: applied}
    written.update({name: None for name in released})
    mutation = _LockMutation(written=written, prior_pins=prior_pins, prior_entries=prior_entries)
    _abort_if_maintenance_intervened(mutation, service)

    # A run-owned pin converges through the lock's watch label: the fork
    # deploy role cannot patch Kustomizations, and verify gates the deploy.
    if not ephemeral:
        reconcile_consumers([service] + released)
    return applied


def reset_service(service: str, if_run: str = "") -> ResetResult:
    """Release a service pin, restoring its canonical image when one was recorded.

    With ``if_run`` the reset is ownership-conditional (ADR-031): it acts
    only while the live pin still records that owning run, so a crashed
    run's always-run restore job cannot clobber a newer sibling's pin. A
    refusal is a typed ``ResetRefusedError`` and mutates nothing. Run-owned
    pins are single-service, so ``if_run`` never pairs the schema loader,
    and the restore converges through the lock's watch label rather than an
    explicit reconciliation.
    """

    if service not in IMAGE_REGISTRY:
        known = ", ".join(sorted(IMAGE_REGISTRY))
        raise PinError(f"Unknown service {service!r}. Known services: {known}")

    targets_all = [service]
    if service == SCHEMA_SERVICE_NAME and not if_run:
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
            if if_run:
                raise ResetRefusedError(
                    "not_pinned",
                    f"{service} is not pinned; nothing to restore for run {if_run}.",
                )
            raise PinError(f"{service} is not pinned.")
        if if_run:
            live = pins[service]
            if live.run_id != if_run:
                owner = (
                    f"run {live.run_id}"
                    if live.run_id
                    else (f"MR !{live.mr}" if live.mr else "an operator pin")
                )
                raise ResetRefusedError(
                    "run_mismatch",
                    f"{service} is pinned by {owner}, not run {if_run}; leaving the pin standing.",
                )

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
    # The run-conditional restore runs under the fork deploy role, which
    # cannot patch Kustomizations; the lock's watch label converges it.
    if restored and not if_run:
        reconcile_consumers(restored)
    return ResetResult(tuple(restored), tuple(refresh_required))


def _kubectl_read_json(args: list[str], describe: str) -> dict | None:
    """Silent kubectl read that distinguishes absent (None) from unreachable
    (raise), so a verify failure can never be mistaken for a missing object."""

    result = run_process(
        ["kubectl", *args, "--ignore-not-found", "-o", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or "kubectl failed"
        raise PinError(f"Could not read {describe}: {detail}")
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PinError(f"Could not parse {describe}: {exc}") from exc


def _collision_note(service: str, digest: str) -> str:
    """Name the pin now holding the lock when it is not ours: the
    cross-pipeline guard's fail-fast diagnostic (ADR-031)."""

    try:
        pin = live_pins().get(service)
    except PinError:
        return ""
    if pin is None:
        return " The image lock records no pin for this service."
    if pin.digest == digest:
        return ""
    if pin.run_id:
        source = pin.source_run_url or pin.source_repo or "unknown source"
        return f" The image lock is now pinned by run {pin.run_id} ({source})."
    if pin.mr:
        return f" The image lock is now pinned to MR !{pin.mr}."
    return " The image lock now records a different pin."


def verify_service_image(
    service: str,
    image: str,
    deployment: str | None = None,
    container: str | None = None,
) -> VerifyResult:
    """Assert the service's Deployment and a running pod carry the digest.

    Deploy success is the running pod carrying the digest (ADR-031), not the
    lock write having landed: a deploy overwritten by a colliding pipeline
    fails here, naming the colliding run, instead of producing a silently
    wrong test result. Typed failures raise ``VerifyError``; an unreachable
    cluster raises plain ``PinError``.
    """

    if service not in IMAGE_REGISTRY:
        known = ", ".join(sorted(IMAGE_REGISTRY))
        raise PinError(f"Unknown service {service!r}. Known services: {known}")
    try:
        _repository, digest = parse_image_digest_ref(image)
    except ImageResolutionError as exc:
        raise PinError(str(exc)) from exc

    deployment = deployment or f"{WORKLOAD_NAMESPACE}-{service}"
    container = container or deployment

    dep = _kubectl_read_json(
        ["get", "deployment", deployment, "-n", WORKLOAD_NAMESPACE],
        f"Deployment {deployment}",
    )
    if dep is None:
        raise VerifyError(
            "deployment_not_found",
            f"Deployment {deployment} not found in namespace {WORKLOAD_NAMESPACE}; "
            "set K8S_DEPLOYMENT_NAME if this service deviates.",
        )

    spec = dep.get("spec") or {}
    containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
    template_image = next(
        (entry.get("image", "") for entry in containers if entry.get("name") == container),
        None,
    )
    if template_image is None:
        raise VerifyError(
            "container_not_found",
            f"Deployment {deployment} has no container named {container!r}; "
            "set K8S_CONTAINER_NAME if this service deviates.",
        )
    if digest not in template_image:
        raise VerifyError(
            "template_mismatch",
            f"Deployment {deployment} pod template runs {template_image!r}, "
            f"not {digest}." + _collision_note(service, digest),
        )

    generation = (dep.get("metadata") or {}).get("generation", 0)
    dep_status = dep.get("status") or {}
    replicas = spec.get("replicas", 1)
    observed = dep_status.get("observedGeneration", 0)
    updated = dep_status.get("updatedReplicas", 0)
    available = dep_status.get("availableReplicas", 0)
    total = dep_status.get("replicas", 0)
    # Kubernetes' Deployment-complete predicate: every status count equals the
    # desired count exactly, so a scale-down still draining old pods (counts
    # above desired) cannot read as complete.
    if observed < generation or updated != replicas or total != replicas or available != replicas:
        raise VerifyError(
            "rollout_incomplete",
            f"Deployment {deployment} rollout is not complete ({updated}/{replicas} "
            f"updated, {total} total, {available} available, generation "
            f"{observed}/{generation}); retry once the rollout settles.",
        )

    selector = (spec.get("selector") or {}).get("matchLabels") or {}
    if not selector:
        raise PinError(f"Deployment {deployment} has no matchLabels selector to find its pods.")
    label_arg = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
    pod_list = (
        _kubectl_read_json(
            ["get", "pods", "-n", WORKLOAD_NAMESPACE, "-l", label_arg],
            f"pods for Deployment {deployment}",
        )
        or {}
    )
    running = [
        pod
        for pod in pod_list.get("items", [])
        if (pod.get("status") or {}).get("phase") == "Running"
    ]
    if not running:
        raise VerifyError(
            "pod_not_running",
            f"No running pods matched {label_arg} in namespace {WORKLOAD_NAMESPACE}.",
        )

    seen: list[str] = []
    for pod in running:
        for entry in (pod.get("status") or {}).get("containerStatuses") or []:
            if entry.get("name") != container:
                continue
            image_id = entry.get("imageID", "")
            if digest in image_id:
                return VerifyResult(
                    deployment=deployment,
                    container=container,
                    pod=(pod.get("metadata") or {}).get("name", ""),
                    image_id=image_id,
                )
            seen.append(image_id or "<no imageID>")
    detail = "; ".join(sorted(set(seen))) if seen else f"no status for container {container!r}"
    raise VerifyError(
        "pod_mismatch",
        f"No running pod's {container!r} imageID carries {digest} (saw: {detail})."
        + _collision_note(service, digest),
    )


def _github_run_status(source_repo: str, run_id: str) -> str | None:
    """Return the owning workflow run's status, or None when unreachable.

    The URL is built only from the allow-listed ``source_repo`` and numeric
    ``run_id``; a repository outside the allow-list is never fetched and
    reads as unreachable, leaving the age threshold to decide.
    """

    if not _FORK_SOURCE_REPO_RE.match(source_repo) or not _RUN_ID_RE.match(run_id):
        return None
    headers = {"User-Agent": "spi-stack-resolver", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{GITHUB_API_HOST}/repos/{source_repo}/actions/runs/{run_id}", headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
            payload = json.loads(resp.read())
    except urllib.error.HTTPError:
        # A 404 may be a deleted run, but also a repository this caller cannot
        # read; neither proves the run terminal, so the age threshold decides.
        return None
    except (TimeoutError, urllib.error.URLError, ConnectionError, json.JSONDecodeError):
        return None
    status = payload.get("status", "") if isinstance(payload, dict) else ""
    return status or None


def _pin_age_exceeds_threshold(pin: ServicePin, now: datetime) -> bool:
    try:
        applied = datetime.fromisoformat(pin.applied_at.replace("Z", "+00:00"))
    except ValueError:
        # An undecodable timestamp cannot prove the pin young; sweep it.
        return True
    if applied.tzinfo is None:
        applied = applied.replace(tzinfo=timezone.utc)
    return now - applied > timedelta(hours=STALE_EPHEMERAL_PIN_AGE_HOURS)


def sweep_stale_ephemeral_pins() -> SweepResult:
    """Sweep abandoned ephemeral pins: ADR-031's weekday backstop.

    A pin is stale when its owning workflow run reports a terminal state or,
    when that state is unreachable, when its age exceeds
    ``STALE_EPHEMERAL_PIN_AGE_HOURS``. Operator pins never carry the marker
    and are never considered.
    """

    candidates = {name: pin for name, pin in live_pins().items() if pin.ephemeral}
    if not candidates:
        return SweepResult((), (), ())

    now = datetime.now(timezone.utc)
    stale: dict[str, ServicePin] = {}
    kept: list[tuple[str, str]] = []
    for name, pin in sorted(candidates.items()):
        run_state = _github_run_status(pin.source_repo, pin.run_id)
        if run_state == "completed":
            stale[name] = pin
        elif run_state is None:
            if _pin_age_exceeds_threshold(pin, now):
                stale[name] = pin
            else:
                kept.append((name, "run state unreachable and pin younger than threshold"))
        else:
            kept.append((name, f"run {pin.run_id} is {run_state}"))

    if not stale:
        return SweepResult((), tuple(kept), ())

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
        data = dict(lock.get("data") or {})
        restored = []
        refresh_required = []
        for name, expected in stale.items():
            live = pins.get(name)
            # A pin re-placed since the staleness check is not the pin found
            # stale; leave it standing.
            if live is None or (live.run_id, live.digest, live.applied_at) != (
                expected.run_id,
                expected.digest,
                expected.applied_at,
            ):
                continue
            pins.pop(name)
            if not live.canonical_repository or not live.canonical_tag:
                refresh_required.append(name)
                continue
            data.update(
                _lock_entry_patch(
                    name,
                    live.canonical_repository,
                    live.canonical_tag,
                    live.canonical_created_at,
                    live.canonical_digest,
                )
            )
            restored.append(name)

        annotations = dict((lock.get("metadata") or {}).get("annotations") or {})
        if pins:
            annotations[PINS_ANNOTATION] = encode_pins(pins)
        else:
            annotations.pop(PINS_ANNOTATION, None)
        return {"data": data, "metadata": {"annotations": annotations}}

    mutate_lock(compute, f"Sweep stale ephemeral pins ({', '.join(sorted(stale))})")
    if restored:
        reconcile_consumers(restored)
    return SweepResult(tuple(sorted(restored)), tuple(kept), tuple(sorted(refresh_required)))


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
