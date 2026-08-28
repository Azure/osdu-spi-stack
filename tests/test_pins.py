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

"""Per-service MR image pins: resolution, lock mutation, and refresh survival."""

import json
import subprocess

import pytest
from typer.testing import CliRunner

from spi import cli, pins, status
from spi.deploy_record import DeployRecord, DeployRecordError
from spi.images import ImageNotFoundError, ImageResolutionError, ResolvedImage
from spi.pins import (
    LOCK_MUTATION_MAX_ATTEMPTS,
    LockConflictError,
    MissingPipelineImageError,
    PinError,
    ResetResult,
    ServicePin,
    decode_pins,
    encode_pins,
    pin_service,
    ref_slug,
    reset_service,
)
from spi.status import KustomizationReadiness, StatusError, StatusReason


def _pin(**overrides) -> ServicePin:
    fields = dict(
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
            lambda service: ResetResult(restored=("schema",), refresh_required=("schema-load",)),
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
