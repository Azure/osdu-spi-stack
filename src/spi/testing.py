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

"""Run one service suite as the commit the environment runs shipped it.

The CLI supplies facts, bearers, and the target. The descriptor, resolver,
and verdict script come from that commit (paired mode) or from a checkout
(``--source``); the CLI never reads the descriptor itself.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .console import console
from .images import (
    IMAGE_REGISTRY,
    SCHEMA_LOAD_SERVICE_NAME,
    ImageNotFoundError,
    ImageResolutionError,
    acceptance_digest_key,
    fork_package_repositories,
    github_file,
    github_get,
    image_lock_key,
    resolve_ghcr_manifest,
)
from .pins import (
    WORKLOAD_NAMESPACE,
    PinError,
    VerifyError,
    _kubectl_read_json,
    _lock_entry_keys,
    decode_canonical_sources,
    decode_pins,
    decode_trusted_repos,
    read_lock,
    require_deployable,
    verify_service_image,
)
from .shell import run_command, run_process

DESCRIPTOR_PATH = ".spi/service.yaml"
RESOLVER_PATH = ".github/actions/acceptance-resolver/resolve.py"
VERDICT_PATH = ".github/actions/acceptance-image/suite-verdict.py"
SETTINGS_PATH = ".mvn/community-maven.settings.xml"
MACHINERY = (DESCRIPTOR_PATH, RESOLVER_PATH, VERDICT_PATH)
REPORT_SCHEMA = 1
SUITE_PLATFORM = "linux/amd64"
# The acceptance image bakes each suite under its working directory, as the lane reads it.
IMAGE_SUITE_ROOT = "/suite"
REPORT_DIRS = ("surefire-reports", "failsafe-reports")
BEARERS = (
    ("deploy", "RESOLVER_TOKEN"),
    ("member", "RESOLVER_MEMBER_TOKEN"),
    ("no_access", "RESOLVER_NO_ACCESS_TOKEN"),
)
MAVEN_BASE_ENV = ("PATH", "HOME", "JAVA_HOME", "MAVEN_OPTS", "LANG", "TMPDIR")
# What any Windows process needs to start, and where mvn.cmd finds the user profile.
WINDOWS_PROCESS_ENV = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
WINDOWS_PROFILE_ENV = ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH")
_ON_WINDOWS = os.name == "nt"
# Kubernetes advances it on every pod template change, a rollback included.
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"

EXIT_PASSED = 0
EXIT_NOT_RUN = 1
EXIT_REFUSED = 2
EXIT_FAILED = 3

# Resolver exit code to (outcome code, spi test exit code).
_RESOLVER_EXITS = {
    2: ("descriptor_rejected", EXIT_NOT_RUN),
    3: ("env_not_ready", EXIT_REFUSED),
    4: ("facts_contradiction", EXIT_NOT_RUN),
}
_COMMIT_TAG_RE = re.compile(r"^sha-([0-9a-f]{12})$")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class SuiteNotRun(RuntimeError):
    """The suite did not run, or its result was discarded.

    ``exit_code`` is 2 when the environment or target was not in a state to
    test, 1 for anything else.
    """

    def __init__(self, code: str, detail: str, exit_code: int = EXIT_NOT_RUN):
        super().__init__(detail)
        self.code = code
        self.exit_code = exit_code


def _refused(code: str, detail: str) -> SuiteNotRun:
    return SuiteNotRun(code, detail, EXIT_REFUSED)


@dataclass(frozen=True)
class PairedTarget:
    """A fork canonical and the acceptance image resolution recorded with it."""

    source_repo: str
    repository: str
    commit: str
    acceptance_digest: str

    @property
    def acceptance_repository(self) -> str:
        return f"{self.repository}-acceptance"

    @property
    def acceptance_image(self) -> str:
        return f"{self.acceptance_repository}@{self.acceptance_digest}"


@dataclass(frozen=True)
class SuiteResult:
    """A suite that ran to a verdict against an unchanged target."""

    service: str
    suite: str
    mode: str
    label: str
    image: str
    commit: str
    passed: bool
    verdict: str
    tests: dict[str, int]

    @property
    def exit_code(self) -> int:
        return EXIT_PASSED if self.passed else EXIT_FAILED


def paired_target(lock: dict, service: str) -> tuple[PairedTarget | None, str]:
    """The fork canonical ``service`` runs and its recorded pair, or why it has none."""

    pin = decode_pins(lock).get(service)
    if pin is not None:
        return None, f"{service} runs a pinned image, not its canonical"
    source = decode_canonical_sources(lock).get(service, "")
    if not source:
        return None, f"{service} follows community images, which ship no acceptance image"
    trusted = decode_trusted_repos(lock).get(service, "")
    if trusted.lower() != source.lower():
        return None, f"{service} follows {source} but {trusted or 'no fork'} is trusted"
    data = lock.get("data") or {}
    key = image_lock_key(service)
    repository = data.get(f"{key}_IMAGE_REPOSITORY", "")
    tag = data.get(f"{key}_IMAGE_TAG", "")
    match = _COMMIT_TAG_RE.match(tag)
    if not match or repository.lower() not in fork_package_repositories(source, service):
        return None, (
            f"{service}'s lock entry ({repository}:{tag}) is not {source}'s canonical yet; "
            f"run 'spi service refresh {service}'"
        )
    pair = data.get(acceptance_digest_key(service), "")
    if not pair:
        return None, (
            f"{repository}:{tag} has no recorded acceptance image; the fork published none "
            f"for that commit, or the entry predates the pair ('spi service refresh {service}')"
        )
    return PairedTarget(source, repository, match.group(1), pair), ""


def target_state(lock: dict, service: str) -> dict:
    """The lock entry and pin a run is judged against."""

    data = lock.get("data") or {}
    keys = (*_lock_entry_keys(service), acceptance_digest_key(service))
    pin = decode_pins(lock).get(service)
    return {
        "entry": {key: data.get(key, "") for key in keys},
        "pin": asdict(pin) if pin else None,
    }


def _read_lock() -> dict:
    try:
        lock = read_lock()
    except PinError as exc:
        raise SuiteNotRun("cluster_unreadable", str(exc)) from exc
    assert lock is not None
    return lock


def _require_running(lock: dict, service: str) -> None:
    data = lock.get("data") or {}
    key = image_lock_key(service)
    reference = f"{data.get(f'{key}_IMAGE_REPOSITORY', '')}@{data.get(f'{key}_IMAGE_DIGEST', '')}"
    try:
        verify_service_image(service, reference)
    except VerifyError:
        raise
    except PinError as exc:
        raise SuiteNotRun("cluster_unreadable", str(exc)) from exc


def _deployment_revision(service: str) -> str:
    deployment = f"{WORKLOAD_NAMESPACE}-{service}"
    try:
        body = _kubectl_read_json(
            ["get", "deployment", deployment, "-n", WORKLOAD_NAMESPACE], f"Deployment {deployment}"
        )
    except PinError as exc:
        raise SuiteNotRun("cluster_unreadable", str(exc)) from exc
    annotations = ((body or {}).get("metadata") or {}).get("annotations") or {}
    return str(annotations.get(REVISION_ANNOTATION, ""))


def check_target(service: str, expected: dict, *, after: str = "", revision: str = "") -> str:
    """Refuse unless ``service`` still has the lock entry ``expected`` and runs it.

    ``after`` is the verdict of a suite that already ran, and ``revision`` the
    Deployment revision it ran against: a borrow and its restore leave the
    entry as it was but not the revision. A changed target discards the
    verdict. Returns the current revision.
    """

    lock = _read_lock()
    moment = "during the run" if after else "before the suite started"
    discarded = f"; the result ({after}) is discarded" if after else ""
    if target_state(lock, service) != expected:
        raise _refused(
            "target_changed", f"{service}'s image lock entry changed {moment}{discarded}."
        )
    try:
        _require_running(lock, service)
    except VerifyError as exc:
        if after:
            raise _refused(
                "target_changed", f"{service} stopped running its lock entry: {exc}{discarded}."
            ) from exc
        raise _refused(
            "not_deployable", f"{service} is not running its lock entry yet: {exc}"
        ) from exc
    current = _deployment_revision(service)
    if after and current != revision:
        raise _refused(
            "target_changed",
            f"{service} rolled out revision {current} during the run, after {revision}{discarded}.",
        )
    return current


def resolve_commit(target: PairedTarget) -> str:
    """The full SHA of the commit the lock's 12 characters name."""

    try:
        body = github_get(f"repos/{target.source_repo}/commits/{target.commit}")
    except ImageResolutionError as exc:
        raise SuiteNotRun("github_unavailable", str(exc)) from exc
    sha = str(body.get("sha", "")) if isinstance(body, dict) else ""
    # The endpoint also accepts a branch or tag named like the prefix.
    if not _FULL_SHA_RE.match(sha) or not sha.startswith(target.commit):
        raise SuiteNotRun(
            "commit_mismatch",
            f"{target.source_repo} resolves {target.commit} to {sha or 'nothing'}, "
            "not a commit with that prefix",
        )
    return sha


def fetch_machinery(source_repo: str, sha: str, into: Path) -> Path:
    """Write the commit's descriptor, resolver, and verdict script under ``into``."""

    for relative in MACHINERY:
        try:
            body = github_file(source_repo, relative, sha)
        except ImageResolutionError as exc:
            raise SuiteNotRun(
                "machinery_missing", f"{source_repo}@{sha[:12]} {relative}: {exc}"
            ) from exc
        path = into / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return into


def _require_pair_published(target: PairedTarget) -> None:
    try:
        resolve_ghcr_manifest(target.acceptance_repository, target.acceptance_digest)
    except ImageNotFoundError as exc:
        raise SuiteNotRun(
            "pair_pruned",
            f"{target.acceptance_image} no longer pulls ({exc}). Run 'spi service refresh' "
            f"onto a newer commit, or --source with a checkout of {target.commit}.",
        ) from exc
    except ImageResolutionError as exc:
        raise SuiteNotRun("registry_unavailable", str(exc)) from exc


def _git(checkout: Path, *args: str) -> str | None:
    result = run_process(
        ["git", "-C", str(checkout), *args], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def checkout_label(checkout: Path, target: PairedTarget | None) -> tuple[str, str]:
    """``(label, head)``: how a checkout relates to the commit the environment runs."""

    head = _git(checkout, "rev-parse", "HEAD") or ""
    if target is None:
        return "unpaired", head
    dirty = _git(checkout, "status", "--porcelain")
    matched = dirty == "" and head.startswith(target.commit)
    return ("matched" if matched else "unmatched"), head


def collect_facts() -> dict:
    from .bootstrap import ClusterConfigError
    from .deploy_record import DeployRecordError
    from .info import collect_info

    try:
        return collect_info()
    except (ClusterConfigError, DeployRecordError, PinError) as exc:
        raise SuiteNotRun("facts_unavailable", str(exc)) from exc


def mint_bearers() -> dict[str, str]:
    """The lane's three callers; an identity the environment lacks stays unset."""

    from .bootstrap import ClusterConfigError
    from .token import TokenError, mint_token

    bearers: dict[str, str] = {}
    for caller, variable in BEARERS:
        try:
            bearers[variable] = mint_token(caller=caller).access_token
        except (ClusterConfigError, TokenError) as exc:
            if caller == "deploy":
                raise SuiteNotRun("token_failed", f"could not mint the run bearer: {exc}") from exc
            console.print(f"  [warning]{variable} stays unset: {exc}[/warning]")
    return bearers


def resolve_suite(
    root: Path,
    suite: str,
    facts: Path,
    work: Path,
    bearers: Mapping[str, str],
    overrides: Mapping[str, str],
) -> tuple[dict, Path]:
    """Run the resolver in ``run`` mode; return the report's contract and the env file.

    The resolver lets an explicit variable win over every fact, so it sees
    only PATH, the bearers, and the caller's overrides (plus the Windows startup set).
    """

    env_file = work / "suite.env"
    report_path = work / "report.json"
    env = {**_host_env(("PATH",), WINDOWS_PROCESS_ENV), **bearers, **overrides}
    result = run_process(
        [
            sys.executable,
            str(root / RESOLVER_PATH),
            "--mode",
            "run",
            "--suite",
            suite,
            "--descriptor",
            str(root / DESCRIPTOR_PATH),
            "--facts",
            str(facts),
            "--env-file",
            str(env_file),
            "--report",
            str(report_path),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        report = {}
    if result.returncode != 0:
        error = report.get("error") or {}
        stderr = (result.stderr or "").strip().splitlines()
        detail = error.get("detail") or (stderr[-1] if stderr else "")
        code, exit_code = _RESOLVER_EXITS.get(result.returncode, ("resolver_failed", EXIT_NOT_RUN))
        raise SuiteNotRun(
            code,
            f"suite {suite} not run: {error.get('code') or f'exit {result.returncode}'}: {detail}",
            exit_code,
        )
    if report.get("report_schema") != REPORT_SCHEMA:
        raise SuiteNotRun(
            "report_schema",
            f"resolver report_schema {report.get('report_schema')!r} is not {REPORT_SCHEMA}",
        )
    return report.get("contract") or {}, env_file


def read_env_file(path: Path) -> dict[str, str]:
    """Parse the resolver's env file as ``NAME=VALUE`` data; it is never sourced."""

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep:
            values[name] = value
    return values


def _host_env(names: Sequence[str], windows: Sequence[str]) -> dict[str, str]:
    """The named host variables that are set, plus what Windows needs to start a process."""

    wanted = (*names, *windows) if _ON_WINDOWS else tuple(names)
    return {name: os.environ[name] for name in wanted if name in os.environ}


def _require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise SuiteNotRun(f"{name}_missing", f"{name} is not on PATH")


def run_paired(
    image: str,
    test_dir: str,
    env_file: Path,
    args: Sequence[str],
    timeout: int | None,
    reports: Path,
) -> int:
    """Run the suite in the paired image; copy its reports out before the container goes."""

    _require_tool("docker")
    pulled = run_command(
        ["docker", "pull", "--platform", SUITE_PLATFORM, image],
        capture_output=False,
        description="Pull the paired acceptance image",
        check=False,
    )
    if pulled.returncode != 0:
        raise SuiteNotRun("pull_failed", f"docker could not pull {image}")
    container = f"spi-test-{os.getpid()}"
    try:
        ran = run_command(
            [
                "docker",
                "run",
                "--name",
                container,
                "--platform",
                SUITE_PLATFORM,
                "--env-file",
                str(env_file),
                "-e",
                f"SUITE_DIR={test_dir}",
                image,
                *args,
            ],
            capture_output=False,
            description=f"Run {test_dir}",
            check=False,
            timeout=timeout,
        )
        copied = run_process(
            ["docker", "cp", f"{container}:{IMAGE_SUITE_ROOT}/{test_dir}", str(reports)],
            capture_output=True,
            text=True,
        )
        if copied.returncode != 0:
            reports.mkdir(parents=True, exist_ok=True)
    finally:
        # Also stops a container whose client the timeout killed.
        run_process(["docker", "rm", "-f", container], capture_output=True, text=True)
    return ran.returncode


def _clear_reports(suite_dir: Path) -> None:
    """Drop reports an earlier build left, so the verdict reads only this run's."""

    for path in list(suite_dir.rglob("*-reports")):
        if path.name in REPORT_DIRS and path.parent.name == "target" and path.is_dir():
            shutil.rmtree(path)


def run_checkout(
    checkout: Path, test_dir: str, env_file: Path, args: Sequence[str], timeout: int | None
) -> tuple[int, Path]:
    """Run the suite with the host's Maven, as the image's entrypoint would."""

    _require_tool("mvn")
    suite_dir = (checkout / test_dir).resolve()
    if not suite_dir.is_relative_to(checkout) or not suite_dir.is_dir():
        raise SuiteNotRun("descriptor_rejected", f"suite directory {test_dir} is not in {checkout}")
    _clear_reports(suite_dir)
    settings = checkout / SETTINGS_PATH
    command = ["mvn", "-B", "--no-transfer-progress"]
    if settings.is_file():
        command += ["--settings", str(settings)]
    env = _host_env(MAVEN_BASE_ENV, WINDOWS_PROCESS_ENV + WINDOWS_PROFILE_ENV)
    env.update(read_env_file(env_file))
    ran = run_command(
        [*command, *args],
        capture_output=False,
        description=f"Run {test_dir} natively",
        check=False,
        timeout=timeout,
        cwd=str(suite_dir),
        env=env,
    )
    return ran.returncode, suite_dir


def count_tests(reports: Path) -> dict[str, int]:
    """Totals across the Surefire and Failsafe reports, for display beside the verdict."""

    totals = {"tests": 0, "skipped": 0, "failures": 0, "errors": 0}
    for path in reports.rglob("TEST-*.xml"):
        if path.parent.name not in REPORT_DIRS:
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for key in totals:
            try:
                totals[key] += int(root.get(key, 0))
            except ValueError:
                pass
    return totals


def judge(root: Path, exit_code: int, reports: Path) -> tuple[bool, str]:
    """The commit's own verdict over this run's reports."""

    result = run_process(
        [
            sys.executable,
            str(root / VERDICT_PATH),
            "--exit-code",
            str(exit_code),
            "--reports",
            str(reports),
        ],
        env=_host_env(("PATH",), WINDOWS_PROCESS_ENV),
        capture_output=True,
        text=True,
    )
    lines = (result.stdout or result.stderr or "").strip().splitlines()
    return result.returncode == 0, (lines[-1] if lines else f"verdict exited {result.returncode}")


def _require_machinery(root: Path) -> None:
    missing = [relative for relative in MACHINERY if not (root / relative).is_file()]
    if missing:
        raise SuiteNotRun(
            "machinery_missing", f"{root} has no {', '.join(missing)}; is it a synced fork?"
        )


def run_suite(
    service: str,
    *,
    suite: str = "acceptance",
    checkout: Path | None = None,
    overrides: Mapping[str, str] | None = None,
    maven_arguments: Sequence[str] = (),
) -> SuiteResult:
    """Run ``suite`` of ``service`` against the environment; raise SuiteNotRun otherwise."""

    if service not in IMAGE_REGISTRY or service == SCHEMA_LOAD_SERVICE_NAME:
        known = ", ".join(sorted(n for n in IMAGE_REGISTRY if n != SCHEMA_LOAD_SERVICE_NAME))
        raise SuiteNotRun(
            "unknown_service", f"Unknown service {service!r}. Known services: {known}"
        )
    try:
        require_deployable()
    except PinError as exc:
        raise _refused("not_deployable", str(exc)) from exc

    lock = _read_lock()
    pin = decode_pins(lock).get(service)
    if pin is not None and pin.ephemeral:
        raise _refused(
            "service_borrowed",
            f"{service} is borrowed by run {pin.run_id} "
            f"({pin.source_run_url or pin.source_repo}); retry once the run restores it.",
        )
    expected = target_state(lock, service)
    target, unpaired = paired_target(lock, service)

    with tempfile.TemporaryDirectory(prefix="spi-test-") as scratch:
        work = Path(scratch)
        if checkout is None:
            if target is None:
                raise SuiteNotRun("unpaired", f"{unpaired}. Run it from a checkout with --source.")
            _require_pair_published(target)
            commit = resolve_commit(target)
            root = fetch_machinery(target.source_repo, commit, work / "commit")
            mode, label, image = "paired", "", target.acceptance_image
        else:
            root = checkout.resolve()
            _require_machinery(root)
            label, commit = checkout_label(root, target)
            mode, image = "checkout", ""

        facts = work / "facts.json"
        facts.write_text(json.dumps(collect_facts()), encoding="utf-8")
        contract, env_file = resolve_suite(
            root, suite, facts, work, mint_bearers(), dict(overrides or {})
        )
        test_dir = str(contract.get("test_dir", ""))
        args = list(maven_arguments) or list(contract.get("maven_arguments") or [])
        timeout = int(contract.get("timeout_minutes") or 0) * 60 or None

        revision = check_target(service, expected)
        try:
            if checkout is None:
                reports = work / "reports"
                code = run_paired(image, test_dir, env_file, args, timeout, reports)
            else:
                code, reports = run_checkout(root, test_dir, env_file, args, timeout)
        finally:
            env_file.unlink(missing_ok=True)
        passed, verdict = judge(root, code, reports)
        tests = count_tests(reports)

    check_target(service, expected, after=verdict, revision=revision)
    return SuiteResult(service, suite, mode, label, image, commit, passed, verdict, tests)
