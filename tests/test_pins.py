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
from typing import TypedDict

import pytest
from typer.testing import CliRunner

from spi import cli, pins
from spi.images import ImageNotFoundError, ImageResolutionError, ResolvedImage
from spi.pins import (
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


def _lock(data=None, pins_annotation=""):
    annotations = {}
    if pins_annotation:
        annotations[pins.PINS_ANNOTATION] = pins_annotation
    return {
        "metadata": {"annotations": annotations},
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


class _PinServiceCalls(TypedDict):
    patch: tuple[dict[str, str], dict[str, ServicePin]] | None
    reconciled: list[str] | None
    fetches: int


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
    def _wire(self, monkeypatch, lock, resolved_names):
        calls: _PinServiceCalls = {"patch": None, "reconciled": None, "fetches": 0}
        mr = {"source_branch": "fix/x", "sha": "b" * 40}
        monkeypatch.setattr(pins, "read_lock", lambda: lock)

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
        monkeypatch.setattr(
            pins,
            "patch_lock",
            lambda data, p, description: calls.__setitem__("patch", (data, dict(p))),
        )
        monkeypatch.setattr(
            pins, "reconcile_consumers", lambda names: calls.__setitem__("reconciled", names)
        )
        return calls

    def test_unknown_service_rejected(self, monkeypatch):
        with pytest.raises(PinError, match="Unknown service"):
            pin_service("nope", "1")

    def test_schema_load_direct_pin_rejected(self):
        with pytest.raises(PinError, match="Pin 'schema'"):
            pin_service("schema-load", "1")

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
        assert calls["fetches"] == 1
        assert calls["reconciled"] == ["schema", "schema-load"]

    def test_schema_pin_tolerates_missing_loader_image(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(data=_canonical_data("schema")), {"schema"})
        results = pin_service("schema", "847")
        assert [name for name, _ in results] == ["schema"]
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


class TestResetService:
    def test_restores_canonical_and_drops_pin(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"storage": _pin()}))
        calls = {}
        monkeypatch.setattr(pins, "read_lock", lambda: lock)
        monkeypatch.setattr(
            pins,
            "patch_lock",
            lambda data, p, description: calls.update(data=data, pins=dict(p)),
        )
        monkeypatch.setattr(pins, "reconcile_consumers", lambda names: None)

        result = reset_service("storage")
        assert result == ResetResult(restored=("storage",), refresh_required=())
        assert calls["data"]["STORAGE_IMAGE_TAG"] == "c" * 40
        assert calls["data"]["STORAGE_IMAGE_REPOSITORY"] == "registry/schema-service-master"
        assert calls["pins"] == {}

    def test_schema_reset_releases_the_loader_too(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"schema": _pin(), "schema-load": _pin()}))
        calls = {}
        monkeypatch.setattr(pins, "read_lock", lambda: lock)
        monkeypatch.setattr(
            pins, "patch_lock", lambda data, p, description: calls.update(pins=dict(p))
        )
        monkeypatch.setattr(pins, "reconcile_consumers", lambda names: None)

        result = reset_service("schema")
        assert result == ResetResult(
            restored=("schema", "schema-load"),
            refresh_required=(),
        )
        assert calls["pins"] == {}

    def test_schema_reset_drops_invalid_loader_pin_for_refresh(self, monkeypatch):
        loader = _pin(canonical_repository="", canonical_tag="")
        lock = _lock(pins_annotation=encode_pins({"schema": _pin(), "schema-load": loader}))
        calls = {}
        monkeypatch.setattr(pins, "read_lock", lambda: lock)
        monkeypatch.setattr(
            pins,
            "patch_lock",
            lambda data, p, description: calls.update(
                data=data,
                pins=dict(p),
                description=description,
            ),
        )
        monkeypatch.setattr(
            pins,
            "reconcile_consumers",
            lambda names: calls.update(reconciled=names),
        )

        result = reset_service("schema")

        assert result == ResetResult(
            restored=("schema",),
            refresh_required=("schema-load",),
        )
        assert calls["pins"] == {}
        assert calls["data"]["SCHEMA_IMAGE_TAG"] == "c" * 40
        assert "SCHEMA_LOAD_IMAGE_TAG" not in calls["data"]
        assert calls["reconciled"] == ["schema"]

    def test_reset_drops_invalid_pin_without_reconciling_stale_image(self, monkeypatch):
        invalid = _pin(canonical_repository="", canonical_tag="")
        lock = _lock(pins_annotation=encode_pins({"storage": invalid}))
        calls = {}
        monkeypatch.setattr(pins, "read_lock", lambda: lock)
        monkeypatch.setattr(
            pins,
            "patch_lock",
            lambda data, p, description: calls.update(data=data, pins=dict(p)),
        )
        monkeypatch.setattr(
            pins,
            "reconcile_consumers",
            lambda names: pytest.fail("stale image must not be reconciled"),
        )

        result = reset_service("storage")

        assert result == ResetResult(restored=(), refresh_required=("storage",))
        assert calls == {"data": {}, "pins": {}}

    def test_schema_load_reset_does_not_duplicate_target(self, monkeypatch):
        lock = _lock(pins_annotation=encode_pins({"schema-load": _pin()}))
        calls = {}
        monkeypatch.setattr(pins, "read_lock", lambda: lock)
        monkeypatch.setattr(
            pins, "patch_lock", lambda data, p, description: calls.update(pins=dict(p))
        )
        monkeypatch.setattr(pins, "reconcile_consumers", lambda names: None)

        result = reset_service("schema-load")
        assert result == ResetResult(restored=("schema-load",), refresh_required=())
        assert calls["pins"] == {}

    def test_unpinned_service_errors(self, monkeypatch):
        monkeypatch.setattr(pins, "read_lock", lambda: _lock())
        with pytest.raises(PinError, match="not pinned"):
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


class TestReconcileConsumers:
    def test_reconciles_in_dependency_order_and_blocks(self, monkeypatch):
        """services must settle before schema-load runs, and schema-load
        before reference re-seeds, mirroring the refresh sequence."""
        reconciled = []

        def fake_run(command, **kwargs):
            assert command[:3] == ["flux", "reconcile", "kustomization"]
            reconciled.append(command[3])

        monkeypatch.setattr(pins, "run_command", fake_run)
        pins.reconcile_consumers(["unit", "schema-load", "storage"])
        assert reconciled == [
            "spi-osdu-services",
            "spi-osdu-schema-load",
            "spi-osdu-reference",
        ]

    def test_only_affected_kustomizations_reconcile(self, monkeypatch):
        reconciled = []
        monkeypatch.setattr(
            pins, "run_command", lambda command, **kwargs: reconciled.append(command[3])
        )
        pins.reconcile_consumers(["storage"])
        assert reconciled == ["spi-osdu-services"]
