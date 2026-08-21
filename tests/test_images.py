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

from datetime import datetime, timezone

from spi import images
from spi.images import (
    ImageRegistryEntry,
    ResolvedImage,
    image_lock_names,
    render_image_lock_configmap,
    resolve_image,
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
