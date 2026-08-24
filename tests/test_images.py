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
    ImageSource,
    ResolvedImage,
    image_lock_names,
    render_image_lock_configmap,
    resolve_ghcr_ref_image,
    resolve_ghcr_tag_image,
    resolve_image,
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
        source=ImageSource.COMMUNITY,
        ref="master",
        org="",
        resolved_at=datetime(2026, 5, 22, tzinfo=timezone.utc),
    )

    assert "name: osdu-image-lock" in yaml
    assert 'IMAGE_SOURCE: "community"' in yaml
    assert 'IMAGE_TAG: ""' in yaml
    assert 'IMAGE_BRANCH: "master"' in yaml
    assert "PARTITION_IMAGE_REPOSITORY" in yaml
    assert "PARTITION_IMAGE_DIGEST" in yaml
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

    resolved = resolve_images(source=ImageSource.COMMUNITY, ref="master", names=("schema", "schema-load"))

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
        resolve_images(source=ImageSource.COMMUNITY, ref="master", names=("schema", "schema-load"))
    except ImageResolutionError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected ImageResolutionError")

    assert "schema: registry repository 'schema-service-master' not found" in message
    assert message.count("schema-load: unable to resolve matching schema tag") == 1


def test_resolve_ghcr_tag_image_pins_manifest_digest(monkeypatch):
    monkeypatch.setattr(
        images,
        "_ghcr_manifest_digest",
        lambda repository, tag: "sha256:" + ("b" * 64),
    )

    resolved = resolve_ghcr_tag_image(
        service_name="partition",
        org="Azure",
        tag="main-snapshot",
    )

    assert resolved.repository == "ghcr.io/azure/partition"
    assert resolved.tag == "main-snapshot"
    assert resolved.digest == "sha256:" + ("b" * 64)
    assert resolved.image == f"{resolved.repository}@{resolved.digest}"


def test_resolve_ghcr_ref_image_uses_ref_commit_and_manifest_digest(monkeypatch):
    commit_sha = "a" * 40

    monkeypatch.setattr(
        images,
        "github_get",
        lambda url: {
            "sha": commit_sha,
            "commit": {"committer": {"date": "2026-07-20T00:00:00Z"}},
        },
    )
    monkeypatch.setattr(
        images,
        "_ghcr_manifest_digest",
        lambda repository, tag: "sha256:" + ("b" * 64),
    )

    resolved = resolve_ghcr_ref_image(
        service_name="partition",
        org="Azure",
        ref="fix/core-lib-azure-3.0.1",
    )

    assert resolved.repository == "ghcr.io/azure/partition"
    assert resolved.tag == "sha-" + commit_sha[:12]
    assert resolved.digest == "sha256:" + ("b" * 64)
    assert resolved.image == f"{resolved.repository}@{resolved.digest}"


def test_render_ghcr_main_lock_records_tag_selector():
    resolved = {
        name: ResolvedImage(
            name=name,
            repository=f"ghcr.io/azure/{name}",
            tag="main-snapshot",
            created_at="",
            digest=f"sha256:{name}",
        )
        for name in image_lock_names()
    }

    yaml = render_image_lock_configmap(
        resolved,
        source=ImageSource.GHCR,
        org="Azure",
        resolved_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )

    assert 'IMAGE_TAG: "main-snapshot"' in yaml
    assert 'IMAGE_REF: ""' in yaml
    assert 'IMAGE_BRANCH: ""' in yaml


def test_gitlab_get_retries_transient_timeouts(monkeypatch):
    calls = {"count": 0}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout=0):
        calls["count"] += 1
        if calls["count"] < 3:
            raise TimeoutError("timed out")
        return FakeResponse()

    monkeypatch.setattr(images.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(images.time, "sleep", lambda seconds: None)

    assert images.gitlab_get("https://example.invalid/api") == {"ok": True}
    assert calls["count"] == 3


def test_gitlab_get_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(
        images.urllib.request,
        "urlopen",
        lambda request, timeout=0: (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    monkeypatch.setattr(images.time, "sleep", lambda seconds: None)

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

    resolved = resolve_images(source=ImageSource.COMMUNITY, ref="master", names=("schema-load",))

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
        resolve_images(source=ImageSource.COMMUNITY, ref="master", names=("schema-load",))

    message = str(exc_info.value)
    assert message.count(";") == 0
    assert message == (
        "schema-load: unable to resolve matching schema tag: "
        "schema: registry repository 'schema-service-master' not found"
    )


def test_image_lock_missing_schema_load_detects_legacy_lock():
    legacy = {"SCHEMA_IMAGE_TAG": "a" * 40}
    current = {
        "SCHEMA_IMAGE_TAG": "a" * 40,
        "SCHEMA_LOAD_IMAGE_REPOSITORY": "registry/schema-load",
        "SCHEMA_LOAD_IMAGE_TAG": "a" * 40,
    }

    assert images.image_lock_missing_schema_load(legacy) is True
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
        "community.opengroup.org:5555/osdu/schema-load-master@sha256:loader-matched"
    )
    assert patch["SCHEMA_LOAD_IMAGE_DIGEST"] == "sha256:loader-matched"
    assert patch["IMAGE_COUNT"] == "14"


def test_schema_load_lock_patch_without_schema_tag_raises(monkeypatch):
    def fail(url: str):
        raise AssertionError("registry must not be queried without a schema tag")

    monkeypatch.setattr(images, "gitlab_get", fail)

    with pytest.raises(ImageResolutionError, match="no schema image tag"):
        images.schema_load_lock_patch({"IMAGE_BRANCH": "master"})
