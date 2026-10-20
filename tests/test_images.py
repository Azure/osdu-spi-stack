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

import json
import urllib.error
from datetime import datetime, timezone
from email.message import Message

import pytest

from spi import images
from spi.images import (
    ImageResolutionError,
    ResolvedImage,
    build_lock_data,
    image_lock_names,
    render_image_lock_configmap,
    resolve_images,
)


def test_render_image_lock_contains_schema_load_service_keys():
    resolved = {
        name: ResolvedImage(
            name=name,
            repository=f"ghcr.io/azure/osdu-spi-{name}",
            tag="sha-" + "1" * 12,
            created_at="2026-05-22T00:00:00+00:00",
            digest=f"sha256:{name}",
        )
        for name in image_lock_names()
    }

    yaml = render_image_lock_configmap(
        resolved,
        resolved_at=datetime(2026, 5, 22, tzinfo=timezone.utc),
    )

    assert "name: osdu-image-lock" in yaml
    assert "IMAGE_BRANCH" not in yaml
    assert "PARTITION_IMAGE_REPOSITORY" in yaml
    assert "INDEXER_QUEUE_IMAGE_TAG" in yaml
    assert "SCHEMA_LOAD_IMAGE_REPOSITORY" in yaml
    assert "SCHEMA_LOAD_IMAGE_TAG" in yaml


def test_lock_data_marks_only_named_services_do_not_disrupt():
    resolved = {
        name: ResolvedImage(name, f"repo/{name}", "sha-1", "", "") for name in image_lock_names()
    }

    data = build_lock_data(resolved, "now", do_not_disrupt={"storage"})
    default = build_lock_data(resolved, "now")

    assert data["STORAGE_DO_NOT_DISRUPT"] == "true"
    assert data["INDEXER_QUEUE_DO_NOT_DISRUPT"] == "false"
    assert {default[f"{name.upper().replace('-', '_')}_DO_NOT_DISRUPT"] for name in resolved} == {
        "false"
    }


def test_schema_load_dependency_error_is_reported_once(monkeypatch):
    def missing(name, repo):
        raise ImageResolutionError(f"{name}: {repo} publishes no main-snapshot image")

    monkeypatch.setattr(images, "resolve_fork_image", missing)

    with pytest.raises(ImageResolutionError) as exc_info:
        resolve_images(names=("schema", "schema-load"))

    message = str(exc_info.value)
    assert "schema: Azure/osdu-spi-schema publishes no main-snapshot image" in message
    assert message.count("schema-load: unable to resolve matching schema tag") == 1


def test_resolve_images_schema_load_only_omits_schema(monkeypatch):
    """Requesting only schema-load resolves schema as a dependency without returning it."""
    schema = ResolvedImage("schema", "ghcr.io/azure/osdu-spi-schema", "sha-b", "d", "sha256:s")
    monkeypatch.setattr(images, "resolve_fork_image", lambda name, repo: (schema, "b" * 12))
    monkeypatch.setattr(
        images,
        "resolve_fork_loader",
        lambda repository, commit: (f"{repository}-load", "sha256:loader"),
    )

    resolved = resolve_images(names=("schema-load",))

    assert set(resolved) == {"schema-load"}
    assert resolved["schema-load"].tag == "sha-b"
    assert resolved["schema-load"].digest == "sha256:loader"


def test_resolve_images_schema_load_only_reports_alternate_error(monkeypatch):
    """When only schema-load is requested and the schema dependency lookup
    fails, the error message should not claim the caller asked for schema."""

    def missing(name, repo):
        raise ImageResolutionError(f"{name}: {repo} publishes no main-snapshot image")

    monkeypatch.setattr(images, "resolve_fork_image", missing)

    with pytest.raises(ImageResolutionError) as exc_info:
        resolve_images(names=("schema-load",))

    message = str(exc_info.value)
    assert message.count(";") == 0
    assert message == (
        "schema-load: unable to resolve matching schema tag: "
        "schema: Azure/osdu-spi-schema publishes no main-snapshot image"
    )


class TestImageRef:
    def test_digest_first_composition(self):
        assert images.image_ref("repo/x", "v1", "sha256:abc") == "repo/x@sha256:abc"

    def test_tag_fallback_when_digest_empty(self):
        assert images.image_ref("repo/x", "v1", "") == "repo/x:v1"


class TestLockDataRefRendering:
    def test_only_an_entry_with_a_paired_acceptance_image_records_one(self):
        resolved = {
            name: ResolvedImage(name, f"repo/{name}", "t", "", "sha256:s")
            for name in image_lock_names()
        }
        resolved["partition"] = ResolvedImage(
            "partition", "ghcr.io/acme/partition", "sha-b", "", "sha256:s", "sha256:acc"
        )

        data = images.build_lock_data(resolved, "now")

        assert {k: v for k, v in data.items() if k.endswith("_ACCEPTANCE_DIGEST")} == {
            "PARTITION_ACCEPTANCE_DIGEST": "sha256:acc"
        }

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

        yaml = render_image_lock_configmap(resolved)

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


class TestResolveGhcrManifest:
    class _Response:
        def __init__(self, body=b'{"token": "t"}'):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self._body

    def test_rate_limit_is_retried_as_transient(self, monkeypatch):
        from spi import images

        calls = {"n": 0}

        def fake_urlopen(req, timeout=15):
            calls["n"] += 1
            # Per attempt: a token fetch then the manifest HEAD. Rate-limit
            # the first attempt's HEAD, succeed on the second.
            if calls["n"] == 2:
                raise urllib.error.HTTPError(
                    req.full_url, 429, "Too Many Requests", Message(), None
                )
            return self._Response()

        monkeypatch.setattr(images.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(images.time, "sleep", lambda s: None)

        images.resolve_ghcr_manifest("ghcr.io/azure/storage", "sha256:" + "f" * 64)

        assert calls["n"] == 4

    def test_malformed_token_response_fails_closed(self, monkeypatch):
        from spi import images

        monkeypatch.setattr(
            images.urllib.request,
            "urlopen",
            lambda req, timeout=15: self._Response(body=b"not json"),
        )
        monkeypatch.setattr(images.time, "sleep", lambda s: None)

        with pytest.raises(images.ImageResolutionError, match="unreachable after 2 attempts"):
            images.resolve_ghcr_manifest("ghcr.io/azure/storage", "sha256:" + "f" * 64, attempts=2)

    def test_non_object_token_payload_reads_as_no_token(self, monkeypatch):
        from spi import images

        monkeypatch.setattr(
            images.urllib.request,
            "urlopen",
            lambda req, timeout=15: self._Response(body=b"[]"),
        )

        assert images._ghcr_pull_token("azure/storage") == ""


class TestForkPackageRepositories:
    def test_prefixed_repository_publishes_under_its_own_name_or_the_service(self):
        assert images.fork_package_repositories("Azure/osdu-spi-partition", "partition") == (
            "ghcr.io/azure/osdu-spi-partition",
            "ghcr.io/azure/partition",
        )

    def test_repository_named_after_the_service_yields_one_package(self):
        assert images.fork_package_repositories("danielscholl-osdu/partition", "partition") == (
            "ghcr.io/danielscholl-osdu/partition",
        )


class TestResolveGhcrTagDigest:
    class _Response:
        def __init__(self, body=b'{"token": "t"}', digest=""):
            self._body = body
            self.headers = {"Docker-Content-Digest": digest} if digest else {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self._body

    def test_returns_the_digest_behind_the_tag(self, monkeypatch):
        digest = "sha256:" + "e" * 64
        seen = []

        def fake_urlopen(req, timeout=15):
            seen.append((req.get_method(), req.full_url))
            if "/manifests/" in req.full_url:
                return self._Response(digest=digest)
            return self._Response()

        monkeypatch.setattr(images.urllib.request, "urlopen", fake_urlopen)

        assert (
            images.resolve_ghcr_tag_digest("ghcr.io/azure/schema-load", "sha-abc123def456")
            == digest
        )
        assert seen[1] == (
            "HEAD",
            "https://ghcr.io/v2/azure/schema-load/manifests/sha-abc123def456",
        )

    @pytest.mark.parametrize("code", [403, 404])
    def test_missing_package_or_tag_reads_as_not_published(self, monkeypatch, code):
        """GHCR answers 403 on the pull token for a package that does not exist and
        404 for a missing tag; neither is an error for a fork that ships no loader."""

        def fake_urlopen(req, timeout=15):
            if code == 403 or "/manifests/" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, code, "nope", Message(), None)
            return self._Response()

        monkeypatch.setattr(images.urllib.request, "urlopen", fake_urlopen)

        assert (
            images.resolve_ghcr_tag_digest("ghcr.io/azure/schema-load", "sha-abc123def456") is None
        )

    def test_server_errors_retry_then_fail_closed(self, monkeypatch):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=15):
            calls["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", Message(), None)

        monkeypatch.setattr(images.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(images.time, "sleep", lambda s: None)

        with pytest.raises(images.ImageResolutionError, match="unreachable after 2 attempts"):
            images.resolve_ghcr_tag_digest("ghcr.io/azure/schema-load", "sha-abc", attempts=2)
        assert calls["n"] == 2

    def test_a_missing_digest_header_fails_closed(self, monkeypatch):
        monkeypatch.setattr(
            images.urllib.request, "urlopen", lambda req, timeout=15: self._Response()
        )
        monkeypatch.setattr(images.time, "sleep", lambda s: None)

        with pytest.raises(images.ImageResolutionError, match="no manifest digest"):
            images.resolve_ghcr_tag_digest("ghcr.io/azure/schema-load", "sha-abc", attempts=1)


class TestResolveForkLoader:
    def test_loader_is_the_pinned_repository_plus_load_at_the_commit_tag(self, monkeypatch):
        seen = []

        def fake_tag(repository, tag):
            seen.append((repository, tag))
            return "sha256:" + "e" * 64

        monkeypatch.setattr(images, "resolve_ghcr_tag_digest", fake_tag)

        assert images.resolve_fork_loader("ghcr.io/azure/osdu-spi-schema", "b" * 40) == (
            "ghcr.io/azure/osdu-spi-schema-load",
            "sha256:" + "e" * 64,
        )
        assert seen == [("ghcr.io/azure/osdu-spi-schema-load", "sha-" + "b" * 12)]

    def test_no_loader_at_the_commit_is_none(self, monkeypatch):
        monkeypatch.setattr(images, "resolve_ghcr_tag_digest", lambda repository, tag: None)
        assert images.resolve_fork_loader("ghcr.io/azure/schema", "b" * 40) is None


SNAPSHOT = "sha256:" + "5" * 64
HEAD, BUILT = "c" * 40, "b" * 40


def _fork_registry(
    monkeypatch,
    tags: dict[tuple[str, str], str | None],
    created: dict[str, str] | None = None,
):
    """GHCR answers only the (repository, tag) pairs given; GitHub is never asked."""

    monkeypatch.setattr(
        images, "resolve_ghcr_tag_digest", lambda repository, tag: tags.get((repository, tag))
    )
    monkeypatch.setattr(
        images,
        "ghcr_tags",
        lambda repository: tuple(t for (r, t), d in tags.items() if r == repository and d),
    )
    monkeypatch.setattr(
        images, "ghcr_image_created", lambda repository, digest: (created or {}).get(digest, "")
    )

    def no_github(*args, **kwargs):
        raise AssertionError("fork images resolve from GHCR alone")

    monkeypatch.setattr(images, "_github_read", no_github)


class TestResolveForkImage:
    def test_main_snapshot_is_labelled_with_the_commit_that_built_it(self, monkeypatch):
        # A pull request build of a newer commit sits beside main's build in the package.
        _fork_registry(
            monkeypatch,
            {
                ("ghcr.io/acme/osdu-spi-partition", "main-snapshot"): SNAPSHOT,
                ("ghcr.io/acme/osdu-spi-partition", f"sha-{HEAD[:12]}"): "sha256:" + "9" * 64,
                ("ghcr.io/acme/osdu-spi-partition", f"sha-{BUILT[:12]}"): SNAPSHOT,
            },
            created={SNAPSHOT: "2026-09-21T00:00:00Z"},
        )

        image, commit = images.resolve_fork_image("partition", "Acme/osdu-spi-partition")

        assert commit == BUILT[:12]
        assert image == images.ResolvedImage(
            "partition",
            "ghcr.io/acme/osdu-spi-partition",
            f"sha-{BUILT[:12]}",
            "2026-09-21T00:00:00Z",
            SNAPSHOT,
        )

    def test_the_acceptance_image_the_commit_built_is_recorded_as_its_pair(self, monkeypatch):
        paired = "sha256:" + "a" * 64
        _fork_registry(
            monkeypatch,
            {
                ("ghcr.io/acme/fork", "main-snapshot"): SNAPSHOT,
                ("ghcr.io/acme/fork", f"sha-{BUILT[:12]}"): SNAPSHOT,
                ("ghcr.io/acme/fork-acceptance", "main-snapshot"): "sha256:" + "0" * 64,
                ("ghcr.io/acme/fork-acceptance", f"sha-{BUILT[:12]}"): paired,
            },
        )

        image, _ = images.resolve_fork_image("partition", "Acme/fork")

        assert image.acceptance_digest == paired

    def test_a_fork_publishing_under_the_service_name_resolves_there(self, monkeypatch):
        _fork_registry(
            monkeypatch,
            {
                ("ghcr.io/acme/fork", "main-snapshot"): None,
                ("ghcr.io/acme/partition", "main-snapshot"): SNAPSHOT,
                ("ghcr.io/acme/partition", f"sha-{HEAD[:12]}"): SNAPSHOT,
            },
        )

        image, _ = images.resolve_fork_image("partition", "Acme/fork")

        assert image.repository == "ghcr.io/acme/partition"

    def test_the_package_built_most_recently_wins(self, monkeypatch):
        # A SERVICE_NAME change leaves the repository-named package on an older snapshot.
        stale = "sha256:" + "e" * 64
        _fork_registry(
            monkeypatch,
            {
                ("ghcr.io/acme/fork", "main-snapshot"): stale,
                ("ghcr.io/acme/fork", f"sha-{BUILT[:12]}"): stale,
                ("ghcr.io/acme/partition", "main-snapshot"): SNAPSHOT,
                ("ghcr.io/acme/partition", f"sha-{HEAD[:12]}"): SNAPSHOT,
            },
            created={stale: "2026-09-20T00:00:00Z", SNAPSHOT: "2026-09-22T00:00:00Z"},
        )

        image, commit = images.resolve_fork_image("partition", "Acme/fork")

        assert (image.repository, commit) == ("ghcr.io/acme/partition", HEAD[:12])

    def test_no_main_snapshot_names_both_packages(self, monkeypatch):
        _fork_registry(monkeypatch, {})

        with pytest.raises(ImageResolutionError) as exc:
            images.resolve_fork_image("partition", "Acme/fork")

        assert "ghcr.io/acme/fork or ghcr.io/acme/partition" in str(exc.value)

    def test_a_snapshot_no_commit_tag_names_is_refused(self, monkeypatch):
        _fork_registry(
            monkeypatch,
            {
                ("ghcr.io/acme/fork", "main-snapshot"): SNAPSHOT,
                ("ghcr.io/acme/fork", f"sha-{HEAD[:12]}"): "sha256:" + "9" * 64,
            },
        )

        with pytest.raises(ImageResolutionError, match="matches no sha- tag"):
            images.resolve_fork_image("partition", "Acme/fork")


class TestGhcrPackageReads:
    def test_tags_follow_the_registry_next_link(self, monkeypatch):
        pages = {
            "/v2/acme/fork/tags/list?n=1000": (
                b'{"tags": ["main-snapshot", "sha-aaaaaaaaaaaa"]}',
                '</v2/acme/fork/tags/list?last=sha-aaaaaaaaaaaa&n=1000>; rel="next"',
            ),
            "/v2/acme/fork/tags/list?last=sha-aaaaaaaaaaaa&n=1000": (
                b'{"tags": ["sha-bbbbbbbbbbbb"]}',
                "",
            ),
        }
        monkeypatch.setattr(images, "_ghcr_get", lambda path, url_path, accept: pages[url_path])

        assert images.ghcr_tags("ghcr.io/acme/fork") == (
            "main-snapshot",
            "sha-aaaaaaaaaaaa",
            "sha-bbbbbbbbbbbb",
        )

    def test_created_reads_an_index_through_its_first_platform(self, monkeypatch):
        bodies = {
            "/v2/acme/fork/manifests/sha256:index": {"manifests": [{"digest": "sha256:amd64"}]},
            "/v2/acme/fork/manifests/sha256:amd64": {"config": {"digest": "sha256:config"}},
            "/v2/acme/fork/blobs/sha256:config": {"created": "2026-10-13T19:30:00Z"},
        }
        monkeypatch.setattr(
            images,
            "_ghcr_get",
            lambda path, url_path, accept: (json.dumps(bodies[url_path]).encode(), ""),
        )

        assert images.ghcr_image_created("ghcr.io/acme/fork", "sha256:index") == (
            "2026-10-13T19:30:00Z"
        )

    def test_an_unreadable_config_leaves_created_empty(self, monkeypatch):
        def refused(path, url_path, accept):
            raise ImageResolutionError("HTTP 404")

        monkeypatch.setattr(images, "_ghcr_get", refused)

        assert images.ghcr_image_created("ghcr.io/acme/fork", "sha256:gone") == ""


class TestResolveImagesUnderASourcePolicy:
    def test_a_service_with_no_recorded_source_follows_its_azure_repository(self, monkeypatch):
        repos = []
        monkeypatch.setattr(
            images,
            "resolve_fork_image",
            lambda name, repo: (
                repos.append(repo)
                or (images.ResolvedImage(name, "r", "sha-b", "", SNAPSHOT), BUILT)
            ),
        )

        images.resolve_images(names=("partition", "indexer-queue"))

        assert repos == ["Azure/osdu-spi-partition", "Azure/osdu-spi-indexer-queue"]

    def test_a_recorded_mirror_wins_over_azure(self, monkeypatch):
        repos = {}
        monkeypatch.setattr(
            images,
            "resolve_fork_image",
            lambda name, repo: (
                repos.update({name: repo})
                or (images.ResolvedImage(name, "r", "sha-b", "", SNAPSHOT), BUILT)
            ),
        )

        images.resolve_images(names=("partition", "legal"), sources={"legal": "Acme/legal"})

        assert repos == {"partition": "Azure/osdu-spi-partition", "legal": "Acme/legal"}

    def test_a_fork_sourced_schema_pairs_the_fork_loader_at_its_commit(self, monkeypatch):
        schema = images.ResolvedImage("schema", "ghcr.io/acme/schema", "sha-b", "d", SNAPSHOT)
        repos = []
        monkeypatch.setattr(
            images,
            "resolve_fork_image",
            lambda name, repo: repos.append(repo) or (schema, BUILT),
        )
        loaders = []
        monkeypatch.setattr(
            images,
            "resolve_fork_loader",
            lambda repository, commit: (
                loaders.append((repository, commit))
                or ("ghcr.io/acme/schema-load", "sha256:" + "e" * 64)
            ),
        )

        resolved = images.resolve_images(
            names=("schema", "schema-load"), sources={"schema": "Acme/schema"}
        )

        assert repos == ["Acme/schema"]
        assert loaders == [("ghcr.io/acme/schema", BUILT)]
        assert resolved["schema-load"] == images.ResolvedImage(
            "schema-load", "ghcr.io/acme/schema-load", "sha-b", "d", "sha256:" + "e" * 64
        )

    def test_a_schema_without_its_loader_names_the_repository(self, monkeypatch):
        schema = images.ResolvedImage("schema", "ghcr.io/acme/schema", "sha-b", "d", SNAPSHOT)
        monkeypatch.setattr(images, "resolve_fork_image", lambda name, repo: (schema, BUILT))
        monkeypatch.setattr(images, "resolve_fork_loader", lambda repository, commit: None)

        with pytest.raises(ImageResolutionError) as exc_info:
            images.resolve_images(names=("schema", "schema-load"), sources={"schema": "Acme/s"})

        assert "Acme/s published no loader ghcr.io/acme/schema-load:sha-b" in str(exc_info.value)
