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

"""The facts of one suite run: what ran, what failed, and which tests check nothing.

They come from the run's Surefire and Failsafe reports and from the suite's
own sources. Nothing here decides the verdict.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

REPORT_DIRS = ("surefire-reports", "failsafe-reports")
FACTS_SCHEMA = 2
REPORT_FOLDER = "spi-reports"
REPORT_PREFIX = "spi-test-"
REPORT_MAX_AGE_DAYS = 7
DETAIL_LINES = 40
LINE_WIDTH = 300
# Shorter values are words a report also uses for something else.
SECRET_LENGTH = 8

_TYPE = re.compile(r"(?:^|\s)(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")
_EXTENDS = re.compile(r"\bextends\s+([\w$.]+)")
_METHOD = re.compile(r"([A-Za-z_$][\w$]*)\s*\(([^()]*)\)\s*(?:throws\s+[\w$.,\s]+)?$")
_ANNOTATION = re.compile(r"@\s*[\w$.]+")
_ANNOTATED = re.compile(r"@\s*[\w$.]+\s*\(")
_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_IMPORT = re.compile(r"^\s*import\s+([\w.]+?)(\.\*)?\s*;", re.M)
# A report name that is a method, with the parameters or index a runner appends.
_CASE = re.compile(r"^([A-Za-z_$][\w$]*)(?:[({]([^)}]*)[)}])?(?:\[.*)?$", re.S)

_SECRET_WORDS = r"token|secret|password|passwd|credential|api[_-]?key"
_SECRET_NAME = re.compile(f"(?i){_SECRET_WORDS}")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
_NAMED_SECRET = re.compile(
    rf"(?i)\b([\w.-]*(?:{_SECRET_WORDS})[\w.-]*)" r"(\"?\s*[:=]\s*\"?)([^\s\"'&,;]{6,})"
)


# --- the suite's sources ---------------------------------------------------


def _space(out: list[str], start: int, end: int) -> None:
    for at in range(start, end):
        if out[at] != "\n":
            out[at] = " "


def _blank(source: str) -> tuple[str, list[tuple[int, int]]]:
    """``source`` with comments and literals spaced out, and where the comments were."""

    out = list(source)
    comments: list[tuple[int, int]] = []
    i, n = 0, len(source)
    while i < n:
        if source.startswith("//", i):
            end = source.find("\n", i)
            end = n if end < 0 else end
            comments.append((i, end))
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            comments.append((i, end))
        elif source.startswith('"""', i):
            end = source.find('"""', i + 3)
            while end > 0 and source[end - 1] == "\\":
                end = source.find('"""', end + 1)
            end = n if end < 0 else end + 3
        elif source[i] in "\"'":
            end = i + 1
            while end < n and source[end] not in (source[i], "\n"):
                end += 2 if source[end] == "\\" else 1
            end = min(end + 1, n)
        else:
            i += 1
            continue
        _space(out, i, end)
        i = end
    return "".join(out), comments


def _closing(code: str, start: int, opener: str, closer: str) -> int:
    depth = 0
    for at in range(start, len(code)):
        if code[at] == opener:
            depth += 1
        elif code[at] == closer:
            depth -= 1
            if depth == 0:
                return at
    return len(code) - 1


def _without_arguments(code: str) -> str:
    """``code`` with each annotation's arguments spaced out, braces and all."""

    out = list(code)
    done = 0
    for match in _ANNOTATED.finditer(code):
        if match.start() >= done:
            done = _closing(code, match.end() - 1, "(", ")") + 1
            _space(out, match.end() - 1, done)
    return "".join(out)


def _without_generics(header: str) -> str:
    out: list[str] = []
    depth = 0
    for char in header:
        depth += char == "<"
        if not depth:
            out.append(char)
        depth -= char == ">" and depth > 0
    return "".join(out)


def _comment_text(comment: str) -> str:
    lines = [line.strip().strip("/*").strip() for line in comment.splitlines()]
    return " ".join(line for line in lines if line)


@dataclass(frozen=True)
class _Method:
    name: str
    arity: int
    empty: bool
    note: str


@dataclass
class _Type:
    """One class of a file, named by its path through the classes that enclose it."""

    name: str
    parent: str
    methods: list[_Method] = field(default_factory=list)


def _types(source: str) -> dict[str, _Type]:
    """The classes a file declares, each with the methods of its own body."""

    blanked, comments = _blank(source)
    code = _without_arguments(blanked)
    found: dict[str, _Type] = {}
    open_types: list[str] = []
    i = start = 0
    while i < len(code):
        if code[i] == ";":
            start = i + 1
        elif code[i] == "}":
            if open_types:
                open_types.pop()
            start = i + 1
        elif code[i] == "{":
            header = _without_generics(_ANNOTATION.sub(" ", code[start:i]))
            declared = _TYPE.search(header)
            if declared:
                open_types.append(declared.group(2))
                extends = _EXTENDS.search(header, declared.end())
                parent = extends.group(1) if extends and declared.group(1) == "class" else ""
                found["$".join(open_types)] = _Type("$".join(open_types), parent)
                start = i + 1
            else:
                end = _closing(code, i, "{", "}")
                match = _METHOD.search(header.strip())
                if match and open_types:
                    notes = [_comment_text(source[a:b]) for a, b in comments if i < a and b <= end]
                    arity = len(match.group(2).split(",")) if match.group(2).strip() else 0
                    empty = not code[i + 1 : end].strip()
                    method = _Method(match.group(1), arity, empty, " ".join(n for n in notes if n))
                    found["$".join(open_types)].methods.append(method)
                i = end
                start = end + 1
        i += 1
    return found


@dataclass(frozen=True)
class _JavaFile:
    path: Path
    package: str
    imports: tuple[str, ...]
    wildcards: tuple[str, ...]
    types: Mapping[str, _Type]


@dataclass(frozen=True)
class Declaration:
    """The body that runs for a reported test, or the parent outside the suite that holds it."""

    declared_in: str
    empty: bool
    note: str
    outside: str = ""


def suite_files(root: Path, pattern: str) -> list[Path]:
    """The suite's own files of a name, in order.

    A file a link leads to outside the suite is not the suite's own: a suite
    copied out of an image can hold a link that names a file of the host.
    """

    try:
        inside = root.resolve()
    except OSError:
        return []
    found = []
    for path in sorted(root.rglob(pattern)):
        try:
            if path.is_file() and path.resolve().is_relative_to(inside):
                found.append(path)
        except OSError:
            pass
    return found


def suite_sources(root: Path, suffixes: Iterable[str]) -> list[Path]:
    """The suite's source files of these suffixes, outside any build output."""

    try:
        inside = root.resolve()
    except OSError:
        return []
    # Where the file lies decides it: a link among the sources can name a report.
    return [
        path
        for suffix in suffixes
        for path in suite_files(root, f"*{suffix}")
        if "target" not in path.resolve().relative_to(inside).parts
    ]


class Sources:
    """The Java sources under a suite directory, read on demand."""

    def __init__(self, root: Path):
        self._by_name: dict[str, list[Path]] = {}
        for path in suite_sources(root, (".java",)):
            self._by_name.setdefault(path.stem, []).append(path)
        self._parsed: dict[Path, _JavaFile] = {}

    def _parse(self, path: Path) -> _JavaFile:
        if path not in self._parsed:
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                source = ""
            code, _ = _blank(source)
            package = _PACKAGE.search(code)
            imports = _IMPORT.findall(code)
            self._parsed[path] = _JavaFile(
                path,
                package.group(1) if package else "",
                tuple(name for name, wildcard in imports if not wildcard),
                tuple(name for name, wildcard in imports if wildcard),
                _types(source),
            )
        return self._parsed[path]

    def _in(self, top: str, packages: Iterable[str]) -> list[_JavaFile]:
        wanted = set(packages)
        found = [self._parse(path) for path in self._by_name.get(top, [])]
        return [java for java in found if java.package in wanted]

    def _parent(self, java: _JavaFile, child: _Type) -> tuple[_JavaFile, _Type] | str | None:
        """The parent's class, the parent's name when the suite does not hold it, or None."""

        parts = child.parent.split(".")
        local = [kind for key, kind in java.types.items() if key.split("$")[-len(parts) :] == parts]
        if local:
            return (java, local[0]) if len(local) == 1 else None
        # By convention a package starts lower case and a class does not.
        classes = [part for part in parts if not part[:1].islower()]
        package = ".".join(parts[: len(parts) - len(classes)])
        if not classes:
            return None
        imported = [line for line in java.imports if line.endswith(f".{classes[0]}")]
        if package:
            files = self._in(classes[0], [package])
        elif imported:
            files = self._in(classes[0], [line.rpartition(".")[0] for line in imported])
        else:
            files = self._in(classes[0], [java.package, *java.wildcards])
        if not files:
            return parts[-1]
        kind = files[0].types.get("$".join(classes)) if len(files) == 1 else None
        return (files[0], kind) if kind is not None else None

    def declaration(self, classname: str, case: str) -> Declaration | None:
        """The most derived body of a reported test, or None when the sources cannot say.

        A body is the reported class's own, or one it inherits, with the
        reported number of parameters. A test reached through a parent the
        suite does not hold comes back naming that parent.
        """

        match = _CASE.match(case)
        if match is None:
            return None
        name, listed = match.group(1), (match.group(2) or "").strip()
        arity = len(listed.split(",")) if listed else 0
        package, _, simple = classname.rpartition(".")
        files = self._in(simple.split("$", 1)[0], [package])
        java = files[0] if len(files) == 1 else None
        kind = java.types.get(simple) if java else None
        seen: set[tuple[Path, str]] = set()
        while java is not None and kind is not None and (java.path, kind.name) not in seen:
            seen.add((java.path, kind.name))
            bodies = [m for m in kind.methods if (m.name, m.arity) == (name, arity)]
            if bodies:
                if len(bodies) > 1:
                    return None
                return Declaration(kind.name.rpartition("$")[2], bodies[0].empty, bodies[0].note)
            if not kind.parent:
                return None
            parent = self._parent(java, kind)
            if isinstance(parent, str):
                return Declaration("", False, "", parent)
            java, kind = parent if parent else (None, None)
        return None


# --- the run's reports -----------------------------------------------------


def secret_values(*sources: Mapping[str, str]) -> tuple[str, ...]:
    """Values a run carried under a name that reads as a credential."""

    return tuple(
        value for source in sources for name, value in source.items() if _SECRET_NAME.search(name)
    )


def redactor(secrets: Iterable[str] = (), *, named: bool = True) -> Callable[[str], str]:
    """Remove the run's credentials, and with ``named`` any value a credential's name holds."""

    known = sorted(
        {secret for secret in secrets if len(secret) >= SECRET_LENGTH}, key=len, reverse=True
    )

    def redact(text: str) -> str:
        for secret in known:
            text = text.replace(secret, "[redacted]")
        text = _BEARER.sub("Bearer [redacted]", text)
        text = _JWT.sub("[redacted]", text)
        if named:
            text = _NAMED_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
        return text

    return redact


def _seconds(value: str | None) -> float:
    try:
        return float((value or "0").replace(",", ""))
    except ValueError:
        return 0.0


def _excerpt(text: str, *, tail: bool = False) -> str:
    lines = [line.rstrip()[:LINE_WIDTH] for line in text.strip().splitlines()]
    if len(lines) > DETAIL_LINES:
        lines = ["...", *lines[-DETAIL_LINES:]] if tail else [*lines[:DETAIL_LINES], "..."]
    return "\n".join(lines)


def _case(case: ET.Element, classname: str, sources: Sources, redact: Callable[[str], str]) -> dict:
    name = case.get("name", "")
    seconds = _seconds(case.get("time"))
    test: dict = {"name": redact(name), "seconds": seconds, "status": "passed"}
    for tag, status in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
        node = case.find(tag)
        if node is None:
            continue
        test["status"] = status
        test["message"] = redact((node.get("message") or "").strip())[: LINE_WIDTH * 2]
        if status != "skipped":
            test["type"] = redact(node.get("type", ""))
            # A credential cut by the excerpt would no longer read as one.
            test["detail"] = _excerpt(redact(node.text or ""))
            output = "\n".join(case.findtext(tag) or "" for tag in ("system-out", "system-err"))
            test["output"] = _excerpt(redact(output), tail=True)
        return test
    declaration = sources.declaration(classname, name)
    if declaration is not None and declaration.empty:
        # A comment is the suite's text, and it goes onto the page like the report's.
        note, declared_in = redact(declaration.note), redact(declaration.declared_in)
        test.update(status="empty", note=note, declared_in=declared_in)
    elif declaration is not None and declaration.outside:
        test["outside"] = redact(declaration.outside)
    return test


def collect(reports: Path, secrets: Iterable[str] = ()) -> dict:
    """The facts of a run: every reported test, and the passes whose body holds no statement."""

    sources = Sources(reports)
    redact = redactor(secrets)
    classes: dict[str, dict] = {}
    for path in suite_files(reports, "TEST-*.xml"):
        if path.parent.name not in REPORT_DIRS:
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for suite in [root] if root.tag == "testsuite" else root.iter("testsuite"):
            for case in suite.findall("testcase"):
                classname = case.get("classname") or suite.get("name", "")
                entry = classes.setdefault(classname, {"name": redact(classname), "tests": []})
                entry["tests"].append(_case(case, classname, sources, redact))
    totals = {key: 0 for key in ("tests", "passed", "empty", "failed", "error", "skipped")}
    seconds = 0.0
    for entry in classes.values():
        entry["seconds"] = round(sum(test["seconds"] for test in entry["tests"]), 3)
        seconds += entry["seconds"]
        for test in entry["tests"]:
            totals["tests"] += 1
            totals[test["status"]] += 1
    return {
        "schema": FACTS_SCHEMA,
        "totals": {**totals, "seconds": round(seconds, 3)},
        "classes": [classes[name] for name in sorted(classes)],
    }


def named_tests(facts: dict) -> list[tuple[str, dict]]:
    """Each test a suite reported beside the name a page and a reviewer know it by.

    That is ``Class.method`` with the class's simple name, or its full name
    where two classes of the suite share the simple one.
    """

    classes = [entry["name"] for entry in facts.get("classes", [])]
    simple = [name.rpartition(".")[2] for name in classes]
    return [
        (f"{name if simple.count(short) > 1 else short}.{test['name']}", test)
        for name, short, entry in zip(classes, simple, facts.get("classes", []))
        for test in entry["tests"]
    ]


# --- the file --------------------------------------------------------------


def _own(folder: Path) -> bool:
    """Whether ``folder`` is a real directory only this user could have written to."""

    try:
        held = folder.lstat()
    except OSError:
        return False
    # A shared temporary directory lets anyone create the name first, and a
    # folder others could write to can hold links they left in it.
    mine = not hasattr(os, "getuid") or (held.st_uid == os.getuid() and not held.st_mode & 0o022)
    return mine and folder.is_dir() and not folder.is_symlink()


def report_folder() -> Path:
    """A folder of this user's own under the system temporary directory."""

    folder = Path(tempfile.gettempdir()) / REPORT_FOLDER
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        if _own(folder):
            # A folder made before this one was may be readable by other users.
            folder.chmod(0o700)
            return folder
    except OSError:
        pass
    return Path(tempfile.mkdtemp(prefix=f"{REPORT_FOLDER}-"))


def write(page: str, folder: Path, *names: str, now: float | None = None) -> Path:
    """Write the page over the last one of these names; drop the reports a week old."""

    cutoff = (time.time() if now is None else now) - REPORT_MAX_AGE_DAYS * 86400
    for old in folder.glob(f"{REPORT_PREFIX}*.html"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    name = "-".join(re.sub(r"[^A-Za-z0-9._+]+", "_", part) for part in names)
    path = folder / f"{REPORT_PREFIX}{name}.html"
    # Text from a run or a reviewer can hold what UTF-8 cannot encode.
    path.write_text(page, encoding="utf-8", errors="replace")
    return path


def saved_pages(folder: Path) -> list[Path]:
    """The reports in ``folder``, oldest first."""

    try:
        pages = [(path.stat().st_mtime, path) for path in folder.glob(f"{REPORT_PREFIX}*.html")]
    except OSError:
        return []
    return [path for _, path in sorted(pages)]


def show(path: Path) -> bool:
    """Open the page in the browser when someone is there to look at it."""

    if os.environ.get("CI") or not sys.stdout.isatty():
        return False
    import webbrowser

    try:
        return webbrowser.open(path.as_uri())
    except webbrowser.Error:
        return False
