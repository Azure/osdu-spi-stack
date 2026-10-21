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

"""The init container validates the whole catalog before writing any DAG file."""

import copy
import hashlib
import importlib.util
import io
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from spi.shell import run_process

ROOT = Path(__file__).resolve().parents[1]
DAGS = ROOT / "software" / "stacks" / "osdu" / "dags"


@pytest.fixture(scope="module")
def loader() -> Any:
    spec = importlib.util.spec_from_file_location("spi_dag_loader", DAGS / "load.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def catalog() -> dict:
    return yaml.safe_load((DAGS / "catalog.yaml").read_text(encoding="utf-8"))


def archive(entries: dict[str, bytes], link: str = "") -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for name, content in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            tar.addfile(member, io.BytesIO(content))
        if link:
            member = tarfile.TarInfo(link)
            member.type = tarfile.SYMTYPE
            member.linkname = "../escape.py"
            tar.addfile(member)
    return stream.getvalue()


@pytest.fixture
def sources(catalog, tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    for entry in catalog["families"][0]["files"]:
        (local / entry["path"]).write_bytes(b"# local smoke\n")
    remote = {}
    for entry in catalog["families"][1]["files"]:
        content = f"# {entry['path']}\n".encode()
        entry["sha256"] = hashlib.sha256(content).hexdigest()
        remote[f"repository-commit/{entry['path']}"] = content
    return local, remote, tmp_path / "dags"


def test_catalog_declares_only_the_four_core_ids(loader, catalog):
    assert loader.declared_dags(catalog) == {
        "Echo_DAG",
        "airflow_monitoring",
        "Osdu_ingest",
        "manifest_ingestion",
    }
    assert catalog["families"][1]["source"]["commit"] == "193f921e377c4ec769ca20051f4a6c6a5ed3da20"


@pytest.mark.parametrize("commit", ["193f921e", "v0.30.0", "master", "g" * 40, None])
def test_remote_commit_is_full_and_not_a_tag_or_branch(loader, catalog, commit):
    catalog["families"][1]["source"]["commit"] = commit
    with pytest.raises(loader.CatalogError, match="full 40-character SHA"):
        loader.validate_catalog(catalog)


@pytest.mark.parametrize("digest", ["", "bad", "z" * 64, None])
def test_remote_file_requires_sha256(loader, catalog, digest):
    entry = catalog["families"][1]["files"][0]
    if digest is None:
        del entry["sha256"]
    else:
        entry["sha256"] = digest
    with pytest.raises(loader.CatalogError, match="osdu-ingest-r3.py.*sha256"):
        loader.validate_catalog(catalog)


def test_duplicate_family_is_refused(loader, catalog):
    catalog["families"].append(copy.deepcopy(catalog["families"][0]))
    with pytest.raises(loader.CatalogError, match="duplicate family"):
        loader.validate_catalog(catalog)


def test_duplicate_dag_id_is_refused_across_families(loader, catalog):
    catalog["families"][1]["dags"].append("Echo_DAG")
    with pytest.raises(loader.CatalogError, match="duplicate DAG ID: Echo_DAG"):
        loader.validate_catalog(catalog)


@pytest.mark.parametrize(
    "path",
    [
        "../escape.py",
        "/escape.py",
        r"C:\escape.py",
        r"src\osdu_dags\evil.py",
        "src/osdu_dags/../../escape.py",
        "src/elsewhere/escape.py",
        "src/osdu_dags//escape.py",
        "src/osdu_dags/./escape.py",
    ],
)
def test_file_path_cannot_leave_its_family(loader, catalog, path):
    catalog["families"][1]["files"][0]["path"] = path
    with pytest.raises(loader.CatalogError, match="path"):
        loader.validate_catalog(catalog)


def test_only_local_and_gitlab_sources_are_accepted(loader, catalog):
    catalog["families"][1]["source"]["type"] = "http"
    with pytest.raises(loader.CatalogError, match="local or gitlab"):
        loader.validate_catalog(catalog)


def test_loader_stages_only_declared_verified_files(loader, catalog, sources, monkeypatch, capsys):
    local, remote, destination = sources
    remote["repository-commit/src/osdu_dags/unreviewed.py"] = b"raise RuntimeError\n"
    urls = []

    def fetch(url):
        urls.append(url)
        return archive(remote)

    monkeypatch.setattr(loader, "fetch", fetch)
    assert loader.load(catalog, local, destination) == 4
    assert sorted(p.relative_to(destination).as_posix() for p in destination.rglob("*.py")) == [
        "core-ingestion/manifest_ingestion.py",
        "core-ingestion/osdu-ingest-r3.py",
        "spi-smoke/airflow_monitoring.py",
        "spi-smoke/echo_dag.py",
    ]
    assert "sha=193f921e377c4ec769ca20051f4a6c6a5ed3da20" in urls[0]
    output = capsys.readouterr().out
    assert "family core-ingestion (gitlab 193f921e)" in output
    assert "sha256 ok" in output
    assert "loaded 4 DAG files" in output


def test_tampered_file_stages_nothing_and_names_the_file(loader, catalog, sources, monkeypatch):
    local, remote, destination = sources
    remote["repository-commit/src/osdu_dags/osdu-ingest-r3.py"] += b"tampered"
    monkeypatch.setattr(loader, "fetch", lambda _: archive(remote))
    with pytest.raises(
        loader.CatalogError, match="core-ingestion/osdu-ingest-r3.py: sha256 mismatch"
    ):
        loader.load(catalog, local, destination)
    assert not destination.exists()


@pytest.mark.parametrize("path", ["root/../../escape.py", "/escape.py", r"root\..\escape.py"])
def test_archive_traversal_is_refused_before_any_write(loader, catalog, sources, monkeypatch, path):
    local, remote, destination = sources
    remote[path] = b"malicious"
    monkeypatch.setattr(loader, "fetch", lambda _: archive(remote))
    with pytest.raises(loader.CatalogError, match="unsafe relative path"):
        loader.load(catalog, local, destination)
    assert not destination.exists()
    assert not (destination.parent / "escape.py").exists()


def test_archive_symlinks_are_refused_even_if_not_selected(loader, catalog, sources, monkeypatch):
    local, remote, destination = sources
    monkeypatch.setattr(
        loader, "fetch", lambda _: archive(remote, "repository-commit/src/osdu_dags/link.py")
    )
    with pytest.raises(loader.CatalogError, match="link.py: links"):
        loader.load(catalog, local, destination)
    assert not destination.exists()


def test_missing_remote_file_and_fetch_failure_name_the_file(loader, catalog, sources, monkeypatch):
    local, remote, destination = sources
    remote.pop("repository-commit/src/osdu_dags/manifest_ingestion.py")
    monkeypatch.setattr(loader, "fetch", lambda _: archive(remote))
    with pytest.raises(loader.CatalogError, match="files missing.*manifest_ingestion.py"):
        loader.load(catalog, local, destination)
    assert not destination.exists()

    def fail(_):
        raise OSError("source unreachable")

    monkeypatch.setattr(loader, "fetch", fail)
    with pytest.raises(loader.CatalogError, match="osdu-ingest-r3.py.*source unreachable"):
        loader.load(catalog, local, destination)


def test_successful_retry_replaces_interrupted_contents(loader, catalog, sources, monkeypatch):
    local, remote, destination = sources
    destination.mkdir()
    (destination / "removed.py").write_text("old", encoding="utf-8")
    monkeypatch.setattr(loader, "fetch", lambda _: archive(remote))
    loader.load(catalog, local, destination)
    assert not (destination / "removed.py").exists()
    assert len(list(destination.rglob("*.py"))) == 4


def test_invalid_catalog_exits_one_without_creating_destination(tmp_path):
    path = tmp_path / "catalog.yaml"
    path.write_text("families: []", encoding="utf-8")
    result = run_process(
        [
            sys.executable,
            str(DAGS / "load.py"),
            "--catalog",
            str(path),
            "--destination",
            str(tmp_path / "dags"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "DAG load failed" in result.stderr
    assert not (tmp_path / "dags").exists()


def check_embedded(files: dict[str, str]) -> None:
    for name, content in files.items():
        if "${" in content:
            raise ValueError(f"{name}: Flux substitution opener in embedded content")


def test_embedded_file_with_substitution_opener_is_refused():
    with pytest.raises(ValueError, match="load.py: Flux substitution"):
        check_embedded({"load.py": "print('${UNSET}')"})


def test_embedded_payloads_contain_no_substitution_opener():
    spec = yaml.safe_load((DAGS / "kustomization.yaml").read_text(encoding="utf-8"))
    files = {}
    for entry in spec["configMapGenerator"][0]["files"]:
        name, _, path = entry.partition("=")
        files[name] = (DAGS / (path or name)).read_text(encoding="utf-8")
    check_embedded(files)


def test_requirements_lock_has_only_full_sha256_pins_and_no_extras():
    text = (DAGS / "requirements.txt").read_text(encoding="utf-8")
    assert "--extra-index" not in text
    assert "--hash=md5" not in text
    assert text.count("--find-links ") == 3
    for requirement in text.replace("\\\n", "").splitlines():
        if not requirement or requirement.startswith(("#", "--")):
            continue
        package, *hashes = requirement.split()
        assert "==" in package and "[" not in package
        assert hashes
        assert all(
            h.startswith("--hash=sha256:") and len(h.removeprefix("--hash=sha256:")) == 64
            for h in hashes
        )
