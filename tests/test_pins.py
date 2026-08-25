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

import pytest

from spi import pins
from spi.images import ImageResolutionError, ResolvedImage
from spi.pins import (
    PinError,
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

    def test_missing_or_corrupt_annotation_reads_empty(self):
        assert decode_pins(_lock()) == {}
        assert decode_pins(_lock(pins_annotation="not json")) == {}
        assert decode_pins(_lock(pins_annotation=json.dumps({"schema": {"mr": "1"}}))) == {}


class TestResolveMrImage:
    def test_resolves_source_branch_slug_at_head_sha(self, monkeypatch):
        sha = "b" * 40
        monkeypatch.setattr(
            pins, "fetch_merge_request", lambda pid, iid: {"source_branch": "fix/x", "sha": sha}
        )
        captured = {}

        def fake_resolve(service, entry, branch, tag):
            captured.update(service=service, branch=branch, tag=tag)
            return ResolvedImage(service, "repo/schema-service-fix-x", tag, "", "")

        monkeypatch.setattr(pins, "resolve_image_tag", fake_resolve)
        image, _mr = pins.resolve_mr_image("schema", "847")
        assert captured == {"service": "schema", "branch": "fix-x", "tag": sha}
        assert image.tag == sha

    def test_falls_back_to_trusted_branch_copy(self, monkeypatch):
        sha = "b" * 40
        monkeypatch.setattr(
            pins, "fetch_merge_request", lambda pid, iid: {"source_branch": "fix/x", "sha": sha}
        )
        attempts = []

        def fake_resolve(service, entry, branch, tag):
            attempts.append(branch)
            if branch != "trusted-fix-x":
                raise ImageResolutionError(f"{service}: repository not found")
            return ResolvedImage(service, "repo/schema-service-trusted-fix-x", tag, "", "")

        monkeypatch.setattr(pins, "resolve_image_tag", fake_resolve)
        image, _mr = pins.resolve_mr_image("schema", "847")
        assert attempts == ["fix-x", "trusted-fix-x"]
        assert image.repository.endswith("trusted-fix-x")

    def test_missing_pipeline_image_names_the_mr(self, monkeypatch):
        monkeypatch.setattr(
            pins,
            "fetch_merge_request",
            lambda pid, iid: {"source_branch": "fix/x", "sha": "b" * 40},
        )

        def raise_missing(service, entry, branch, tag=None):
            raise ImageResolutionError(f"{service}: tag not found")

        monkeypatch.setattr(pins, "resolve_image_tag", raise_missing)
        monkeypatch.setattr(pins, "resolve_image", raise_missing)
        with pytest.raises(PinError, match="containerize pipeline"):
            pins.resolve_mr_image("schema", "847")


class TestPinService:
    def _wire(self, monkeypatch, lock, resolved_names):
        calls = {"patch": None, "reconciled": None}
        monkeypatch.setattr(pins, "read_lock", lambda: lock)

        def fake_resolve(service, mr_iid):
            if service not in resolved_names:
                raise PinError(f"{service}: no image")
            return (
                ResolvedImage(service, f"repo/{service}-fix-x", "b" * 40, "now", "sha256:new"),
                {"source_branch": "fix/x", "sha": "b" * 40},
            )

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

    def test_schema_pin_pairs_the_loader(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(), {"schema", "schema-load"})
        results = pin_service("schema", "847")
        assert [name for name, _ in results] == ["schema", "schema-load"]
        assert calls["reconciled"] == ["schema", "schema-load"]

    def test_schema_pin_tolerates_missing_loader_image(self, monkeypatch):
        calls = self._wire(monkeypatch, _lock(), {"schema"})
        results = pin_service("schema", "847")
        assert [name for name, _ in results] == ["schema"]
        assert calls["reconciled"] == ["schema"]


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

        assert reset_service("storage") == ["storage"]
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

        assert reset_service("schema") == ["schema", "schema-load"]
        assert calls["pins"] == {}

    def test_unpinned_service_errors(self, monkeypatch):
        monkeypatch.setattr(pins, "read_lock", lambda: _lock())
        with pytest.raises(PinError, match="not pinned"):
            reset_service("storage")


class TestRefreshSurvival:
    def test_reapply_pins_patches_pinned_entries(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            pins,
            "patch_lock",
            lambda data, p, description: captured.update(data=data, pins=p),
        )
        active = {"schema": _pin()}
        pins.reapply_pins(active)
        assert captured["data"]["SCHEMA_IMAGE_TAG"] == "a" * 40
        assert captured["pins"] is active

    def test_reapply_pins_noop_when_empty(self, monkeypatch):
        monkeypatch.setattr(
            pins,
            "patch_lock",
            lambda *a, **k: pytest.fail("patch_lock must not run without pins"),
        )
        pins.reapply_pins({})

    def test_live_pins_swallow_cluster_errors(self, monkeypatch):
        def boom():
            raise PinError("no cluster")

        monkeypatch.setattr(pins, "read_lock", boom)
        assert pins.live_pins() == {}


class TestReconcileConsumers:
    def test_maps_service_files_to_kustomizations(self, monkeypatch):
        annotated = []

        def fake_run(command, **kwargs):
            annotated.append(command[3])

        monkeypatch.setattr(pins, "run_command", fake_run)
        pins.reconcile_consumers(["storage", "unit", "schema-load"])
        assert annotated == [
            "kustomization/spi-osdu-reference",
            "kustomization/spi-osdu-schema-load",
            "kustomization/spi-osdu-services",
        ]
