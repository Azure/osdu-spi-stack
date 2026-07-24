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
    ImageSource,
    ResolvedImage,
    image_lock_names,
    render_image_lock_configmap,
    resolve_ghcr_ref_image,
    resolve_ghcr_tag_image,
    resolve_image,
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


def test_render_image_lock_contains_service_keys_without_schema_load():
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
    assert "SCHEMA_LOAD_IMAGE_TAG" not in yaml


def test_resolve_ghcr_tag_image_pins_manifest_digest(monkeypatch):
    monkeypatch.setattr(
        images,
        "_ghcr_manifest_digest",
        lambda repository, tag: "sha256:" + ("b" * 64),
    )

    resolved = resolve_ghcr_tag_image(
        service_name="partition",
        org="yuchen-osdu",
        tag="main-snapshot",
    )

    assert resolved.repository == "ghcr.io/yuchen-osdu/partition"
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
        org="yuchen-osdu",
        ref="fix/core-lib-azure-3.0.1",
    )

    assert resolved.repository == "ghcr.io/yuchen-osdu/partition"
    assert resolved.tag == "sha-" + commit_sha[:12]
    assert resolved.digest == "sha256:" + ("b" * 64)
    assert resolved.image == f"{resolved.repository}@{resolved.digest}"


def test_render_ghcr_main_lock_records_tag_selector():
    resolved = {
        name: ResolvedImage(
            name=name,
            repository=f"ghcr.io/yuchen-osdu/{name}",
            tag="main-snapshot",
            created_at="",
            digest=f"sha256:{name}",
        )
        for name in image_lock_names()
    }

    yaml = render_image_lock_configmap(
        resolved,
        source=ImageSource.GHCR,
        org="yuchen-osdu",
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
