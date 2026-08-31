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

"""Per-service image pins: resolution, lock mutation, verification, and sweep."""

import json
import subprocess
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from spi import cli, pins, status
from spi.deploy_record import DeployRecord, DeployRecordError
from spi.images import (
    ImageNotFoundError,
    ImageResolutionError,
    ResolvedImage,
    parse_image_digest_ref,
    require_ghcr_repository,
)
from spi.pins import (
    LOCK_MUTATION_MAX_ATTEMPTS,
    LockConflictError,
    MissingPipelineImageError,
    PinError,
    ResetRefusedError,
    ResetResult,
    ServicePin,
    SweepResult,
    VerifyError,
    decode_pins,
    encode_pins,
    pin_service,
    pin_service_image,
    ref_slug,
    reset_service,
    sweep_stale_ephemeral_pins,
    verify_service_image,
)
from spi.status import KustomizationReadiness, StatusError, StatusReason


def _pin(**overrides) -> ServicePin:
    fields: dict = dict(
        mr="847",
        branch="fix/upgrade-core-lib",
        repository="registry/schema-service-fix-upgrade-core-lib",
        tag="a" * 40,
        canonical_repository="registry/schema-service-master",
        canonical_tag="c" * 40,
        canonical_created_at="2026-08-20T00:00:00Z",
        canonical_digest="sha256:abc",
        applied_at="2026-08-25T00:00:00Z",
    )
    fields.update(overrides)
    return ServicePin(**fields)


_GHCR_DIGEST = "sha256:" + "f" * 64
_GHCR_IMAGE = f"ghcr.io/azure/storage@{_GHCR_DIGEST}"


def _image_pin(**overrides) -> ServicePin:
    """An ephemeral fork-deploy pin as `spi service pin --image` records it."""

    fields: dict = dict(
        mr="",
        branch="",
        repository="ghcr.io/azure/storage",
        tag="",
        canonical_repository="repo/storage-master",
        canonical_tag="c" * 40,
        canonical_created_at="2026-08-20T00:00:00Z",
        canonical_digest="sha256:old",
        applied_at="2026-08-25T00:00:00Z",
        digest=_GHCR_DIGEST,
        origin="github",
        ephemeral=True,
        run_id="1234",
        source_repo="Azure/osdu-spi-storage",
        source_sha="b" * 40,
        source_run_url="https://github.com/Azure/osdu-spi-storage/actions/runs/1234",
    )
    fields.update(overrides)
    return ServicePin(**fields)


def _readiness(ready=True, blocker: str | None = None) -> KustomizationReadiness:
    """A `collect_kustomization_readiness` result, Ready by default.

    ``blocker`` names the not-Ready Kustomization for the refusal-path
    tests, mirroring the `kustomization_not_ready` reason `spi status` emits.
    """
    reason = None
    if not ready:
        name = blocker or "spi-osdu-services"
        reason = StatusReason(
            code="kustomization_not_ready",
            message=f"{name}: Progressing",
            resource=f"kustomization/osdu-flux/{name}",
        )
    return KustomizationReadiness(items=(), states=(), ready=ready, reason=reason)


def _deploy_record(maintenance=False) -> DeployRecord:
    return DeployRecord(
        ref="v0.6.0",
        resolved_commit="d" * 40,
        deployed_at="2026-08-25T00:00:00Z",
        cli_version="0.6.0",
        profile="core",
        maintenance=maintenance,
    )


def _lock(data=None, pins_annotation="", resource_version="1"):
    annotations = {}
    if pins_annotation:
        annotations[pins.PINS_ANNOTATION] = pins_annotation
    return {
        "metadata": {"annotations": annotations, "resourceVersion": resource_version},
        "data": data or {},
    }


def _canonical_data(*services):
    data = {}
    for service in services:
        key = service.upper().replace("-", "_")
        data.update(
            {
                f"{key}_IMAGE_REPOSITORY": f"repo/{service}-master",
                f"{key}_IMAGE_TAG": "c" * 40,
                f"{key}_IMAGE_CREATED_AT": "then",
                f"{key}_IMAGE_DIGEST": "sha256:old",
            }
        )
    return data


def _pinned_data(service: str, pin: ServicePin) -> dict:
    """The lock entry a pin write leaves behind, alongside its annotation."""
    key = service.upper().replace("-", "_")
    return {
        f"{key}_IMAGE": f"{pin.repository}:{pin.tag}",
        f"{key}_IMAGE_REPOSITORY": pin.repository,
        f"{key}_IMAGE_TAG": pin.tag,
        f"{key}_IMAGE_CREATED_AT": pin.created_at,
        f"{key}_IMAGE_DIGEST": pin.digest,
        f"{key}_IMAGE_REF": (
            f"{pin.repository}@{pin.digest}" if pin.digest else f"{pin.repository}:{pin.tag}"
        ),
    }


def _wire_lock(monkeypatch, lock, conflicts: int = 0) -> dict:
    """Wire pins.read_lock/run_command to an in-memory ConfigMap so the real
    ``mutate_lock`` compare-and-retry loop runs against a controllable fake
    cluster instead of a mocked-out patch call.

    ``conflicts`` simulates that many concurrent writers landing a change
    (an unrelated pin annotation plus a bumped resourceVersion) between the
    read and the patch attempt, forcing that many JSON Patch ``test``
    failures before the loop's own patch can land.

    Returns a dict with ``patch`` (the final applied ``(data, pins)`` pair),
    ``reconciled`` (the services passed to ``reconcile_consumers``), and
    ``attempts`` (how many patch calls were issued).
    """

    box = [lock]
    attempts = 0
    conflicts_left = conflicts
    calls: dict = {
        "patch": None,
        "reconciled": None,
        "attempts": 0,
        "description": None,
        "box": box,
    }

    def fake_read_lock(required=True):
        return box[0]

    def fake_run_command(cmd, description=None, check=True, **kwargs):
        nonlocal attempts, conflicts_left
        assert cmd[:3] == ["kubectl", "patch", "configmap"]
        calls["description"] = description
        attempts += 1
        patch_ops = json.loads(cmd[cmd.index("-p") + 1])
        test_op, data_op, annotations_op = patch_ops
        current_rv = (box[0].get("metadata") or {}).get("resourceVersion", "0")

        if conflicts_left > 0:
            conflicts_left -= 1
            # Simulate another writer landing an unrelated pin concurrently.
            other_annotations = dict((box[0].get("metadata") or {}).get("annotations") or {})
            other_pins = decode_pins(box[0])
            other_pins["_concurrent"] = _pin(mr="999")
            other_annotations[pins.PINS_ANNOTATION] = encode_pins(other_pins)
            box[0] = {
                "metadata": {
                    "resourceVersion": str(int(current_rv) + 1),
                    "annotations": other_annotations,
                },
                "data": dict(box[0].get("data") or {}),
            }
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="the object has been modified; please try again"
            )

        if test_op["value"] != current_rv:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="the object has been modified; please try again"
            )

        new_lock = {
            "metadata": {
                "resourceVersion": str(int(current_rv) + 1),
                "annotations": dict(annotations_op["value"]),
            },
            "data": dict(data_op["value"]),
        }
        box[0] = new_lock
        calls["patch"] = (dict(data_op["value"]), decode_pins(new_lock))
        calls["attempts"] = attempts
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(new_lock), stderr="")

    monkeypatch.setattr(pins, "read_lock", fake_read_lock)
    monkeypatch.setattr(pins, "run_command", fake_run_command)
    monkeypatch.setattr(pins.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        pins, "reconcile_consumers", lambda names: calls.__setitem__("reconciled", names)
    )
    monkeypatch.setattr(pins, "read_deploy_record", lambda required=False: _deploy_record())
    monkeypatch.setattr(status, "collect_kustomization_readiness", lambda: _readiness())
    return calls


class TestRefSlug:
    def test_matches_gitlab_ci_commit_ref_slug_rules(self):
        assert ref_slug("fix/Upgrade_Core-Lib") == "fix-upgrade-core-lib"
        assert ref_slug("--weird--") == "weird"
        assert len(ref_slug("x" * 100)) == 63

    def test_truncation_strips_trailing_hyphen(self):
        assert not ref_slug("a" * 62 + "/b").endswith("-")


class TestPinCodec:
    def test_round_trip(self):
        original = {"schema": _pin()}
        assert decode_pins(_lock(pins_annotation=encode_pins(original))) == original

    def test_missing_annotation_reads_empty(self):
        assert decode_pins(_lock()) == {}

    def test_corrupt_annotation_raises(self):
        with pytest.raises(PinError, match="Corrupt"):
            decode_pins(_lock(pins_annotation="not json"))
        with pytest.raises(PinError, match="Corrupt"):
            decode_pins(_lock(pins_annotation=json.dumps({"schema": {"mr": "1"}})))


class TestResolveMrImage:
    def test_resolves_source_branch_slug_at_head_sha(self, monkeypatch):
        sha = "b" * 40
        monkeypatch.setattr(
            pins, "fetch_merge_request", lambda pid, iid: {"source_branch": "fix/x", "sha": sha}
        )
        captured = {}

        def fake_resolve(service, entry, branch, sha):
            captured.update(service=service, branch=branch, sha=sha)
            return ResolvedImage(service, "repo/schema-service-fix-x", sha[:12], "", "")

        monkeypatch.setattr(pins, "resolve_image_commit", fake_resolve)
        image, _mr = pins.resolve_mr_image("schema", "847")
        assert captured == {"service": "schema", "branch": "fix-x", "sha": sha}
        assert image.tag == sha[:12]

    def test_falls_back_to_trusted_branch_copy(self, monkeypatch):
        sha = "b" * 40
        monkeypatch.setattr(
            pins, "fetch_merge_request", lambda pid, iid: {"source_branch": "fix/x", "sha": sha}
        )
        attempts = []

        def fake_resolve(service, entry, branch, sha):
            attempts.append(branch)
            if branch != "trusted-fix-x":
                raise ImageNotFoundError(f"{service}: repository not found")
            return ResolvedImage(service, "repo/schema-service-trusted-fix-x", sha, "", "")

        monkeypatch.setattr(pins, "resolve_image_commit", fake_resolve)
        image, _mr = pins.resolve_mr_image("schema", "847")
        assert attempts == ["fix-x", "trusted-fix-x"]
        assert image.repository.endswith("trusted-fix-x")

    def test_trusted_slug_truncates_after_prefix(self, monkeypatch):
        branch = "x" * 100
        monkeypatch.setattr(
            pins, "fetch_merge_request", lambda pid, iid: {"source_branch": branch, "sha": "b" * 40}
        )
        attempts = []

        def fake_resolve(service, entry, branch, sha):
            attempts.append(branch)
            raise ImageNotFoundError("nope")

        monkeypatch.setattr(pins, "resolve_image_commit", fake_resolve)
        with pytest.raises(PinError):
            pins.resolve_mr_image("schema", "847")
        assert attempts == ["x" * 63, "trusted-" + "x" * 55]
        assert all(len(candidate) <= 63 for candidate in attempts)

    def test_missing_pipeline_image_names_the_mr(self, monkeypatch):
        monkeypatch.setattr(
            pins,
            "fetch_merge_request",
            lambda pid, iid: {"source_branch": "fix/x", "sha": "b" * 40},
        )

        def raise_missing(service, entry, branch, sha):
            raise ImageNotFoundError(f"{service}: no tag for commit")

        monkeypatch.setattr(pins, "resolve_image_commit", raise_missing)
        with pytest.raises(PinError, match="containerize pipeline"):
            pins.resolve_mr_image("schema", "847")

    def test_registry_lookup_failure_is_not_reported_as_missing(self, monkeypatch):
        monkeypatch.setattr(
            pins,
            "fetch_merge_request",
            lambda pid, iid: {"source_branch": "fix/x", "sha": "b" * 40},
        )
        attempts = []

        def raise_lookup_failure(service, entry, branch, sha):
            attempts.append(branch)
            raise ImageResolutionError("GitLab API unreachable")

        monkeypatch.setattr(pins, "resolve_image_commit", raise_lookup_failure)
        with pytest.raises(ImageResolutionError, match="unreachable"):
            pins.resolve_mr_image("schema", "847")
        assert attempts == ["fix-x"]

    def test_nonexistent_mr_becomes_pin_error(self, monkeypatch):
        import urllib.error
        from email.message import Message

        def raise_404(url):
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)

        monkeypatch.setattr(pins, "gitlab_get", raise_404)
        with pytest.raises(PinError, match="not found"):
            pins.fetch_merge_request(26, "99999")


class TestPinService:
    def _wire(self, monkeypatch, lock, resolved_names, conflicts: int = 0):
        calls = _wire_lock(monkeypatch, lock, conflicts=conflicts)
        calls["fetches"] = 0
        mr = {"source_branch": "fix/x", "sha": "b" * 40}

        def fake_fetch(project_id, mr_iid):
            calls["fetches"] += 1
            return mr

        def fake_resolve(service, mr_iid, mr_snapshot=None):
            assert mr_snapshot is mr
            if service not in resolved_names:
                raise MissingPipelineImageError(f"{service}: no image")
            return (
                ResolvedImage(service, f"repo/{service}-fix-x", "b" * 40, "now", "sha256:new"),
                mr_snapshot,
            )

        monkeypatch.setattr(pins, "fetch_merge_request", fake_fetch)
        monkeypatch.setattr(pins, "resolve_mr_image", fake_resolve)
        return calls

    def test_unknown_service_rejected(self, monkeypatch):
        with pytest.raises(PinError, match="Unknown service"):
            pin_service("nope", "1")

    def test_schema_load_direct_pin_rejected(self):
        with pytest.raises(PinError, match="Pin 'schema'"):
            pin_service("schema-load", "1")

    def test_pin_refused_while_maintenance_set(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(data=_canonical_data("storage")), {"storage"})
        monkeypatch.setattr(
            pins,
            "read_deploy_record",
            lambda required=False: _deploy_record(maintenance=True),
        )

        with pytest.raises(PinError, match="maintenance"):
            pin_service("storage", "42")

        assert calls["fetches"] == 0
        assert calls["patch"] is None

    def test_pin_refused_while_not_ready(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(data=_canonical_data("storage")), {"storage"})
        monkeypatch.setattr(
            status,
            "collect_kustomization_readiness",
            lambda: _readiness(ready=False, blocker="spi-osdu-services"),
        )

        with pytest.raises(PinError, match="spi-osdu-services: Progressing"):
            pin_service("storage", "42")

        assert calls["fetches"] == 0
        assert calls["patch"] is None

    def test_pin_readiness_check_failure_becomes_pin_error(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(data=_canonical_data("storage")), {"storage"})

        def raise_unreachable():
            raise StatusError("Could not read Flux Kustomizations: connection refused")

        monkeypatch.setattr(status, "collect_kustomization_readiness", raise_unreachable)

        with pytest.raises(PinError, match="connection refused"):
            pin_service("storage", "42")

        assert calls["fetches"] == 0
        assert calls["patch"] is None

    def test_pin_refused_without_deploy_record(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(data=_canonical_data("storage")), {"storage"})
        monkeypatch.setattr(pins, "read_deploy_record", lambda required=False: None)

        with pytest.raises(PinError, match="deploy record"):
            pin_service("storage", "42")

        assert calls["fetches"] == 0
        assert calls["patch"] is None

    def test_pin_record_read_failure_becomes_pin_error(self, monkeypatch):
        self._wire(monkeypatch, _lock(data=_canonical_data("storage")), {"storage"})

        def raise_unreachable(required=False):
            raise DeployRecordError("Could not read spi-deploy-record: connection refused")

        monkeypatch.setattr(pins, "read_deploy_record", raise_unreachable)

        with pytest.raises(PinError, match="connection refused"):
            pin_service("storage", "42")

    def test_first_pin_captures_canonical_from_lock(self, monkeypatch):
        lock = _lock(
            data={
                "STORAGE_IMAGE_REPOSITORY": "repo/storage-master",
                "STORAGE_IMAGE_TAG": "c" * 40,
                "STORAGE_IMAGE_CREATED_AT": "then",
                "STORAGE_IMAGE_DIGEST": "sha256:old",
            }
        )
        calls = self._wire(monkeypatch, lock, {"storage"})
        results = pin_service("storage", "42")

        assert [name for name, _ in results] == ["storage"]
        data, saved = calls["patch"]
        assert data["STORAGE_IMAGE_TAG"] == "b" * 40
        assert saved["storage"].canonical_repository == "repo/storage-master"
        assert saved["storage"].canonical_tag == "c" * 40
        assert calls["reconciled"] == ["storage"]

    def test_repin_keeps_original_canonical(self, monkeypatch):
        existing = _pin(mr="1", canonical_repository="repo/storage-master")
        lock = _lock(
            data={"STORAGE_IMAGE_REPOSITORY": "repo/storage-fix-x"},
            pins_annotation=encode_pins({"storage": existing}),
        )
        calls = self._wire(monkeypatch, lock, {"storage"})
        pin_service("storage", "2")

        _, saved = calls["patch"]
        assert saved["storage"].mr == "2"
        assert saved["storage"].canonical_repository == "repo/storage-master"

    def test_first_pin_rejects_missing_canonical_lock_data(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(), {"storage"})

        with pytest.raises(PinError, match="refresh-images"):
            pin_service("storage", "42")

        assert calls["patch"] is None
        assert calls["reconciled"] is None

    def test_schema_pin_pairs_the_loader_from_one_mr_snapshot(self, monkeypatch):
        lock = _lock(data=_canonical_data("schema", "schema-load"))
        calls = self._wire(monkeypatch, lock, {"schema", "schema-load"})
        results = pin_service("schema", "847")
        assert [name for name, _ in results] == ["schema", "schema-load"]
        assert calls["description"] == "Pin schema, schema-load to MR !847 image"
        assert calls["fetches"] == 1
        assert calls["reconciled"] == ["schema", "schema-load"]

    def test_schema_pin_tolerates_missing_loader_image(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(data=_canonical_data("schema")), {"schema"})
        results = pin_service("schema", "847")
        assert [name for name, _ in results] == ["schema"]
        assert calls["description"] == "Pin schema to MR !847 image"
        assert calls["reconciled"] == ["schema"]

    def test_schema_pin_propagates_loader_lookup_failure(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"schema-load": _pin(mr="1")}))
        calls = self._wire(monkeypatch, lock, {"schema", "schema-load"})

        def fake_resolve(service, mr_iid, mr_snapshot=None):
            if service == "schema-load":
                raise PinError("MR 847: unexpected GitLab API response")
            return (
                ResolvedImage(service, f"repo/{service}-fix-x", "b" * 40, "now", "sha256:new"),
                mr_snapshot,
            )

        monkeypatch.setattr(pins, "resolve_mr_image", fake_resolve)
        with pytest.raises(PinError, match="unexpected GitLab API response"):
            pin_service("schema", "847")
        assert calls["patch"] is None

    def test_schema_repin_releases_stale_loader_pin(self, monkeypatch):
        stale = _pin(mr="1", canonical_repository="repo/loader-master", canonical_tag="c" * 40)
        lock = _lock(pins_annotation=encode_pins({"schema": _pin(mr="1"), "schema-load": stale}))
        calls = self._wire(monkeypatch, lock, {"schema"})
        results = pin_service("schema", "2")

        assert [name for name, _ in results] == ["schema"]
        data, saved = calls["patch"]
        assert data["SCHEMA_LOAD_IMAGE_REPOSITORY"] == "repo/loader-master"
        assert data["SCHEMA_LOAD_IMAGE_TAG"] == "c" * 40
        assert "schema-load" not in saved
        assert calls["reconciled"] == ["schema", "schema-load"]

    def test_schema_repin_directs_invalid_loader_through_reset_and_refresh(self, monkeypatch):
        stale = _pin(mr="1", canonical_repository="", canonical_tag="")
        lock = _lock(pins_annotation=encode_pins({"schema": _pin(mr="1"), "schema-load": stale}))
        self._wire(monkeypatch, lock, {"schema"})
        with pytest.raises(PinError, match="service reset schema.*reconcile --refresh-images"):
            pin_service("schema", "2")


class TestPinServicePostWriteRecheck:
    """A lifecycle run can set `maintenance` between the pre-write refusal and
    the lock's CAS write; the post-write recheck closes that window."""

    def _wire(self, monkeypatch, lock, resolved_names):
        calls = _wire_lock(monkeypatch, lock)
        mr = {"source_branch": "fix/x", "sha": "b" * 40}
        monkeypatch.setattr(pins, "fetch_merge_request", lambda project_id, mr_iid: mr)

        def fake_resolve(service, mr_iid, mr_snapshot=None):
            assert mr_snapshot is mr
            if service not in resolved_names:
                raise MissingPipelineImageError(f"{service}: no image")
            return (
                ResolvedImage(service, f"repo/{service}-fix-x", "b" * 40, "now", "sha256:new"),
                mr_snapshot,
            )

        monkeypatch.setattr(pins, "resolve_mr_image", fake_resolve)
        return calls

    def test_reverts_pin_when_maintenance_appears_after_write(self, monkeypatch):
        lock = _lock(data=_canonical_data("storage"))
        calls = self._wire(monkeypatch, lock, {"storage"})
        records = iter([_deploy_record(), _deploy_record(maintenance=True)])
        monkeypatch.setattr(pins, "read_deploy_record", lambda required=False: next(records))

        with pytest.raises(PinError, match="maintenance"):
            pin_service("storage", "42")

        data, saved = calls["patch"]
        assert data["STORAGE_IMAGE_TAG"] == "c" * 40
        assert data["STORAGE_IMAGE_REPOSITORY"] == "repo/storage-master"
        assert "storage" not in saved
        assert calls["reconciled"] is None

    def test_leaves_a_replaced_pin_alone(self, monkeypatch):
        """If another `spi service pin` lands on the same service in the
        post-write window, that newer pin is left standing rather than
        reverted."""
        lock = _lock(data=_canonical_data("storage"))
        calls = self._wire(monkeypatch, lock, {"storage"})

        def fake_read_deploy_record(required=False):
            if calls["patch"] is None:
                return _deploy_record()
            current = calls["box"][0]
            other = _pin(mr="99", applied_at="2026-08-25T01:00:00Z")
            calls["box"][0] = {
                "metadata": {
                    "resourceVersion": str(int(current["metadata"]["resourceVersion"]) + 1),
                    "annotations": {pins.PINS_ANNOTATION: encode_pins({"storage": other})},
                },
                "data": dict(current["data"]),
            }
            return _deploy_record(maintenance=True)

        monkeypatch.setattr(pins, "read_deploy_record", fake_read_deploy_record)

        with pytest.raises(PinError, match="maintenance"):
            pin_service("storage", "42")

        final_pins = decode_pins(calls["box"][0])
        assert final_pins["storage"].mr == "99"
        assert calls["reconciled"] is None

    def test_restores_the_replaced_pin_rather_than_canonical(self, monkeypatch):
        """Re-pinning a pinned service and then losing the maintenance race
        must put the earlier pin back, not strip the service to canonical:
        the rollback undoes this call, not the experiment it interrupted."""
        earlier = _pin(
            mr="7",
            repository="repo/storage-fix-earlier",
            tag="e" * 40,
            created_at="earlier",
            digest="sha256:earlier",
        )
        lock = _lock(
            data=_pinned_data("storage", earlier),
            pins_annotation=encode_pins({"storage": earlier}),
        )
        calls = self._wire(monkeypatch, lock, {"storage"})
        records = iter([_deploy_record(), _deploy_record(maintenance=True)])
        monkeypatch.setattr(pins, "read_deploy_record", lambda required=False: next(records))

        with pytest.raises(PinError, match="maintenance"):
            pin_service("storage", "42")

        data, saved = calls["patch"]
        assert saved["storage"] == earlier
        assert data == _pinned_data("storage", earlier)
        assert calls["reconciled"] is None

    def test_restoring_a_legacy_pin_keeps_its_live_digest(self, monkeypatch):
        """A pin encoded before `created_at` and `digest` joined the schema
        decodes with those fields empty while the lock still carries them.
        Re-deriving the entry from the annotation would blank the digest keys
        ADR-017 makes load-bearing, so the rollback replays the lock data."""
        legacy = _pin(mr="7", repository="repo/storage-fix-earlier", tag="e" * 40)
        assert (legacy.created_at, legacy.digest) == ("", "")
        live = _pinned_data("storage", legacy)
        live["STORAGE_IMAGE_DIGEST"] = "sha256:live"
        live["STORAGE_IMAGE_CREATED_AT"] = "then"
        live["STORAGE_IMAGE_REF"] = "repo/storage-fix-earlier@sha256:live"
        lock = _lock(data=live, pins_annotation=encode_pins({"storage": legacy}))
        calls = self._wire(monkeypatch, lock, {"storage"})
        records = iter([_deploy_record(), _deploy_record(maintenance=True)])
        monkeypatch.setattr(pins, "read_deploy_record", lambda required=False: next(records))

        with pytest.raises(PinError, match="maintenance"):
            pin_service("storage", "42")

        data, _ = calls["patch"]
        assert data["STORAGE_IMAGE_DIGEST"] == "sha256:live"
        assert data["STORAGE_IMAGE_CREATED_AT"] == "then"
        assert data["STORAGE_IMAGE_REF"] == "repo/storage-fix-earlier@sha256:live"

    def test_reinstates_a_loader_pin_released_by_the_same_write(self, monkeypatch):
        """Pinning schema from an MR that never rebuilt the loader releases a
        stale loader pin; the rollback owes that release an undo too."""
        loader = _pin(mr="7", repository="repo/schema-load-fix-earlier", tag="e" * 40)
        lock = _lock(
            data={**_canonical_data("schema"), **_pinned_data("schema-load", loader)},
            pins_annotation=encode_pins({"schema-load": loader}),
        )
        calls = self._wire(monkeypatch, lock, {"schema"})
        records = iter([_deploy_record(), _deploy_record(maintenance=True)])
        monkeypatch.setattr(pins, "read_deploy_record", lambda required=False: next(records))

        with pytest.raises(PinError, match="maintenance"):
            pin_service("schema", "42")

        data, saved = calls["patch"]
        assert saved["schema-load"] == loader
        assert data["SCHEMA_LOAD_IMAGE_REPOSITORY"] == "repo/schema-load-fix-earlier"
        assert "schema" not in saved
        assert data["SCHEMA_IMAGE_REPOSITORY"] == "repo/schema-master"
        assert calls["reconciled"] is None

    def test_leaves_the_released_loader_alone_when_the_schema_pin_moved_on(self, monkeypatch):
        """The release is only undone together with the pin it accompanied.
        Reinstating the loader on its own evidence, after another call has
        taken over the schema pin, would pair that newer pin with a stale
        loader: the exact mismatch releasing the loader exists to prevent."""
        loader = _pin(mr="7", repository="repo/schema-load-fix-earlier", tag="e" * 40)
        lock = _lock(
            data={**_canonical_data("schema"), **_pinned_data("schema-load", loader)},
            pins_annotation=encode_pins({"schema-load": loader}),
        )
        calls = self._wire(monkeypatch, lock, {"schema"})

        def fake_read_deploy_record(required=False):
            if calls["patch"] is None:
                return _deploy_record()
            current = calls["box"][0]
            newer = _pin(mr="99", applied_at="2026-08-25T01:00:00Z")
            calls["box"][0] = {
                "metadata": {
                    "resourceVersion": str(int(current["metadata"]["resourceVersion"]) + 1),
                    "annotations": {pins.PINS_ANNOTATION: encode_pins({"schema": newer})},
                },
                "data": dict(current["data"]),
            }
            return _deploy_record(maintenance=True)

        monkeypatch.setattr(pins, "read_deploy_record", fake_read_deploy_record)

        with pytest.raises(PinError, match="nothing was reverted"):
            pin_service("schema", "42")

        final_pins = decode_pins(calls["box"][0])
        assert final_pins["schema"].mr == "99"
        assert "schema-load" not in final_pins
        assert calls["reconciled"] is None

    def test_reports_pin_still_live_when_revert_fails(self, monkeypatch):
        lock = _lock(data=_canonical_data("storage"))
        calls = self._wire(monkeypatch, lock, {"storage"})

        def fake_read_lock(required=True):
            if calls["patch"] is not None:
                raise PinError("cluster unreachable")
            return calls["box"][0]

        monkeypatch.setattr(pins, "read_lock", fake_read_lock)
        records = iter([_deploy_record(), _deploy_record(maintenance=True)])
        monkeypatch.setattr(pins, "read_deploy_record", lambda required=False: next(records))

        with pytest.raises(PinError, match="spi service reset storage"):
            pin_service("storage", "42")

    def test_happy_path_still_reconciles_when_maintenance_never_appears(self, monkeypatch):
        lock = _lock(data=_canonical_data("storage"))
        calls = self._wire(monkeypatch, lock, {"storage"})
        monkeypatch.setattr(pins, "read_deploy_record", lambda required=False: _deploy_record())

        results = pin_service("storage", "42")

        assert [name for name, _ in results] == ["storage"]
        assert calls["reconciled"] == ["storage"]


class TestResetService:
    def test_restores_canonical_and_drops_pin(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"storage": _pin()}))
        calls = _wire_lock(monkeypatch, lock)

        result = reset_service("storage")
        assert result == ResetResult(restored=("storage",), refresh_required=())
        data, saved = calls["patch"]
        assert data["STORAGE_IMAGE_TAG"] == "c" * 40
        assert data["STORAGE_IMAGE_REPOSITORY"] == "registry/schema-service-master"
        assert saved == {}

    def test_schema_reset_releases_the_loader_too(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"schema": _pin(), "schema-load": _pin()}))
        calls = _wire_lock(monkeypatch, lock)

        result = reset_service("schema")
        assert result == ResetResult(
            restored=("schema", "schema-load"),
            refresh_required=(),
        )
        _data, saved = calls["patch"]
        assert saved == {}

    def test_schema_reset_drops_invalid_loader_pin_for_refresh(self, monkeypatch):
        loader = _pin(canonical_repository="", canonical_tag="")
        lock = _lock(pins_annotation=encode_pins({"schema": _pin(), "schema-load": loader}))
        calls = _wire_lock(monkeypatch, lock)

        result = reset_service("schema")

        assert result == ResetResult(
            restored=("schema",),
            refresh_required=("schema-load",),
        )
        data, saved = calls["patch"]
        assert saved == {}
        assert data["SCHEMA_IMAGE_TAG"] == "c" * 40
        assert "SCHEMA_LOAD_IMAGE_TAG" not in data
        assert calls["reconciled"] == ["schema"]

    def test_reset_drops_invalid_pin_without_reconciling_stale_image(self, monkeypatch):
        invalid = _pin(canonical_repository="", canonical_tag="")
        lock = _lock(pins_annotation=encode_pins({"storage": invalid}))
        calls = _wire_lock(monkeypatch, lock)
        monkeypatch.setattr(
            pins,
            "reconcile_consumers",
            lambda names: pytest.fail("stale image must not be reconciled"),
        )

        result = reset_service("storage")

        assert result == ResetResult(restored=(), refresh_required=("storage",))
        data, saved = calls["patch"]
        assert data == {}
        assert saved == {}

    def test_schema_load_reset_does_not_duplicate_target(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"schema-load": _pin()}))
        calls = _wire_lock(monkeypatch, lock)

        result = reset_service("schema-load")
        assert result == ResetResult(restored=("schema-load",), refresh_required=())
        _data, saved = calls["patch"]
        assert saved == {}

    def test_unpinned_service_errors(self, monkeypatch):
        monkeypatch.setattr(pins, "read_lock", lambda required=True: _lock())
        with pytest.raises(PinError, match="not pinned"):
            reset_service("storage")

    def test_missing_lock_reports_the_configmap(self, monkeypatch):
        monkeypatch.setattr(pins, "read_lock", lambda required=True: None)
        with pytest.raises(PinError, match="osdu-image-lock"):
            reset_service("storage")


class TestServiceResetCli:
    def test_invalid_pin_recovery_requires_refresh(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
        monkeypatch.setattr(
            cli,
            "reset_service",
            lambda service, if_run="": ResetResult(
                restored=("schema",), refresh_required=("schema-load",)
            ),
        )

        result = CliRunner().invoke(cli.app, ["service", "reset", "schema"])

        assert result.exit_code == 0
        assert "schema restored to canonical image" in result.output
        assert "schema-load pin removed" in result.output
        assert "spi reconcile --refresh-images" in result.output


class TestRefreshSurvival:
    def test_render_overlays_pinned_entries_and_annotation(self):
        resolved = {
            "schema": ResolvedImage("schema", "repo/schema-master", "c" * 40, "then", "sha:old"),
            "storage": ResolvedImage("storage", "repo/storage-master", "d" * 40, "then", "sha:st"),
        }
        resolved.update(
            {
                name: ResolvedImage(name, f"repo/{name}-master", "e" * 40, "", "")
                for name in pins.IMAGE_REGISTRY
                if name not in resolved
            }
        )
        active = {"schema": _pin()}
        rendered = pins.render_lock_with_pins(resolved, "master", active)
        assert f"SCHEMA_IMAGE_TAG: {'a' * 40!r}".replace("'", '"') in rendered
        assert "registry/schema-service-fix-upgrade-core-lib" in rendered
        assert pins.PINS_ANNOTATION in rendered
        # Unpinned services keep their freshly resolved entries.
        assert f"STORAGE_IMAGE_TAG: {'d' * 40!r}".replace("'", '"') in rendered

    def test_render_preserves_pinned_digest_and_created_at(self):
        """A pin's own digest/created-at have to survive the overlay: losing
        them would leave the rendered lock without a ref-substitution source
        for the pinned service, even though the pin itself is preserved."""
        pin = _pin(
            repository="registry/schema-service-fix-x",
            tag="b" * 40,
            created_at="2026-08-26T00:00:00Z",
            digest="sha256:pinned",
        )
        resolved = {
            name: ResolvedImage(name, f"repo/{name}-master", "e" * 40, "then", "sha256:canon")
            for name in pins.IMAGE_REGISTRY
        }
        rendered = pins.render_lock_with_pins(resolved, "master", {"schema": pin})

        assert 'SCHEMA_IMAGE_CREATED_AT: "2026-08-26T00:00:00Z"' in rendered
        assert 'SCHEMA_IMAGE_DIGEST: "sha256:pinned"' in rendered
        assert 'SCHEMA_IMAGE_REF: "registry/schema-service-fix-x@sha256:pinned"' in rendered

    def test_render_without_pins_omits_annotation(self):
        resolved = {
            name: ResolvedImage(name, f"repo/{name}-master", "e" * 40, "", "")
            for name in pins.IMAGE_REGISTRY
        }
        rendered = pins.render_lock_with_pins(resolved, "master", {})
        assert pins.PINS_ANNOTATION not in rendered

    def test_live_pins_empty_when_lock_absent(self, monkeypatch):
        monkeypatch.setattr(pins, "read_lock", lambda required=True: None)
        assert pins.live_pins() == {}

    def test_live_pins_raise_on_read_failure(self, monkeypatch):
        def boom(required=True):
            raise PinError("could not read lock")

        monkeypatch.setattr(pins, "read_lock", boom)
        with pytest.raises(PinError):
            pins.live_pins()

    def test_decode_pins_without_created_at_or_digest_still_decodes(self):
        """A pin encoded before ``created_at``/``digest`` existed on
        ``ServicePin`` has to keep decoding: this scope adds no new required
        fields to the annotation format."""
        legacy_fields = dict(
            mr="1",
            branch="fix/x",
            repository="registry/schema-service-fix-x",
            tag="a" * 40,
            canonical_repository="registry/schema-service-master",
            canonical_tag="c" * 40,
            canonical_created_at="then",
            canonical_digest="sha256:old",
            applied_at="2026-08-20T00:00:00Z",
        )
        legacy_annotation = json.dumps({"schema": legacy_fields})
        decoded = decode_pins(_lock(pins_annotation=legacy_annotation))
        assert decoded["schema"].created_at == ""
        assert decoded["schema"].digest == ""
        assert decoded["schema"].mr == "1"


class TestApplyImageLock:
    def _resolved(self):
        return {
            name: ResolvedImage(name, f"repo/{name}-master", "e" * 40, "then", "sha256:canon")
            for name in pins.IMAGE_REGISTRY
        }

    def test_creates_lock_when_absent(self, monkeypatch):
        created = {}

        def fake_run_process(cmd, input=None, **kwargs):
            assert cmd[:3] == ["kubectl", "create", "-f"]
            assert input is not None
            document = json.loads(input)
            created["document"] = document
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(document), stderr="")

        monkeypatch.setattr(pins, "read_lock", lambda required=True: None)
        monkeypatch.setattr(pins, "run_process", fake_run_process)

        active_pins = pins.apply_image_lock(self._resolved(), "master")

        assert active_pins == {}
        assert created["document"]["data"]["PARTITION_IMAGE_TAG"] == "e" * 40
        assert created["document"]["metadata"]["name"] == "osdu-image-lock"

    def test_preserves_active_pin_on_refresh(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"storage": _pin()}))
        calls = _wire_lock(monkeypatch, lock)

        active_pins = pins.apply_image_lock(self._resolved(), "master")

        assert set(active_pins) == {"storage"}
        data, saved = calls["patch"]
        assert data["STORAGE_IMAGE_TAG"] == _pin().tag
        assert data["STORAGE_IMAGE_REPOSITORY"] == _pin().repository
        assert saved["storage"].mr == _pin().mr
        # Unpinned services get the freshly resolved canonical entries.
        assert data["PARTITION_IMAGE_TAG"] == "e" * 40

    def test_recomputes_pins_from_fresh_lock_on_retry(self, monkeypatch):
        """A pin applied by a concurrent `spi service pin` between the read
        and the patch has to survive a `spi up --refresh-images` refresh
        racing it, not just an unrelated key."""
        lock = _lock(pins_annotation=encode_pins({"storage": _pin()}))
        calls = _wire_lock(monkeypatch, lock, conflicts=1)

        pins.apply_image_lock(self._resolved(), "master")

        assert calls["attempts"] == 2
        _data, saved = calls["patch"]
        assert set(saved) == {"storage", "_concurrent"}


class TestApplySchemaLoadBackfill:
    def test_returns_false_when_lock_absent(self, monkeypatch):
        monkeypatch.setattr(pins, "read_lock", lambda required=True: None)
        assert pins.apply_schema_load_backfill("master") is False

    def test_returns_false_when_already_backfilled(self, monkeypatch):
        data = _canonical_data("schema")
        data.update(_canonical_data("schema-load"))
        data["SCHEMA_LOAD_IMAGE_REF"] = "repo/schema-load-master@sha256:old"
        lock = _lock(data=data)
        monkeypatch.setattr(pins, "read_lock", lambda required=True: lock)

        def fail_run_command(*args, **kwargs):
            pytest.fail("a lock that already carries schema-load must not be patched")

        monkeypatch.setattr(pins, "run_command", fail_run_command)
        assert pins.apply_schema_load_backfill("master") is False

    def test_backfills_legacy_lock(self, monkeypatch):
        lock = _lock(data=_canonical_data("schema"))
        calls = _wire_lock(monkeypatch, lock)
        monkeypatch.setattr(
            pins,
            "schema_load_lock_patch",
            lambda lock_data, branch=None: {
                "SCHEMA_LOAD_IMAGE_REPOSITORY": "repo/schema-load-master",
                "SCHEMA_LOAD_IMAGE_TAG": lock_data["SCHEMA_IMAGE_TAG"],
                "SCHEMA_LOAD_IMAGE_REF": f"repo/schema-load-master:{lock_data['SCHEMA_IMAGE_TAG']}",
            },
        )

        assert pins.apply_schema_load_backfill("master") is True
        data, _saved = calls["patch"]
        assert data["SCHEMA_LOAD_IMAGE_REPOSITORY"] == "repo/schema-load-master"
        assert data["SCHEMA_LOAD_IMAGE_TAG"] == "c" * 40


class TestLockMutationConflicts:
    """Bounded resourceVersion compare-and-retry under concurrent writers."""

    def test_pin_service_retries_through_a_concurrent_pin(self, monkeypatch):
        lock = _lock(data=_canonical_data("storage"))
        mr = {"source_branch": "fix/x", "sha": "b" * 40}
        calls = _wire_lock(monkeypatch, lock, conflicts=2)
        monkeypatch.setattr(pins, "fetch_merge_request", lambda project_id, mr_iid: mr)
        monkeypatch.setattr(
            pins,
            "resolve_mr_image",
            lambda service, mr_iid, mr_snapshot=None: (
                ResolvedImage(service, f"repo/{service}-fix-x", "b" * 40, "now", "sha256:new"),
                mr_snapshot,
            ),
        )

        results = pin_service("storage", "42")

        assert [name for name, _ in results] == ["storage"]
        assert calls["attempts"] == 3
        _data, saved = calls["patch"]
        # The concurrent writer's unrelated pin survives the retry alongside
        # the newly applied one.
        assert set(saved) == {"storage", "_concurrent"}

    def test_reset_service_retries_and_preserves_concurrent_pin(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"storage": _pin()}))
        calls = _wire_lock(monkeypatch, lock, conflicts=1)

        result = reset_service("storage")

        assert result == ResetResult(restored=("storage",), refresh_required=())
        assert calls["attempts"] == 2
        _data, saved = calls["patch"]
        assert set(saved) == {"_concurrent"}

    def test_exhausting_retries_raises_lock_conflict_error(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"storage": _pin()}))
        calls = _wire_lock(monkeypatch, lock, conflicts=LOCK_MUTATION_MAX_ATTEMPTS)

        with pytest.raises(LockConflictError, match="concurrent writers"):
            reset_service("storage")
        assert calls["patch"] is None

    def test_terminal_failure_is_not_retried(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"storage": _pin()}))
        attempts = {"n": 0}

        def fake_read_lock(required=True):
            return lock

        def fake_run_command(cmd, description=None, check=True, **kwargs):
            attempts["n"] += 1
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="Forbidden: user cannot patch configmaps"
            )

        monkeypatch.setattr(pins, "read_lock", fake_read_lock)
        monkeypatch.setattr(pins, "run_command", fake_run_command)
        monkeypatch.setattr(pins, "reconcile_consumers", lambda names: None)

        with pytest.raises(PinError, match="Forbidden"):
            reset_service("storage")
        assert attempts["n"] == 1

    def test_reconciles_in_dependency_order_and_blocks(self, monkeypatch):
        """services must settle before schema-load runs, and schema-load
        before reference re-seeds, mirroring the refresh sequence."""
        reconciled = []

        def fake_run(command, **kwargs):
            assert command[:3] == ["flux", "reconcile", "kustomization"]
            assert kwargs.get("check", True), "a failed stage must abort the sequence"
            reconciled.append(command[3])

        monkeypatch.setattr(pins, "run_command", fake_run)
        pins.reconcile_consumers(["unit", "schema-load", "storage"])
        assert reconciled == [
            "spi-osdu-services",
            "spi-osdu-schema-load",
            "spi-osdu-reference",
        ]

    def test_failed_stage_stops_the_sequence(self, monkeypatch):
        import typer

        reconciled = []

        def fake_run(command, **kwargs):
            reconciled.append(command[3])
            if command[3] == "spi-osdu-services":
                raise typer.Exit(code=1)

        monkeypatch.setattr(pins, "run_command", fake_run)
        with pytest.raises(typer.Exit):
            pins.reconcile_consumers(["storage", "schema-load"])
        assert reconciled == ["spi-osdu-services"]

    def test_only_affected_kustomizations_reconcile(self, monkeypatch):
        reconciled = []
        monkeypatch.setattr(
            pins, "run_command", lambda command, **kwargs: reconciled.append(command[3])
        )
        pins.reconcile_consumers(["storage"])
        assert reconciled == ["spi-osdu-services"]


class TestParseImageDigestRef:
    def test_splits_repository_and_digest(self):
        assert parse_image_digest_ref(_GHCR_IMAGE) == ("ghcr.io/azure/storage", _GHCR_DIGEST)

    def test_tag_reference_rejected_with_digest_guidance(self):
        with pytest.raises(ImageResolutionError, match="carries no digest"):
            parse_image_digest_ref("ghcr.io/azure/storage:sha-abc1234")

    def test_tag_alongside_digest_rejected(self):
        with pytest.raises(ImageResolutionError, match="alongside the digest"):
            parse_image_digest_ref(f"ghcr.io/azure/storage:v1@{_GHCR_DIGEST}")

    def test_malformed_digest_rejected(self):
        with pytest.raises(ImageResolutionError, match="not a sha256 manifest digest"):
            parse_image_digest_ref("ghcr.io/azure/storage@sha256:short")

    def test_missing_repository_path_rejected(self):
        with pytest.raises(ImageResolutionError, match="missing repository path"):
            parse_image_digest_ref(f"storage@{_GHCR_DIGEST}")

    def test_ghcr_owner_allow_list(self):
        require_ghcr_repository("ghcr.io/azure/storage")
        require_ghcr_repository("ghcr.io/Azure/storage")
        with pytest.raises(ImageResolutionError, match="allow-listed"):
            require_ghcr_repository("ghcr.io/evil/storage")
        with pytest.raises(ImageResolutionError, match="allow-listed"):
            require_ghcr_repository("docker.io/azure/storage")


class TestPinServiceImage:
    def _wire(self, monkeypatch, lock, conflicts=0, manifest_ok=True):
        calls = _wire_lock(monkeypatch, lock, conflicts=conflicts)
        calls["manifest_checks"] = []

        def fake_manifest(repository, digest):
            calls["manifest_checks"].append((repository, digest))
            if not manifest_ok:
                raise ImageResolutionError("manifest missing")

        monkeypatch.setattr(pins, "resolve_ghcr_manifest", fake_manifest)
        return calls

    def test_unknown_service_rejected(self):
        with pytest.raises(PinError, match="Unknown service"):
            pin_service_image("nope", _GHCR_IMAGE)

    def test_schema_load_direct_pin_rejected(self):
        with pytest.raises(PinError, match="cannot be pinned directly"):
            pin_service_image("schema-load", _GHCR_IMAGE)

    def test_tag_reference_rejected(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(data=_canonical_data("storage")))
        with pytest.raises(PinError, match="carries no digest"):
            pin_service_image("storage", "ghcr.io/azure/storage:sha-abc1234")
        assert calls["manifest_checks"] == []
        assert calls["patch"] is None

    def test_disallowed_owner_rejected(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(data=_canonical_data("storage")))
        with pytest.raises(PinError, match="allow-listed"):
            pin_service_image("storage", f"ghcr.io/evil/storage@{_GHCR_DIGEST}")
        assert calls["patch"] is None

    def test_ephemeral_requires_run_id(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(data=_canonical_data("storage")))
        with pytest.raises(PinError, match="requires --run-id"):
            pin_service_image("storage", _GHCR_IMAGE, ephemeral=True)
        assert calls["patch"] is None

    def test_run_id_must_be_numeric(self, monkeypatch):
        self._wire(monkeypatch, _lock(data=_canonical_data("storage")))
        with pytest.raises(PinError, match="numeric"):
            pin_service_image("storage", _GHCR_IMAGE, ephemeral=True, run_id="abc")

    def test_source_repo_shape_enforced(self, monkeypatch):
        self._wire(monkeypatch, _lock(data=_canonical_data("storage")))
        with pytest.raises(PinError, match="org.*repo"):
            pin_service_image(
                "storage", _GHCR_IMAGE, ephemeral=True, run_id="1", source_repo="not a repo"
            )

    def test_pin_writes_digest_entry_and_provenance(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(data=_canonical_data("storage")))

        pin = pin_service_image(
            "storage",
            _GHCR_IMAGE,
            ephemeral=True,
            run_id="1234",
            source_repo="Azure/osdu-spi-storage",
            source_sha="b" * 40,
            source_run_url="https://github.com/Azure/osdu-spi-storage/actions/runs/1234",
        )

        assert calls["manifest_checks"] == [("ghcr.io/azure/storage", _GHCR_DIGEST)]
        data, saved = calls["patch"]
        assert data["STORAGE_IMAGE_TAG"] == ""
        assert data["STORAGE_IMAGE_DIGEST"] == _GHCR_DIGEST
        assert data["STORAGE_IMAGE_REF"] == _GHCR_IMAGE
        assert data["STORAGE_IMAGE"] == _GHCR_IMAGE
        assert saved["storage"] == pin
        assert pin.origin == "github"
        assert pin.ephemeral is True
        assert pin.run_id == "1234"
        assert pin.source_repo == "Azure/osdu-spi-storage"
        assert pin.canonical_repository == "repo/storage-master"
        assert pin.canonical_tag == "c" * 40
        assert calls["reconciled"] == ["storage"]

    def test_operator_pin_carries_no_ephemeral_marker(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(data=_canonical_data("storage")))
        pin = pin_service_image("storage", _GHCR_IMAGE)
        _, saved = calls["patch"]
        assert saved["storage"] == pin
        assert pin.ephemeral is False
        assert pin.run_id == ""

    def test_repin_keeps_original_canonical(self, monkeypatch):
        existing = _pin(mr="1", canonical_repository="repo/storage-master")
        lock = _lock(
            data={"STORAGE_IMAGE_REPOSITORY": "repo/storage-fix-x"},
            pins_annotation=encode_pins({"storage": existing}),
        )
        calls = self._wire(monkeypatch, lock)
        pin_service_image("storage", _GHCR_IMAGE)

        _, saved = calls["patch"]
        assert saved["storage"].canonical_repository == "repo/storage-master"
        assert saved["storage"].digest == _GHCR_DIGEST

    def test_first_pin_rejects_missing_canonical_lock_data(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock())
        with pytest.raises(PinError, match="refresh-images"):
            pin_service_image("storage", _GHCR_IMAGE)
        assert calls["patch"] is None

    def test_refused_while_maintenance_set_before_manifest_check(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(data=_canonical_data("storage")))
        monkeypatch.setattr(
            pins,
            "read_deploy_record",
            lambda required=False: _deploy_record(maintenance=True),
        )

        with pytest.raises(PinError, match="maintenance"):
            pin_service_image("storage", _GHCR_IMAGE)

        assert calls["manifest_checks"] == []
        assert calls["patch"] is None

    def test_manifest_failure_aborts_before_lock_write(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(data=_canonical_data("storage")), manifest_ok=False)
        with pytest.raises(PinError, match="manifest missing"):
            pin_service_image("storage", _GHCR_IMAGE)
        assert calls["patch"] is None

    def test_post_write_maintenance_reverts_the_pin(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(data=_canonical_data("storage")))
        records = iter([_deploy_record(), _deploy_record(maintenance=True)])
        monkeypatch.setattr(pins, "read_deploy_record", lambda required=False: next(records))

        with pytest.raises(PinError, match="maintenance"):
            pin_service_image("storage", _GHCR_IMAGE)

        data, saved = calls["patch"]
        assert data["STORAGE_IMAGE_TAG"] == "c" * 40
        assert data["STORAGE_IMAGE_REPOSITORY"] == "repo/storage-master"
        assert "storage" not in saved
        assert calls["reconciled"] is None

    def test_retries_through_a_concurrent_pin(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(data=_canonical_data("storage")), conflicts=1)

        pin_service_image("storage", _GHCR_IMAGE)

        assert calls["attempts"] == 2
        _, saved = calls["patch"]
        assert set(saved) == {"storage", "_concurrent"}


def _deployment_json(
    image,
    name="osdu-storage",
    container=None,
    generation=1,
    observed=1,
    replicas=1,
    updated=1,
    available=1,
    total=1,
):
    return {
        "metadata": {"name": name, "generation": generation},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
            "template": {"spec": {"containers": [{"name": container or name, "image": image}]}},
        },
        "status": {
            "observedGeneration": observed,
            "updatedReplicas": updated,
            "availableReplicas": available,
            "replicas": total,
        },
    }


def _pod_json(image_id, name="osdu-storage-abc12", container="osdu-storage", phase="Running"):
    return {
        "metadata": {"name": name},
        "status": {
            "phase": phase,
            "containerStatuses": [{"name": container, "imageID": image_id}],
        },
    }


class TestVerifyServiceImage:
    def _wire(self, monkeypatch, deployment=None, pods=(), lock=None):
        calls = {"reads": []}

        def fake_run_process(cmd, **kwargs):
            assert cmd[0] == "kubectl"
            calls["reads"].append(cmd)
            if cmd[1:3] == ["get", "deployment"]:
                out = json.dumps(deployment) if deployment is not None else ""
                return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
            if cmd[1:3] == ["get", "pods"]:
                out = json.dumps({"items": list(pods)})
                return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
            raise AssertionError(f"unexpected kubectl read: {cmd}")

        monkeypatch.setattr(pins, "run_process", fake_run_process)
        monkeypatch.setattr(pins, "read_lock", lambda required=True: lock or _lock())
        return calls

    def test_verified_when_template_and_pod_carry_digest(self, monkeypatch):
        calls = self._wire(
            monkeypatch,
            deployment=_deployment_json(_GHCR_IMAGE),
            pods=[_pod_json(_GHCR_IMAGE)],
        )

        result = verify_service_image("storage", _GHCR_IMAGE)

        assert result.deployment == "osdu-storage"
        assert result.container == "osdu-storage"
        assert result.pod == "osdu-storage-abc12"
        assert _GHCR_DIGEST in result.image_id
        # Default names follow the Flux release convention osdu-<service>.
        assert calls["reads"][0][1:6] == ["get", "deployment", "osdu-storage", "-n", "osdu"]

    def test_deployment_and_container_overrides(self, monkeypatch):
        calls = self._wire(
            monkeypatch,
            deployment=_deployment_json(_GHCR_IMAGE, name="storage-svc", container="app"),
            pods=[_pod_json(_GHCR_IMAGE, container="app")],
        )

        result = verify_service_image(
            "storage", _GHCR_IMAGE, deployment="storage-svc", container="app"
        )

        assert result.deployment == "storage-svc"
        assert result.container == "app"
        assert calls["reads"][0][3] == "storage-svc"

    def test_missing_deployment_is_typed(self, monkeypatch):
        self._wire(monkeypatch, deployment=None)
        with pytest.raises(VerifyError, match="not found") as excinfo:
            verify_service_image("storage", _GHCR_IMAGE)
        assert excinfo.value.code == "deployment_not_found"

    def test_template_mismatch_names_the_colliding_run(self, monkeypatch):
        other = _image_pin(
            digest="sha256:" + "e" * 64,
            run_id="9999",
            source_run_url="https://github.com/Azure/osdu-spi-storage/actions/runs/9999",
        )
        self._wire(
            monkeypatch,
            deployment=_deployment_json("ghcr.io/azure/storage@sha256:" + "e" * 64),
            lock=_lock(pins_annotation=encode_pins({"storage": other})),
        )

        with pytest.raises(VerifyError, match="run 9999") as excinfo:
            verify_service_image("storage", _GHCR_IMAGE)
        assert excinfo.value.code == "template_mismatch"

    def test_rollout_incomplete_is_typed(self, monkeypatch):
        self._wire(
            monkeypatch,
            deployment=_deployment_json(_GHCR_IMAGE, replicas=2, updated=1, available=1, total=2),
        )
        with pytest.raises(VerifyError, match="rollout") as excinfo:
            verify_service_image("storage", _GHCR_IMAGE)
        assert excinfo.value.code == "rollout_incomplete"

    def test_no_running_pod_is_typed(self, monkeypatch):
        self._wire(
            monkeypatch,
            deployment=_deployment_json(_GHCR_IMAGE),
            pods=[_pod_json(_GHCR_IMAGE, phase="Pending")],
        )
        with pytest.raises(VerifyError, match="No running pods") as excinfo:
            verify_service_image("storage", _GHCR_IMAGE)
        assert excinfo.value.code == "pod_not_running"

    def test_pod_digest_mismatch_is_typed(self, monkeypatch):
        stale = "ghcr.io/azure/storage@sha256:" + "e" * 64
        self._wire(
            monkeypatch,
            deployment=_deployment_json(_GHCR_IMAGE),
            pods=[_pod_json(stale)],
        )
        with pytest.raises(VerifyError, match="imageID") as excinfo:
            verify_service_image("storage", _GHCR_IMAGE)
        assert excinfo.value.code == "pod_mismatch"

    def test_tag_reference_rejected(self, monkeypatch):
        self._wire(monkeypatch)
        with pytest.raises(PinError, match="carries no digest"):
            verify_service_image("storage", "ghcr.io/azure/storage:sha-abc1234")

    def test_unreachable_cluster_is_not_a_typed_failure(self, monkeypatch):
        def fake_run_process(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="connection refused")

        monkeypatch.setattr(pins, "run_process", fake_run_process)
        with pytest.raises(PinError, match="connection refused") as excinfo:
            verify_service_image("storage", _GHCR_IMAGE)
        assert not isinstance(excinfo.value, VerifyError)


class TestResetIfRun:
    def test_matching_run_restores_canonical(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"storage": _image_pin(run_id="1234")}))
        calls = _wire_lock(monkeypatch, lock)

        result = reset_service("storage", if_run="1234")

        assert result == ResetResult(restored=("storage",), refresh_required=())
        data, saved = calls["patch"]
        assert data["STORAGE_IMAGE_TAG"] == "c" * 40
        assert data["STORAGE_IMAGE_REPOSITORY"] == "repo/storage-master"
        assert saved == {}

    def test_non_matching_run_is_a_typed_no_op(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"storage": _image_pin(run_id="9999")}))
        calls = _wire_lock(monkeypatch, lock)

        with pytest.raises(ResetRefusedError, match="run 9999") as excinfo:
            reset_service("storage", if_run="1234")

        assert excinfo.value.code == "run_mismatch"
        assert calls["patch"] is None
        assert decode_pins(calls["box"][0])["storage"].run_id == "9999"

    def test_operator_pin_is_refused_by_name(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"storage": _pin()}))
        calls = _wire_lock(monkeypatch, lock)

        with pytest.raises(ResetRefusedError, match="MR !847") as excinfo:
            reset_service("storage", if_run="1234")

        assert excinfo.value.code == "run_mismatch"
        assert calls["patch"] is None

    def test_unpinned_service_is_a_typed_no_op(self, monkeypatch):
        calls = _wire_lock(monkeypatch, _lock())

        with pytest.raises(ResetRefusedError, match="not pinned") as excinfo:
            reset_service("storage", if_run="1234")

        assert excinfo.value.code == "not_pinned"
        assert calls["patch"] is None

    def test_schema_if_run_reset_leaves_the_loader_alone(self, monkeypatch):
        loader = _pin()
        lock = _lock(
            pins_annotation=encode_pins(
                {
                    "schema": _image_pin(repository="ghcr.io/azure/schema", run_id="55"),
                    "schema-load": loader,
                }
            )
        )
        calls = _wire_lock(monkeypatch, lock)

        result = reset_service("schema", if_run="55")

        assert result == ResetResult(restored=("schema",), refresh_required=())
        _, saved = calls["patch"]
        assert saved == {"schema-load": loader}


class _FakeHttpResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestGithubRunStatus:
    def test_disallowed_source_repo_is_never_fetched(self, monkeypatch):
        def fail_urlopen(*args, **kwargs):
            pytest.fail("a repository outside the allow-list must not be fetched")

        monkeypatch.setattr(pins.urllib.request, "urlopen", fail_urlopen)
        assert pins._github_run_status("evil/osdu-spi-storage", "123") is None
        assert pins._github_run_status("Azure/other-repo", "123") is None
        assert pins._github_run_status("Azure/osdu-spi-storage", "not-a-number") is None

    def test_gone_run_reads_as_completed(self, monkeypatch):
        from email.message import Message

        def raise_404(req, timeout=15):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", Message(), None)

        monkeypatch.setattr(pins.urllib.request, "urlopen", raise_404)
        assert pins._github_run_status("Azure/osdu-spi-storage", "123") == "completed"

    def test_reports_the_run_state(self, monkeypatch):
        monkeypatch.setattr(
            pins.urllib.request,
            "urlopen",
            lambda req, timeout=15: _FakeHttpResponse({"status": "in_progress"}),
        )
        assert pins._github_run_status("Azure/osdu-spi-storage", "123") == "in_progress"

    def test_network_failure_reads_as_unreachable(self, monkeypatch):
        def raise_unreachable(req, timeout=15):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(pins.urllib.request, "urlopen", raise_unreachable)
        assert pins._github_run_status("Azure/osdu-spi-storage", "123") is None


class TestSweepStaleEphemeralPins:
    def _wire(self, monkeypatch, lock, run_states=None):
        calls = _wire_lock(monkeypatch, lock)
        calls["lookups"] = []
        states = run_states or {}

        def fake_run_status(source_repo, run_id):
            calls["lookups"].append((source_repo, run_id))
            return states.get(run_id)

        monkeypatch.setattr(pins, "_github_run_status", fake_run_status)
        return calls

    def test_no_ephemeral_pins_is_a_quiet_no_op(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"storage": _pin()}))
        calls = self._wire(monkeypatch, lock)

        assert sweep_stale_ephemeral_pins() == SweepResult((), (), ())
        assert calls["lookups"] == []
        assert calls["patch"] is None

    def test_terminal_run_is_swept_to_canonical(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"storage": _image_pin(run_id="1234")}))
        calls = self._wire(monkeypatch, lock, run_states={"1234": "completed"})

        result = sweep_stale_ephemeral_pins()

        assert result.swept == ("storage",)
        data, saved = calls["patch"]
        assert data["STORAGE_IMAGE_TAG"] == "c" * 40
        assert saved == {}
        assert calls["reconciled"] == ["storage"]

    def test_running_pin_is_kept(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"storage": _image_pin(run_id="1234")}))
        calls = self._wire(monkeypatch, lock, run_states={"1234": "in_progress"})

        result = sweep_stale_ephemeral_pins()

        assert result.swept == ()
        assert result.kept == (("storage", "run 1234 is in_progress"),)
        assert calls["patch"] is None

    def test_unreachable_state_defers_to_the_age_threshold(self, monkeypatch):
        young = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        old = (
            datetime.now(timezone.utc) - timedelta(hours=pins.STALE_EPHEMERAL_PIN_AGE_HOURS + 1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        lock = _lock(
            pins_annotation=encode_pins(
                {
                    "storage": _image_pin(run_id="1", applied_at=young),
                    "search": _image_pin(
                        repository="ghcr.io/azure/search",
                        run_id="2",
                        applied_at=old,
                        canonical_repository="repo/search-master",
                    ),
                }
            )
        )
        calls = self._wire(monkeypatch, lock)

        result = sweep_stale_ephemeral_pins()

        assert result.swept == ("search",)
        assert result.kept == (("storage", "run state unreachable and pin younger than threshold"),)
        _, saved = calls["patch"]
        assert set(saved) == {"storage"}

    def test_swept_pin_without_canonical_requires_refresh(self, monkeypatch):
        stale = _image_pin(run_id="1234", canonical_repository="", canonical_tag="")
        lock = _lock(pins_annotation=encode_pins({"storage": stale}))
        calls = self._wire(monkeypatch, lock, run_states={"1234": "completed"})
        monkeypatch.setattr(
            pins,
            "reconcile_consumers",
            lambda names: pytest.fail("no restored image, nothing to reconcile"),
        )

        result = sweep_stale_ephemeral_pins()

        assert result == SweepResult(swept=(), kept=(), refresh_required=("storage",))
        _, saved = calls["patch"]
        assert saved == {}

    def test_operator_pins_are_never_consulted(self, monkeypatch):
        lock = _lock(
            pins_annotation=encode_pins({"storage": _pin(), "search": _image_pin(run_id="7")})
        )
        calls = self._wire(monkeypatch, lock, run_states={"7": "in_progress"})

        sweep_stale_ephemeral_pins()

        assert calls["lookups"] == [("Azure/osdu-spi-storage", "7")]

    def test_repinned_service_survives_the_sweep_write(self, monkeypatch):
        """A pin replaced between the staleness check and the CAS write is a
        newer run's pin, not the stale one; it must be left standing."""
        stale = _image_pin(run_id="1234")
        lock = _lock(pins_annotation=encode_pins({"storage": stale}))
        calls = self._wire(monkeypatch, lock, run_states={"1234": "completed"})

        newer = _image_pin(run_id="5678", applied_at="2026-08-25T02:00:00Z")
        swapped = {"done": False}

        def read_then_swap(required=True):
            current = calls["box"][0]
            if not swapped["done"]:
                # After the staleness scan reads the stale pin, a newer run
                # re-pins the service before the sweep's CAS write.
                swapped["done"] = True
                calls["box"][0] = {
                    "metadata": {
                        "resourceVersion": str(int(current["metadata"]["resourceVersion"]) + 1),
                        "annotations": {pins.PINS_ANNOTATION: encode_pins({"storage": newer})},
                    },
                    "data": dict(current.get("data") or {}),
                }
                return current
            return calls["box"][0]

        monkeypatch.setattr(pins, "read_lock", read_then_swap)

        result = sweep_stale_ephemeral_pins()

        assert result.swept == ()
        final_pins = decode_pins(calls["box"][0])
        assert final_pins["storage"].run_id == "5678"


class TestServicePinCli:
    def test_mr_and_image_are_mutually_exclusive(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
        result = CliRunner().invoke(
            cli.app, ["service", "pin", "storage", "--mr", "1", "--image", _GHCR_IMAGE]
        )
        assert result.exit_code == 1
        assert "exactly one" in result.output

        result = CliRunner().invoke(cli.app, ["service", "pin", "storage"])
        assert result.exit_code == 1
        assert "exactly one" in result.output

    def test_provenance_flags_require_image_and_ephemeral(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
        result = CliRunner().invoke(
            cli.app, ["service", "pin", "storage", "--mr", "1", "--ephemeral"]
        )
        assert result.exit_code == 1
        assert "--image" in result.output

        result = CliRunner().invoke(
            cli.app,
            ["service", "pin", "storage", "--image", _GHCR_IMAGE, "--run-id", "1"],
        )
        assert result.exit_code == 1
        assert "--ephemeral" in result.output

    def test_image_pin_reports_digest_and_run(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
        captured = {}

        def fake_pin(service, image, **kwargs):
            captured.update(service=service, image=image, **kwargs)
            return _image_pin()

        monkeypatch.setattr(cli, "pin_service_image", fake_pin)

        result = CliRunner().invoke(
            cli.app,
            [
                "service",
                "pin",
                "storage",
                "--image",
                _GHCR_IMAGE,
                "--ephemeral",
                "--run-id",
                "1234",
                "--source-repo",
                "Azure/osdu-spi-storage",
                "--source-sha",
                "b" * 40,
            ],
        )

        assert result.exit_code == 0
        assert captured["ephemeral"] is True
        assert captured["run_id"] == "1234"
        assert "ephemeral, run 1234" in result.output


class TestServiceVerifyCli:
    def test_typed_failure_exits_two(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")

        def raise_verify(service, image, deployment=None, container=None):
            raise VerifyError("pod_mismatch", "no pod carries the digest")

        monkeypatch.setattr(cli, "verify_service_image", raise_verify)

        result = CliRunner().invoke(
            cli.app, ["service", "verify", "storage", "--image", _GHCR_IMAGE]
        )

        assert result.exit_code == 2
        assert "pod_mismatch" in result.output

    def test_unreachable_exits_one(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")

        def raise_pin_error(service, image, deployment=None, container=None):
            raise PinError("Could not read Deployment osdu-storage: connection refused")

        monkeypatch.setattr(cli, "verify_service_image", raise_pin_error)

        result = CliRunner().invoke(
            cli.app, ["service", "verify", "storage", "--image", _GHCR_IMAGE]
        )

        assert result.exit_code == 1

    def test_success_reports_the_pod(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
        monkeypatch.setattr(
            cli,
            "verify_service_image",
            lambda service, image, deployment=None, container=None: pins.VerifyResult(
                deployment="osdu-storage",
                container="osdu-storage",
                pod="osdu-storage-abc12",
                image_id=_GHCR_IMAGE,
            ),
        )

        result = CliRunner().invoke(
            cli.app, ["service", "verify", "storage", "--image", _GHCR_IMAGE]
        )

        assert result.exit_code == 0
        assert "osdu-storage-abc12" in result.output

    def test_json_outcome_is_the_final_stdout_line(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
        monkeypatch.setattr(
            cli,
            "verify_service_image",
            lambda service, image, deployment=None, container=None: pins.VerifyResult(
                deployment="osdu-storage",
                container="osdu-storage",
                pod="osdu-storage-abc12",
                image_id=_GHCR_IMAGE,
            ),
        )

        result = CliRunner().invoke(
            cli.app, ["service", "verify", "storage", "--image", _GHCR_IMAGE, "--json"]
        )

        assert result.exit_code == 0
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["outcome"] == "verified"
        assert payload["code"] is None
        assert payload["pod"] == "osdu-storage-abc12"
        assert payload["imageId"] == _GHCR_IMAGE
        assert payload["apiVersion"] == "spi.osdu.dev/v1"

    def test_json_failure_carries_the_typed_code(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")

        def raise_verify(service, image, deployment=None, container=None):
            raise VerifyError("template_mismatch", "template runs another digest")

        monkeypatch.setattr(cli, "verify_service_image", raise_verify)

        result = CliRunner().invoke(
            cli.app, ["service", "verify", "storage", "--image", _GHCR_IMAGE, "--json"]
        )

        assert result.exit_code == 2
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["outcome"] == "failed"
        assert payload["code"] == "template_mismatch"
        assert "another digest" in payload["detail"]


class TestServiceResetCliConditional:
    def test_refusal_exits_two(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")

        def raise_refused(service, if_run=""):
            raise ResetRefusedError("run_mismatch", "storage is pinned by run 9999")

        monkeypatch.setattr(cli, "reset_service", raise_refused)

        result = CliRunner().invoke(cli.app, ["service", "reset", "storage", "--if-run", "1234"])

        assert result.exit_code == 2
        assert "run_mismatch" in result.output

    def test_sweep_flags_must_pair(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
        result = CliRunner().invoke(cli.app, ["service", "reset", "--ephemeral"])
        assert result.exit_code == 1
        assert "--stale-only" in result.output

    def test_sweep_takes_no_service_argument(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
        result = CliRunner().invoke(
            cli.app, ["service", "reset", "storage", "--ephemeral", "--stale-only"]
        )
        assert result.exit_code == 1
        assert "no service argument" in result.output

    def test_reset_without_service_or_sweep_flags_is_rejected(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
        result = CliRunner().invoke(cli.app, ["service", "reset"])
        assert result.exit_code == 1
        assert "Provide a service name" in result.output

    def test_sweep_reports_each_outcome(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
        monkeypatch.setattr(
            cli,
            "sweep_stale_ephemeral_pins",
            lambda: SweepResult(
                swept=("storage",),
                kept=(("search", "run 7 is in_progress"),),
                refresh_required=("legal",),
            ),
        )

        result = CliRunner().invoke(cli.app, ["service", "reset", "--ephemeral", "--stale-only"])

        assert result.exit_code == 0
        assert "storage" in result.output
        assert "search kept" in result.output
        assert "spi reconcile --refresh-images" in result.output

    def test_json_refusal_carries_the_typed_code(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")

        def raise_refused(service, if_run=""):
            raise ResetRefusedError("run_mismatch", "storage is pinned by run 9999")

        monkeypatch.setattr(cli, "reset_service", raise_refused)

        result = CliRunner().invoke(
            cli.app, ["service", "reset", "storage", "--if-run", "1234", "--json"]
        )

        assert result.exit_code == 2
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["outcome"] == "refused"
        assert payload["code"] == "run_mismatch"
        assert "run 9999" in payload["detail"]

    def test_json_reset_reports_restored_services(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
        monkeypatch.setattr(
            cli,
            "reset_service",
            lambda service, if_run="": ResetResult(restored=("storage",), refresh_required=()),
        )

        result = CliRunner().invoke(
            cli.app, ["service", "reset", "storage", "--if-run", "1234", "--json"]
        )

        assert result.exit_code == 0
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["outcome"] == "reset"
        assert payload["code"] is None
        assert payload["restored"] == ["storage"]
        assert payload["refreshRequired"] == []

    def test_json_sweep_reports_each_bucket(self, monkeypatch):
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
        monkeypatch.setattr(
            cli,
            "sweep_stale_ephemeral_pins",
            lambda: SweepResult(
                swept=("storage",),
                kept=(("search", "run 7 is in_progress"),),
                refresh_required=("legal",),
            ),
        )

        result = CliRunner().invoke(
            cli.app, ["service", "reset", "--ephemeral", "--stale-only", "--json"]
        )

        assert result.exit_code == 0
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["outcome"] == "swept"
        assert payload["swept"] == ["storage"]
        assert payload["kept"] == [{"service": "search", "reason": "run 7 is in_progress"}]
        assert payload["refreshRequired"] == ["legal"]


class TestPinCodecProvenance:
    def test_ephemeral_provenance_round_trips(self):
        original = {"storage": _image_pin()}
        decoded = decode_pins(_lock(pins_annotation=encode_pins(original)))
        assert decoded == original
        assert decoded["storage"].ephemeral is True
        assert decoded["storage"].origin == "github"

    def test_annotation_without_provenance_reads_as_operator_pin(self):
        """A pin encoded before the ADR-031 fields existed decodes with them
        empty, which every consumer reads as a non-ephemeral operator pin."""
        fields = dict(
            mr="847",
            branch="fix/x",
            repository="registry/schema-service-fix-x",
            tag="a" * 40,
            canonical_repository="registry/schema-service-master",
            canonical_tag="c" * 40,
            canonical_created_at="then",
            canonical_digest="sha256:old",
            applied_at="2026-08-20T00:00:00Z",
            created_at="now",
            digest="sha256:new",
        )
        decoded = decode_pins(_lock(pins_annotation=json.dumps({"schema": fields})))
        assert decoded["schema"].ephemeral is False
        assert decoded["schema"].run_id == ""
        assert decoded["schema"].origin == ""

    def test_mr_pins_now_record_their_origin(self, monkeypatch):
        calls = _wire_lock(monkeypatch, _lock(data=_canonical_data("storage")))
        mr = {"source_branch": "fix/x", "sha": "b" * 40}
        monkeypatch.setattr(pins, "fetch_merge_request", lambda project_id, mr_iid: mr)
        monkeypatch.setattr(
            pins,
            "resolve_mr_image",
            lambda service, mr_iid, mr_snapshot=None: (
                ResolvedImage(service, f"repo/{service}-fix-x", "b" * 40, "now", "sha256:new"),
                mr_snapshot,
            ),
        )

        pin_service("storage", "42")

        _, saved = calls["patch"]
        assert saved["storage"].origin == "gitlab-mr"
        assert saved["storage"].ephemeral is False
