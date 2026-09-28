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
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Collection, Mapping, Sequence

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
from .suite_report import REPORT_DIRS, secret_values

DESCRIPTOR_PATH = ".spi/service.yaml"
RESOLVER_PATH = ".github/actions/acceptance-resolver/resolve.py"
VERDICT_PATH = ".github/actions/acceptance-image/suite-verdict.py"
SETTINGS_PATH = ".mvn/community-maven.settings.xml"
MACHINERY = (DESCRIPTOR_PATH, RESOLVER_PATH, VERDICT_PATH)
REPORT_SCHEMA = 1
SUITE_PLATFORM = "linux/amd64"
# The acceptance image bakes each suite under its working directory, as the lane reads it.
IMAGE_SUITE_ROOT = "/suite"
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
    report: dict | None = None
    # Every suite the commit's descriptor declares, as its resolver reports them.
    declared: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return EXIT_PASSED if self.passed else EXIT_FAILED


def _fork_canonical(lock: dict, service: str) -> tuple[tuple[str, str, str] | None, str]:
    """``(source_repo, repository, commit)`` of the fork canonical ``service`` runs, or why not."""

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
    return (source, repository, match.group(1)), ""


def paired_target(lock: dict, service: str) -> tuple[PairedTarget | None, str]:
    """The fork canonical ``service`` runs and its recorded pair, or why it has none."""

    canonical, why = _fork_canonical(lock, service)
    if canonical is None:
        return None, why
    source, repository, commit = canonical
    tag = f"sha-{commit}"
    pair = (lock.get("data") or {}).get(acceptance_digest_key(service), "")
    if not pair:
        return None, (
            f"{repository}:{tag} has no recorded acceptance image; the fork published none "
            f"for that commit, or the entry predates the pair ('spi service refresh {service}')"
        )
    return PairedTarget(source, repository, commit, pair), ""


def deployed_fork_commit(lock: dict, service: str) -> str:
    """The 12-character commit a fork canonical names, whether or not it has a pair."""

    canonical, _ = _fork_canonical(lock, service)
    return canonical[2] if canonical else ""


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


def checkout_label(checkout: Path, deployed: str) -> tuple[str, str]:
    """``(label, head)``: how a checkout relates to ``deployed``, the fork commit running."""

    head = _git(checkout, "rev-parse", "HEAD") or ""
    if not deployed:
        return "unpaired", head
    dirty = _git(checkout, "status", "--porcelain")
    matched = dirty == "" and head.startswith(deployed)
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
    """The lane's three callers; an identity the environment lacks stays unset.

    Only absence is tolerated: a provisioned identity that fails to mint
    refuses the run rather than surfacing later as a binding not published.
    """

    from .bootstrap import ClusterConfigError, read_cluster_config
    from .token import CALLERS, TokenError, mint_token

    try:
        config = read_cluster_config()
    except ClusterConfigError as exc:
        raise SuiteNotRun("token_failed", f"could not read the test callers: {exc}") from exc
    bearers: dict[str, str] = {}
    for caller, variable in BEARERS:
        key = CALLERS[caller][0]
        if caller != "deploy" and not config.get(key):
            console.print(f"  [warning]{variable} stays unset: no {key} provisioned[/warning]")
            continue
        try:
            bearers[variable] = mint_token(caller=caller).access_token
        except (ClusterConfigError, TokenError) as exc:
            raise SuiteNotRun("token_failed", f"could not mint {variable}: {exc}") from exc
    return bearers


def resolve_suite(
    service: str,
    root: Path,
    suite: str,
    facts: Path,
    work: Path,
    bearers: Mapping[str, str],
    overrides: Mapping[str, str],
) -> tuple[dict, Path]:
    """Run the resolver in ``run`` mode; return the report's contract and the env file.

    The descriptor must name ``service``, or a checkout of another fork would
    run its suite under this service's guards.

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
    declared = report.get("service")
    if declared != service:
        raise SuiteNotRun(
            "descriptor_mismatch",
            f"the descriptor declares service {declared!r}, not {service!r}; "
            "point --source at the fork that builds it",
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
    container = f"spi-test-{uuid.uuid4().hex[:12]}"
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
            # A partial copy could hide the reports that recorded failures.
            shutil.rmtree(reports, ignore_errors=True)
            detail = (copied.stderr or "").strip() or f"exit {copied.returncode}"
            raise SuiteNotRun(
                "reports_unavailable",
                f"the suite ran (exit {ran.returncode}) but its reports could not be copied "
                f"out of {container}: {detail}; no verdict can be given",
            )
    except BaseException:
        _remove_container(container, strict=False)
        raise
    _remove_container(container)
    return ran.returncode


def _remove_container(container: str, *, strict: bool = True) -> None:
    """Remove the suite container, which a timeout leaves running with the suite's bearers.

    A failed removal refuses the run; ``strict=False`` only warns, for a run
    already failing with another error.
    """

    removed = run_process(["docker", "rm", "-f", container], capture_output=True, text=True)
    detail = (removed.stderr or "").strip()
    if removed.returncode == 0 or "no such container" in detail.lower():
        return
    message = (
        f"container {container} could not be removed ({detail or f'exit {removed.returncode}'})"
        f" and may still be running with the suite's bearers; run 'docker rm -f {container}'"
    )
    if not strict:
        console.print(f"  [warning]{message}[/warning]")
        return
    raise SuiteNotRun("cleanup_failed", message)


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
    lines = (result.stdout or "").strip().splitlines()
    # The verdict contract: exit 0 pass or 1 fail, with the verdict line printed.
    if result.returncode not in (0, 1) or not lines:
        stderr = (result.stderr or "").strip().splitlines()
        raise SuiteNotRun(
            "verdict_failed",
            f"{VERDICT_PATH} exited {result.returncode} without a verdict"
            + (f": {stderr[-1]}" if stderr else ""),
        )
    return result.returncode == 0, lines[-1]


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
    inspect: Callable[[Path, Collection[str]], dict | None] | None = None,
) -> SuiteResult:
    """Run ``suite`` of ``service`` against the environment; raise SuiteNotRun otherwise.

    ``inspect`` reads the suite directory and its reports before they are
    discarded, given the credentials the run carried so it can leave them out.
    """

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
            label, commit = checkout_label(root, deployed_fork_commit(lock, service))
            mode, image = "checkout", ""

        facts = work / "facts.json"
        facts.write_text(json.dumps(collect_facts()), encoding="utf-8")
        bearers = mint_bearers()
        contract, env_file = resolve_suite(
            service, root, suite, facts, work, bearers, dict(overrides or {})
        )
        secrets = secret_values(bearers, read_env_file(env_file)) if inspect else ()
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
        report = inspect(reports, secrets) if inspect else None

    check_target(service, expected, after=verdict, revision=revision)
    declared = tuple(contract.get("suites") or ())
    return SuiteResult(
        service, suite, mode, label, image, commit, passed, verdict, tests, report, declared
    )
