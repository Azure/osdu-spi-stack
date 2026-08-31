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

import urllib.error
from datetime import datetime, timezone
from email.message import Message

import pytest

from spi import images
from spi.images import (
    ImageRegistryEntry,
    ImageResolutionError,
    ResolvedImage,
    image_lock_names,
    render_image_lock_configmap,
    resolve_image,
    resolve_image_commit,
    resolve_image_tag,
    resolve_images,
)


def test_resolve_image_selects_newest_immutable_sha(monkeypatch):
    old_sha = "a" * 40
    new_sha = "b" * 40

    def fake_gitlab_get(url: str):
        if "registry/repositories?" in url:
            return [
                {
                    "id": 123,
                    "name": "partition-master",
                    "location": "community.opengroup.org:5555/osdu/partition-master",
                }
            ]
        if url.endswith("/tags?per_page=100&page=1"):
            return [{"name": old_sha}, {"name": "latest"}, {"name": new_sha}]
        if url.endswith(f"/tags/{old_sha}"):
            return {
                "name": old_sha,
                "created_at": "2026-05-01T00:00:00+00:00",
                "digest": "sha256:old",
            }
        if url.endswith("/tags/latest"):
            return {
                "name": "latest",
                "created_at": "2026-05-22T00:00:00+00:00",
                "digest": "sha256:latest",
            }
        if url.endswith(f"/tags/{new_sha}"):
            return {
                "name": new_sha,
                "created_at": "2026-05-21T00:00:00+00:00",
                "digest": "sha256:new",
            }
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(images, "gitlab_get", fake_gitlab_get)

    resolved = resolve_image(
        "partition",
        ImageRegistryEntry(1, "partition", "services/partition.yaml"),
        "master",
    )

    assert resolved.tag == new_sha
    assert resolved.digest == "sha256:new"


def test_render_image_lock_contains_schema_load_service_keys():
    resolved = {
        name: ResolvedImage(
            name=name,
            repository=f"community.opengroup.org:5555/example/{name}",
            tag="1" * 40,
            created_at="2026-05-22T00:00:00+00:00",
            digest=f"sha256:{name}",
        )
        for name in image_lock_names()
    }

    yaml = render_image_lock_configmap(
        resolved,
        branch="master",
        resolved_at=datetime(2026, 5, 22, tzinfo=timezone.utc),
    )

    assert "name: osdu-image-lock" in yaml
    assert 'IMAGE_BRANCH: "master"' in yaml
    assert "PARTITION_IMAGE_REPOSITORY" in yaml
    assert "INDEXER_QUEUE_IMAGE_TAG" in yaml
    assert "SCHEMA_LOAD_IMAGE_REPOSITORY" in yaml
    assert "SCHEMA_LOAD_IMAGE_TAG" in yaml


def test_schema_load_resolves_from_selected_schema_tag(monkeypatch):
    older_sha = "a" * 40
    schema_sha = "b" * 40
    loader_newest_sha = "c" * 40

    def fake_gitlab_get(url: str):
        if "registry/repositories?" in url:
            if "search=schema-service-schema-load-master" in url:
                return [
                    {
                        "id": 456,
                        "name": "schema-service-schema-load-master",
                        "location": "community.opengroup.org:5555/osdu/schema-load-master",
                    }
                ]
            if "search=schema-service-master" in url:
                return [
                    {
                        "id": 123,
                        "name": "schema-service-master",
                        "location": "community.opengroup.org:5555/osdu/schema-service-master",
                    }
                ]
        if url.endswith("/registry/repositories/123/tags?per_page=100&page=1"):
            return [{"name": older_sha}, {"name": schema_sha}]
        if url.endswith("/registry/repositories/456/tags?per_page=100&page=1"):
            return [{"name": schema_sha}, {"name": loader_newest_sha}]
        if url.endswith(f"/registry/repositories/123/tags/{older_sha}"):
            return {
                "name": older_sha,
                "created_at": "2026-05-01T00:00:00+00:00",
                "digest": "sha256:schema-old",
            }
        if url.endswith(f"/registry/repositories/123/tags/{schema_sha}"):
            return {
                "name": schema_sha,
                "created_at": "2026-05-21T00:00:00+00:00",
                "digest": "sha256:schema-new",
            }
        if url.endswith(f"/registry/repositories/456/tags/{schema_sha}"):
            return {
                "name": schema_sha,
                "created_at": "2026-05-20T00:00:00+00:00",
                "digest": "sha256:loader-matched",
            }
        if url.endswith(f"/registry/repositories/456/tags/{loader_newest_sha}"):
            return {
                "name": loader_newest_sha,
                "created_at": "2026-05-22T00:00:00+00:00",
                "digest": "sha256:loader-newest",
            }
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(images, "gitlab_get", fake_gitlab_get)

    resolved = resolve_images(branch="master", names=("schema", "schema-load"))

    assert resolved["schema"].tag == schema_sha
    assert resolved["schema-load"].tag == schema_sha
    assert resolved["schema-load"].digest == "sha256:loader-matched"


def test_schema_load_dependency_error_is_reported_once(monkeypatch):
    def fake_gitlab_get(url: str):
        if "registry/repositories?" in url and "search=schema-service-master" in url:
            return []
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(images, "gitlab_get", fake_gitlab_get)

    try:
        resolve_images(branch="master", names=("schema", "schema-load"))
    except ImageResolutionError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected ImageResolutionError")

    assert "schema: registry repository 'schema-service-master' not found" in message
    assert message.count("schema-load: unable to resolve matching schema tag") == 1


def test_gitlab_get_retries_transient_timeouts(monkeypatch):
    """Transient network failures retry with backoff; success on a later
    attempt returns normally instead of aborting the whole resolution."""
    from spi import images

    calls = {"n": 0}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("The read operation timed out")
        return FakeResponse()

    monkeypatch.setattr(images.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(images.time, "sleep", lambda s: None)

    assert images.gitlab_get("https://example.invalid/api") == {"ok": True}
    assert calls["n"] == 3


def test_gitlab_get_raises_after_exhausting_attempts(monkeypatch):
    from spi import images

    def always_timeout(req, timeout=0):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(images.urllib.request, "urlopen", always_timeout)
    monkeypatch.setattr(images.time, "sleep", lambda s: None)

    try:
        images.gitlab_get("https://example.invalid/api", attempts=2)
    except images.ImageResolutionError as exc:
        assert "2 attempts" in str(exc)
    else:
        raise AssertionError("expected ImageResolutionError")


def test_resolve_image_tag_missing_tag_raises_resolution_error(monkeypatch):
    """A schema tag that never reached the loader repository (divergent
    pipelines/retention) has to fail fast with a clear, service-specific
    error instead of bubbling up a raw HTTPError."""

    def fake_gitlab_get(url: str):
        if "registry/repositories?" in url:
            return [
                {
                    "id": 456,
                    "name": "schema-service-schema-load-master",
                    "location": "community.opengroup.org:5555/osdu/schema-load-master",
                }
            ]
        raise AssertionError(f"unexpected URL: {url}")

    def fake_tag_detail(project_id, repo_id, tag):
        raise urllib.error.HTTPError("https://example.invalid", 404, "Not Found", Message(), None)

    monkeypatch.setattr(images, "gitlab_get", fake_gitlab_get)
    monkeypatch.setattr(images, "_tag_detail", fake_tag_detail)

    entry = ImageRegistryEntry(26, "schema-service-schema-load", "schema-load/job.yaml")

    with pytest.raises(ImageResolutionError, match="tag .* not found"):
        resolve_image_tag("schema-load", entry, "master", "a" * 40)


def test_resolve_image_tag_propagates_non_404_http_error(monkeypatch):
    """A non-404 HTTPError (e.g. a registry outage) is a transient/unknown
    failure, not a missing-tag condition, and should not be masked as an
    ImageResolutionError."""

    def fake_gitlab_get(url: str):
        if "registry/repositories?" in url:
            return [
                {
                    "id": 456,
                    "name": "schema-service-schema-load-master",
                    "location": "community.opengroup.org:5555/osdu/schema-load-master",
                }
            ]
        raise AssertionError(f"unexpected URL: {url}")

    def fake_tag_detail(project_id, repo_id, tag):
        raise urllib.error.HTTPError(
            "https://example.invalid", 500, "Internal Server Error", Message(), None
        )

    monkeypatch.setattr(images, "gitlab_get", fake_gitlab_get)
    monkeypatch.setattr(images, "_tag_detail", fake_tag_detail)

    entry = ImageRegistryEntry(26, "schema-service-schema-load", "schema-load/job.yaml")

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        resolve_image_tag("schema-load", entry, "master", "a" * 40)
    assert exc_info.value.code == 500


def test_resolve_image_commit_matches_short_sha_tags(monkeypatch):
    """OSDU pipelines tag with CI short SHAs, so a tag matches the MR head
    commit when it is a prefix of the full SHA; unrelated tags never do."""
    sha = "1f325c1e71be" + "d" * 28

    def fake_gitlab_get(url: str):
        if "registry/repositories?" in url:
            return [
                {
                    "id": 9,
                    "name": "schema-service-trusted-fix-x",
                    "location": "registry/schema-service-trusted-fix-x",
                }
            ]
        if url.endswith("/tags?per_page=100&page=1"):
            return [{"name": "e" * 12}, {"name": "1f325c1e71be"}]
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(images, "gitlab_get", fake_gitlab_get)
    monkeypatch.setattr(
        images,
        "_tag_detail",
        lambda project_id, repo_id, tag: {"name": tag, "created_at": "now", "digest": "sha256:x"},
    )

    entry = ImageRegistryEntry(26, "schema-service", "services/schema.yaml")
    image = resolve_image_commit("schema", entry, "trusted-fix-x", sha)
    assert image.tag == "1f325c1e71be"

    with pytest.raises(ImageResolutionError, match="no tag for commit"):
        resolve_image_commit("schema", entry, "trusted-fix-x", "f" * 40)


def test_resolve_image_commit_handles_tag_pruned_after_listing(monkeypatch):
    sha = "1f325c1e71be" + "d" * 28

    def fake_gitlab_get(url: str):
        if "registry/repositories?" in url:
            return [
                {
                    "id": 9,
                    "name": "schema-service-fix-x",
                    "location": "registry/schema-service-fix-x",
                }
            ]
        if url.endswith("/tags?per_page=100&page=1"):
            return [{"name": sha[:12]}]
        raise AssertionError(f"unexpected URL: {url}")

    def fake_tag_detail(project_id, repo_id, tag):
        raise urllib.error.HTTPError("https://example.invalid", 404, "Not Found", Message(), None)

    monkeypatch.setattr(images, "gitlab_get", fake_gitlab_get)
    monkeypatch.setattr(images, "_tag_detail", fake_tag_detail)

    entry = ImageRegistryEntry(26, "schema-service", "services/schema.yaml")
    with pytest.raises(ImageResolutionError, match="tag .* not found"):
        resolve_image_commit("schema", entry, "fix-x", sha)


def test_resolve_images_schema_load_only_omits_schema(monkeypatch):
    """Requesting only schema-load has to resolve schema as a dependency
    without returning it, and use the loader-specific error message on
    lookup failure."""
    schema_sha = "b" * 40
    loader_newest_sha = "c" * 40

    def fake_gitlab_get(url: str):
        if "registry/repositories?" in url:
            if "search=schema-service-schema-load-master" in url:
                return [
                    {
                        "id": 456,
                        "name": "schema-service-schema-load-master",
                        "location": "community.opengroup.org:5555/osdu/schema-load-master",
                    }
                ]
            if "search=schema-service-master" in url:
                return [
                    {
                        "id": 123,
                        "name": "schema-service-master",
                        "location": "community.opengroup.org:5555/osdu/schema-service-master",
                    }
                ]
        if url.endswith("/registry/repositories/123/tags?per_page=100&page=1"):
            return [{"name": schema_sha}]
        if url.endswith("/registry/repositories/456/tags?per_page=100&page=1"):
            return [{"name": schema_sha}, {"name": loader_newest_sha}]
        if url.endswith(f"/registry/repositories/123/tags/{schema_sha}"):
            return {
                "name": schema_sha,
                "created_at": "2026-05-21T00:00:00+00:00",
                "digest": "sha256:schema-new",
            }
        if url.endswith(f"/registry/repositories/456/tags/{schema_sha}"):
            return {
                "name": schema_sha,
                "created_at": "2026-05-20T00:00:00+00:00",
                "digest": "sha256:loader-matched",
            }
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(images, "gitlab_get", fake_gitlab_get)

    resolved = resolve_images(branch="master", names=("schema-load",))

    assert set(resolved) == {"schema-load"}
    assert resolved["schema-load"].tag == schema_sha
    assert resolved["schema-load"].digest == "sha256:loader-matched"


def test_resolve_images_schema_load_only_reports_alternate_error(monkeypatch):
    """When only schema-load is requested and the schema dependency lookup
    fails, the error message should not claim the caller asked for schema."""

    def fake_gitlab_get(url: str):
        if "registry/repositories?" in url and "search=schema-service-master" in url:
            return []
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(images, "gitlab_get", fake_gitlab_get)

    with pytest.raises(ImageResolutionError) as exc_info:
        resolve_images(branch="master", names=("schema-load",))

    message = str(exc_info.value)
    assert message.count(";") == 0
    assert message == (
        "schema-load: unable to resolve matching schema tag: "
        "schema: registry repository 'schema-service-master' not found"
    )


def test_image_lock_missing_schema_load_detects_legacy_lock():
    legacy = {"SCHEMA_IMAGE_TAG": "a" * 40}
    missing_ref = {
        "SCHEMA_IMAGE_TAG": "a" * 40,
        "SCHEMA_LOAD_IMAGE_REPOSITORY": "registry/schema-load",
        "SCHEMA_LOAD_IMAGE_TAG": "a" * 40,
    }
    current = {
        "SCHEMA_IMAGE_TAG": "a" * 40,
        "SCHEMA_LOAD_IMAGE_REPOSITORY": "registry/schema-load",
        "SCHEMA_LOAD_IMAGE_TAG": "a" * 40,
        "SCHEMA_LOAD_IMAGE_REF": "registry/schema-load:" + "a" * 40,
    }

    assert images.image_lock_missing_schema_load(legacy) is True
    # A lock with repository/tag but no composed ref still needs backfilling:
    # the Job substitutes the single ref key.
    assert images.image_lock_missing_schema_load(missing_ref) is True
    assert images.image_lock_missing_schema_load(current) is False


def test_schema_load_lock_patch_resolves_from_recorded_schema_tag(monkeypatch):
    """Backfilling a legacy lock has to reuse the schema tag it already pins,
    so the loader matches the running service instead of jumping to master."""
    schema_sha = "d" * 40

    def fake_gitlab_get(url: str):
        if "registry/repositories?" in url and "search=schema-service-schema-load-master" in url:
            return [
                {
                    "id": 456,
                    "name": "schema-service-schema-load-master",
                    "location": "community.opengroup.org:5555/osdu/schema-load-master",
                }
            ]
        if url.endswith(f"/registry/repositories/456/tags/{schema_sha}"):
            return {
                "name": schema_sha,
                "created_at": "2026-05-20T00:00:00+00:00",
                "digest": "sha256:loader-matched",
            }
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(images, "gitlab_get", fake_gitlab_get)

    patch = images.schema_load_lock_patch(
        {"IMAGE_BRANCH": "master", "IMAGE_COUNT": "13", "SCHEMA_IMAGE_TAG": schema_sha}
    )

    assert patch["SCHEMA_LOAD_IMAGE_TAG"] == schema_sha
    assert patch["SCHEMA_LOAD_IMAGE_REPOSITORY"] == (
        "community.opengroup.org:5555/osdu/schema-load-master"
    )
    assert patch["SCHEMA_LOAD_IMAGE"] == (
        f"community.opengroup.org:5555/osdu/schema-load-master:{schema_sha}"
    )
    assert patch["SCHEMA_LOAD_IMAGE_DIGEST"] == "sha256:loader-matched"
    assert (
        patch["SCHEMA_LOAD_IMAGE_REF"]
        == "community.opengroup.org:5555/osdu/schema-load-master@sha256:loader-matched"
    )
    assert patch["IMAGE_COUNT"] == "14"


def test_schema_load_lock_patch_composes_missing_ref_without_incrementing_count(monkeypatch):
    def fail(url: str):
        raise AssertionError(f"registry must not be queried to compose an existing image: {url}")

    monkeypatch.setattr(images, "gitlab_get", fail)
    patch = images.schema_load_lock_patch(
        {
            "IMAGE_BRANCH": "master",
            "IMAGE_COUNT": "14",
            "SCHEMA_LOAD_IMAGE_REPOSITORY": "registry/schema-load",
            "SCHEMA_LOAD_IMAGE_TAG": "a" * 40,
            "SCHEMA_LOAD_IMAGE_DIGEST": "sha256:loader",
        }
    )

    assert patch == {"SCHEMA_LOAD_IMAGE_REF": "registry/schema-load@sha256:loader"}


def test_schema_load_lock_patch_without_schema_tag_raises(monkeypatch):
    def fail(url: str):
        raise AssertionError("registry must not be queried without a schema tag")

    monkeypatch.setattr(images, "gitlab_get", fail)

    with pytest.raises(ImageResolutionError, match="no schema image tag"):
        images.schema_load_lock_patch({"IMAGE_BRANCH": "master"})


class TestImageRef:
    def test_digest_first_composition(self):
        assert images.image_ref("repo/x", "v1", "sha256:abc") == "repo/x@sha256:abc"

    def test_tag_fallback_when_digest_empty(self):
        assert images.image_ref("repo/x", "v1", "") == "repo/x:v1"


class TestLockDataRefRendering:
    def test_render_image_lock_includes_composed_ref_per_service(self):
        resolved = {
            name: ResolvedImage(
                name=name,
                repository=f"community.opengroup.org:5555/example/{name}",
                tag="1" * 40,
                created_at="2026-05-22T00:00:00+00:00",
                digest=f"sha256:{name}",
            )
            for name in image_lock_names()
        }

        yaml = render_image_lock_configmap(
            resolved,
            branch="master",
            resolved_at=datetime(2026, 5, 22, tzinfo=timezone.utc),
        )

        assert (
            'PARTITION_IMAGE_REF: "community.opengroup.org:5555/example/partition@sha256:partition"'
            in yaml
        )
        assert (
            'SCHEMA_LOAD_IMAGE_REF: "community.opengroup.org:5555/example/schema-load@sha256:schema-load"'
            in yaml
        )

    def test_render_image_lock_falls_back_to_tag_when_digest_missing(self):
        resolved = {
            name: ResolvedImage(
                name=name,
                repository=f"community.opengroup.org:5555/example/{name}",
                tag="1" * 40,
                created_at="",
                digest="",
            )
            for name in image_lock_names()
        }

        yaml = render_image_lock_configmap(resolved, branch="master")

        assert (
            f'PARTITION_IMAGE_REF: "community.opengroup.org:5555/example/partition:{"1" * 40}"'
            in yaml
        )


class TestGhcrIndexChildDigests:
    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            import json

            return json.dumps(self._payload).encode()

    def test_returns_the_index_children(self, monkeypatch):
        from spi import images

        responses = iter(
            [
                self._Response({"token": "t"}),
                self._Response(
                    {
                        "mediaType": "application/vnd.oci.image.index.v1+json",
                        "manifests": [
                            {"digest": "sha256:" + "1" * 64},
                            {"digest": "sha256:" + "2" * 64},
                            {"annotations": {"vnd.docker.reference.type": "attestation-manifest"}},
                        ],
                    }
                ),
            ]
        )
        monkeypatch.setattr(
            images.urllib.request, "urlopen", lambda req, timeout=15: next(responses)
        )

        digests = images.ghcr_index_child_digests("ghcr.io/azure/storage", "sha256:" + "f" * 64)

        assert digests == ("sha256:" + "1" * 64, "sha256:" + "2" * 64)

    def test_single_manifest_has_no_children(self, monkeypatch):
        from spi import images

        responses = iter(
            [
                self._Response({"token": "t"}),
                self._Response({"config": {"digest": "sha256:" + "3" * 64}}),
            ]
        )
        monkeypatch.setattr(
            images.urllib.request, "urlopen", lambda req, timeout=15: next(responses)
        )

        assert images.ghcr_index_child_digests("ghcr.io/azure/storage", "sha256:" + "f" * 64) == ()

    def test_fetch_failure_narrows_to_index_matching(self, monkeypatch):
        from spi import images

        def always_timeout(req, timeout=15):
            raise TimeoutError("timed out")

        monkeypatch.setattr(images.urllib.request, "urlopen", always_timeout)

        assert images.ghcr_index_child_digests("ghcr.io/azure/storage", "sha256:" + "f" * 64) == ()

    def test_non_ghcr_repository_is_never_fetched(self, monkeypatch):
        from spi import images

        def fail_urlopen(*args, **kwargs):
            pytest.fail("a non-GHCR repository must not be fetched")

        monkeypatch.setattr(images.urllib.request, "urlopen", fail_urlopen)

        assert (
            images.ghcr_index_child_digests(
                "community.opengroup.org:5555/example/storage", "sha256:" + "f" * 64
            )
            == ()
        )
