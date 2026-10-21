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

"""Validate the reviewed catalog, then stage its complete set into the pod's emptyDir."""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import urlopen

import yaml

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_EXPANDED_BYTES = 64 * 1024 * 1024


class CatalogError(ValueError):
    """An input cannot be staged as the declared family."""


def relative_path(value: object, label: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
        or PurePosixPath(value).is_absolute()
    ):
        raise CatalogError(f"{label}: unsafe relative path {value!r}")
    return PurePosixPath(value)


def validate_catalog(catalog: object) -> list[dict]:
    if not isinstance(catalog, dict) or set(catalog) != {"families"}:
        raise CatalogError("catalog must contain only families")
    families = catalog["families"]
    if not isinstance(families, list) or not families:
        raise CatalogError("catalog families must be a non-empty list")
    names: set[str] = set()
    dag_ids: set[str] = set()
    for family in families:
        if not isinstance(family, dict) or set(family) != {"name", "source", "files", "dags"}:
            raise CatalogError("family requires name, source, files and dags")
        name = family["name"]
        if not isinstance(name, str) or not re.fullmatch(
            r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", name
        ):
            raise CatalogError(f"invalid family name {name!r}")
        if name in names:
            raise CatalogError(f"duplicate family name: {name}")
        names.add(name)
        source = family["source"]
        if not isinstance(source, dict) or source.get("type") not in ("local", "gitlab"):
            raise CatalogError(f"{name}: source.type must be local or gitlab")
        prefix = None
        if source["type"] == "gitlab":
            if set(source) != {"type", "url", "project", "commit", "strip_prefix"}:
                raise CatalogError(
                    f"{name}: gitlab source requires url, project, commit and strip_prefix"
                )
            commit = source["commit"]
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
                raise CatalogError(
                    f"{name}: commit must be a full 40-character SHA, not a tag or branch"
                )
            if not isinstance(source["url"], str):
                raise CatalogError(f"{name}: source.url must be an HTTPS GitLab origin")
            url = urlsplit(source["url"])
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in ("", "/")
            ):
                raise CatalogError(f"{name}: source.url must be an HTTPS GitLab origin")
            relative_path(source["project"], f"{name} project")
            prefix = relative_path(source["strip_prefix"], f"{name} strip_prefix")
        elif set(source) != {"type"}:
            raise CatalogError(f"{name}: local source takes only type")
        if not isinstance(family["files"], list) or not family["files"]:
            raise CatalogError(f"{name}: files must be a non-empty list")
        paths: set[PurePosixPath] = set()
        for entry in family["files"]:
            if not isinstance(entry, dict) or not {"path"} <= set(entry) <= {"path", "sha256"}:
                raise CatalogError(f"{name}: file requires path and optionally sha256")
            path = relative_path(entry["path"], f"{name} file")
            if prefix:
                if not path.is_relative_to(prefix) or path == prefix:
                    raise CatalogError(f"{name}/{path}: path is outside its family strip_prefix")
                path = path.relative_to(prefix)
            if path in paths:
                raise CatalogError(f"{name}/{path}: duplicate file path")
            paths.add(path)
            digest = entry.get("sha256")
            if source["type"] == "gitlab" or digest is not None:
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                    raise CatalogError(
                        f"{name}/{path}: sha256 must contain 64 hexadecimal characters"
                    )
        if not isinstance(family["dags"], list) or not family["dags"]:
            raise CatalogError(f"{name}: dags must be a non-empty list")
        for dag_id in family["dags"]:
            if not isinstance(dag_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,250}", dag_id):
                raise CatalogError(f"{name}: invalid DAG ID {dag_id!r}")
            if dag_id in dag_ids:
                raise CatalogError(f"duplicate DAG ID: {dag_id}")
            dag_ids.add(dag_id)
    return families


def declared_dags(catalog: object) -> set[str]:
    return {dag for family in validate_catalog(catalog) for dag in family["dags"]}


def fetch(url: str) -> bytes:
    with urlopen(url, timeout=60) as response:
        content = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(content) > MAX_ARCHIVE_BYTES:
        raise CatalogError("archive exceeds the 32MiB download limit")
    return content


def archive_files(content: bytes, family: dict) -> dict[str, bytes]:
    """Inspect every member before reading selected files; never extract an archive."""
    selected = {entry["path"] for entry in family["files"]}
    found: dict[str, bytes] = {}
    root = None
    seen: set[str] = set()
    expanded = 0
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        members = []
        for member in archive:
            if len(members) >= MAX_ARCHIVE_MEMBERS:
                raise CatalogError(f"{family['name']}: too many archive members")
            path = relative_path(member.name.rstrip("/"), f"{family['name']} archive member")
            if root is None:
                root = path.parts[0]
            if path.parts[0] != root or (not member.isdir() and len(path.parts) < 2):
                raise CatalogError(f"{member.name}: archive member is outside its family root")
            if not (member.isdir() or member.isfile()):
                raise CatalogError(f"{member.name}: links and special archive members are refused")
            if str(path) in seen:
                raise CatalogError(f"{member.name}: duplicate archive member")
            seen.add(str(path))
            if member.size > MAX_FILE_BYTES:
                raise CatalogError(f"{member.name}: archive member exceeds 4MiB")
            expanded += member.size
            if expanded > MAX_EXPANDED_BYTES:
                raise CatalogError(f"{member.name}: expanded archive exceeds 64MiB")
            members.append((member, PurePosixPath(*path.parts[1:]).as_posix()))
        for member, path in members:
            if path not in selected or not member.isfile():
                continue
            stream = archive.extractfile(member)
            if stream is None:
                raise CatalogError(f"{path}: cannot read archive member")
            found[path] = stream.read(MAX_FILE_BYTES + 1)
    missing = selected - found.keys()
    if missing:
        raise CatalogError(
            f"{family['name']}: files missing from archive: {', '.join(sorted(missing))}"
        )
    return found


def load(catalog: object, local_dir: Path, destination: Path) -> int:
    families = validate_catalog(catalog)
    pending: list[tuple[Path, bytes]] = []
    root = destination.resolve()
    if destination.is_symlink():
        raise CatalogError(f"{destination}: destination must not be a symlink")
    for family in families:
        name, source = family["name"], family["source"]
        revision = f"gitlab {source['commit'][:8]}" if source["type"] == "gitlab" else "local"
        print(f"family {name} ({revision})", flush=True)
        remote = {}
        if source["type"] == "gitlab":
            query = urlencode({"sha": source["commit"], "path": source["strip_prefix"]})
            url = (
                f"{source['url'].rstrip('/')}/api/v4/projects/{quote(source['project'], safe='')}"
                f"/repository/archive.tar.gz?{query}"
            )
            try:
                remote = archive_files(fetch(url), family)
            except Exception as exc:
                files = ", ".join(entry["path"] for entry in family["files"])
                raise CatalogError(f"{name} ({files}): {exc}") from exc
        for entry in family["files"]:
            source_path = PurePosixPath(entry["path"])
            if source["type"] == "gitlab":
                relative = source_path.relative_to(source["strip_prefix"])
                content = remote[entry["path"]]
            else:
                relative = source_path
                local_path = local_dir.joinpath(*relative.parts)
                if not local_path.resolve().is_relative_to(local_dir.resolve()):
                    raise CatalogError(f"{name}/{relative}: local file escapes its source")
                try:
                    content = local_path.read_bytes()
                except OSError as exc:
                    raise CatalogError(f"{name}/{relative}: {exc}") from exc
            if len(content) > MAX_FILE_BYTES:
                raise CatalogError(f"{name}/{relative}: file exceeds 4MiB")
            digest = entry.get("sha256")
            if digest and hashlib.sha256(content).hexdigest() != digest.lower():
                raise CatalogError(f"{name}/{relative}: sha256 mismatch")
            target = root / name / Path(*relative.parts)
            if not target.resolve().is_relative_to(root / name):
                raise CatalogError(f"{name}/{relative}: destination escapes its family")
            pending.append((target, content))
            print(f"  {name}/{relative}" + ("  sha256 ok" if digest else ""), flush=True)
    # The emptyDir belongs to this loader. Retries replace any interrupted write,
    # but only after every catalog path, archive member and digest has passed.
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for target, content in pending:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    print(f"loaded {len(pending)} DAG files into {destination}", flush=True)
    return len(pending)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path("/opt/spi/osdu/catalog.yaml"))
    parser.add_argument("--local-dir", type=Path, default=Path("/opt/spi/osdu"))
    parser.add_argument("--destination", type=Path, default=Path("/opt/airflow/dags"))
    args = parser.parse_args()
    try:
        catalog = yaml.safe_load(args.catalog.read_text(encoding="utf-8"))
        load(catalog, args.local_dir, args.destination)
    except Exception as exc:
        print(f"DAG load failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
