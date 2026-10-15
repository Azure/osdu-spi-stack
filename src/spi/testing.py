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

import base64
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Collection, Iterable, Mapping, Sequence

from rich.markup import escape

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
    LOCAL_ORIGIN,
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
from .suite_report import REPORT_DIRS, redactor, secret_name, secret_values

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
_ENV_REFERENCE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


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


@dataclass(frozen=True)
class SuitePlan:
    """What a run of a suite would start, bound and checked without starting it."""

    service: str
    suite: str
    mode: str
    label: str
    image: str
    commit: str
    # Where the command runs: on the host from a checkout, inside the image when paired.
    directory: str
    # As it may be displayed, like ``shown``: an argument's credential is left out.
    command: tuple[str, ...]
    timeout_minutes: int
    variables: dict[str, str]
    # The variables as they may be displayed: no credential, whole or inside a value.
    shown: dict[str, str]
    expires: str
    declared: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Binding:
    """A suite bound to the environment, with the target state it was bound against."""

    mode: str
    label: str
    image: str
    commit: str
    root: Path
    contract: dict
    env_file: Path
    bearers: dict[str, str]
    expected: dict

    @property
    def test_dir(self) -> str:
        return str(self.contract.get("test_dir", ""))

    @property
    def timeout_minutes(self) -> int:
        return int(self.contract.get("timeout_minutes") or 0)

    @property
    def test_entrypoint(self) -> str:
        return str(self.contract.get("test_entrypoint") or "")

    @property
    def report_paths(self) -> tuple[str, ...]:
        return tuple(self.contract.get("report_paths") or ())

    @property
    def test_type(self) -> str:
        return str(self.contract.get("test_type") or "maven")

    def arguments(
        self, maven_arguments: Sequence[str], variables: Mapping[str, str] | None = None
    ) -> list[str]:
        if self.contract.get("test_type") == "script":
            return _resolve_arguments(
                self.contract.get("test_arguments") or [],
                variables or {},
            )
        return list(maven_arguments) or list(self.contract.get("maven_arguments") or [])


def _resolve_arguments(arguments: Sequence[str], variables: Mapping[str, str]) -> list[str]:
    """Resolve script argument placeholders from the generated suite environment."""

    resolved = []
    for argument in arguments:
        missing = [name for name in _ENV_REFERENCE_RE.findall(argument) if name not in variables]
        if missing:
            raise SuiteNotRun(
                "resolver_failed",
                f"script argument references unset environment variable {missing[0]}",
            )
        resolved.append(
            _ENV_REFERENCE_RE.sub(lambda match: variables[match.group(1)], str(argument))
        )
    return resolved


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
    """The commit the running image names: a fork canonical's 12 characters, or the
    checkout a local pin was built from. A dirty build names no commit a checkout can match."""

    pin = decode_pins(lock).get(service)
    if pin is not None and pin.origin == LOCAL_ORIGIN:
        return pin.source_sha
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
        # The resolver held these credentials, so what it says of a failure may quote one.
        redact = redactor(secret_values(bearers, overrides))
        detail = redact(error.get("detail") or (stderr[-1] if stderr else ""))
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


def write_env_file(path: Path, variables: Mapping[str, str]) -> None:
    """Write ``variables`` as ``NAME=VALUE`` lines, owner-only where files have modes."""

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        # An existing file keeps its mode through the open.
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            os.chmod(path, 0o600)
        handle.writelines(f"{name}={value}\n" for name, value in variables.items())


def _expiry(tokens: Iterable[str]) -> str:
    """When the first of ``tokens`` to expire does, for those that are JWTs."""

    moments = []
    for token in tokens:
        parts = token.split(".")
        if len(parts) != 3:
            continue
        try:
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            moments.append(datetime.fromtimestamp(int(claims["exp"]), timezone.utc))
        except (ValueError, KeyError, TypeError, OverflowError, OSError):
            continue
    return min(moments).strftime("%Y-%m-%d %H:%M UTC") if moments else ""


def _host_env(names: Sequence[str], windows: Sequence[str]) -> dict[str, str]:
    """The named host variables that are set, plus what Windows needs to start a process."""

    wanted = (*names, *windows) if _ON_WINDOWS else tuple(names)
    return {name: os.environ[name] for name in wanted if name in os.environ}


def _require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise SuiteNotRun(f"{name}_missing", f"{name} is not on PATH")


def _docker_run(
    image: str,
    test_dir: str,
    env_file: str,
    args: Sequence[str],
    container: str = "",
    entrypoint: str = "",
) -> list[str]:
    name = ["--name", container] if container else []
    script = ["-e", f"SUITE_ENTRYPOINT={entrypoint}"] if entrypoint else []
    return [
        "docker",
        "run",
        *name,
        "--platform",
        SUITE_PLATFORM,
        "--env-file",
        env_file,
        "-e",
        f"SUITE_DIR={test_dir}",
        *script,
        image,
        *args,
    ]


def run_paired(
    image: str,
    test_dir: str,
    env_file: Path,
    args: Sequence[str],
    timeout: int | None,
    reports: Path,
    entrypoint: str = "",
    *,
    secrets: Collection[str] = (),
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
        command = _docker_run(image, test_dir, str(env_file), args, container, entrypoint)
        ran = run_command(
            command,
            capture_output=False,
            display_cmd=list(map(redactor(secrets), command)),
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


def _suite_dir(checkout: Path, test_dir: str) -> Path:
    suite_dir = (checkout / test_dir).resolve()
    if not suite_dir.is_relative_to(checkout) or not suite_dir.is_dir():
        raise SuiteNotRun("descriptor_rejected", f"suite directory {test_dir} is not in {checkout}")
    return suite_dir


def _validate_report_patterns(report_paths: Sequence[str]) -> tuple[str, ...]:
    patterns = tuple(str(pattern) for pattern in report_paths)
    for pattern in patterns:
        path = Path(pattern)
        posix_path = PurePosixPath(pattern.replace("\\", "/"))
        if path.is_absolute() or posix_path.is_absolute() or ".." in posix_path.parts:
            raise SuiteNotRun(
                "descriptor_rejected",
                f"report path {pattern!r} must stay beneath the suite directory",
            )
    return patterns


def _report_files(reports: Path, report_paths: Sequence[str]) -> list[Path]:
    patterns = _validate_report_patterns(report_paths)
    if not patterns:
        return [path for path in reports.rglob("TEST-*.xml") if path.parent.name in REPORT_DIRS]
    root = reports.resolve()
    paths = sorted({path for pattern in patterns for path in reports.glob(pattern)})
    for path in paths:
        if not path.resolve().is_relative_to(root):
            raise SuiteNotRun(
                "descriptor_rejected",
                f"report path {path} resolves outside the suite directory",
            )
    return paths


def _maven(checkout: Path, args: Sequence[str]) -> list[str]:
    settings = checkout / SETTINGS_PATH
    command = ["mvn", "-B", "--no-transfer-progress"]
    if settings.is_file():
        command += ["--settings", str(settings)]
    return [*command, *args]


def _bash_executable() -> str:
    bash = shutil.which("bash") or "bash"
    if not _is_wsl_bash(bash):
        return bash

    git = PureWindowsPath(shutil.which("git") or "")
    git_bash = git.parent.parent / "bin" / "bash.exe"
    return str(git_bash) if git.name.lower() == "git.exe" and os.path.isfile(git_bash) else bash


def _is_wsl_bash(bash: str) -> bool:
    launcher = PureWindowsPath(bash)
    return launcher.name.lower() == "bash.exe" and launcher.parent.name.lower() == "system32"


def _bash_path(target: Path, bash: str) -> str:
    path = target.as_posix()
    if _is_wsl_bash(bash) and re.match(r"^[A-Za-z]:/", path):
        return f"/mnt/{path[0].lower()}{path[2:]}"
    return path


def _script(checkout: Path, entrypoint: str, args: Sequence[str]) -> list[str]:
    path = Path(entrypoint)
    if path.is_absolute() or ".." in path.parts:
        raise SuiteNotRun(
            "descriptor_rejected",
            f"script entrypoint {entrypoint!r} must stay beneath the suite directory",
        )
    target = (checkout / path).resolve()
    if not target.is_relative_to(checkout.resolve()) or not target.is_file():
        raise SuiteNotRun(
            "descriptor_rejected",
            f"script entrypoint {entrypoint!r} is not a file in {checkout}",
        )
    if _ON_WINDOWS and target.suffix == ".sh":
        bash = _bash_executable()
        command = [bash, _bash_path(target, bash)]
    else:
        command = [str(target)]
    return [*command, *args]


def run_checkout(
    checkout: Path,
    test_dir: str,
    env_file: Path,
    args: Sequence[str],
    timeout: int | None,
    *,
    test_type: str = "maven",
    entrypoint: str = "",
    report_paths: Sequence[str] = (),
    secrets: Collection[str] = (),
) -> tuple[int, Path]:
    """Run a Maven or script suite from its checkout."""

    suite_dir = _suite_dir(checkout, test_dir)
    for report in _report_files(suite_dir, report_paths):
        if report.is_file():
            report.unlink()
    _clear_reports(suite_dir)
    if test_type == "script":
        command = _script(suite_dir, entrypoint, args)
    else:
        _require_tool("mvn")
        command = _maven(checkout, args)
    env = _host_env(MAVEN_BASE_ENV, WINDOWS_PROCESS_ENV + WINDOWS_PROFILE_ENV)
    env.update(read_env_file(env_file))
    if (
        _ON_WINDOWS
        and test_type == "script"
        and Path(entrypoint).suffix == ".sh"
        and not _is_wsl_bash(command[0])
    ):
        env["MSYS2_ARG_CONV_EXCL"] = "*"
    ran = run_command(
        command,
        capture_output=False,
        display_cmd=list(map(redactor(secrets), command)),
        description=f"Run {test_dir} natively",
        check=False,
        timeout=timeout,
        cwd=str(suite_dir),
        env=env,
    )
    return ran.returncode, suite_dir


def count_tests(reports: Path, report_paths: Sequence[str] = ()) -> dict[str, int]:
    """Totals across the suite's declared JUnit reports, or Maven's standard reports."""

    totals = {"tests": 0, "skipped": 0, "failures": 0, "errors": 0}
    for path in _report_files(reports, report_paths):
        if not path.is_file():
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        nodes = [root]
        if not any(root.get(key) is not None for key in totals):
            child_suites = [child for child in root if child.tag.rsplit("}", 1)[-1] == "testsuite"]
            nodes = child_suites or nodes
        for node in nodes:
            for key in totals:
                try:
                    totals[key] += int(node.get(key, 0))
                except ValueError:
                    pass
    return totals


def _run_verdict(root: Path, exit_code: int, reports: Path) -> tuple[bool, str]:
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


def _copy_verdict_report(source: Path, destination: Path) -> None:
    try:
        tree = ET.parse(source)
    except ET.ParseError:
        shutil.copy2(source, destination)
        return
    root = tree.getroot()
    totals = ("tests", "skipped", "failures", "errors")
    if any(root.get(key) is not None for key in totals):
        shutil.copy2(source, destination)
        return
    child_suites = [child for child in root if child.tag.rsplit("}", 1)[-1] == "testsuite"]
    try:
        for key in totals:
            root.set(key, str(sum(int(child.get(key, 0)) for child in child_suites)))
    except ValueError:
        shutil.copy2(source, destination)
        return
    tree.write(destination, encoding="utf-8", xml_declaration=True)


def judge(
    root: Path,
    exit_code: int,
    reports: Path,
    report_paths: Sequence[str] = (),
) -> tuple[bool, str]:
    """The commit's own verdict over this run's declared reports."""

    if not report_paths:
        return _run_verdict(root, exit_code, reports)
    with tempfile.TemporaryDirectory(prefix="spi-verdict-") as temporary:
        selected = Path(temporary) / REPORT_DIRS[0]
        selected.mkdir()
        for index, report in enumerate(_report_files(reports, report_paths)):
            if report.is_file():
                _copy_verdict_report(report, selected / f"TEST-{index:04d}.xml")
        return _run_verdict(root, exit_code, Path(temporary))


def _require_machinery(root: Path) -> None:
    missing = [relative for relative in MACHINERY if not (root / relative).is_file()]
    if missing:
        raise SuiteNotRun(
            "machinery_missing", f"{root} has no {', '.join(missing)}; is it a synced fork?"
        )


def _bind(
    service: str,
    suite: str,
    checkout: Path | None,
    overrides: Mapping[str, str] | None,
    work: Path,
) -> _Binding:
    """Guard the environment and bind ``suite`` to it, under ``work``."""

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
    _validate_report_patterns(contract.get("report_paths") or ())
    return _Binding(mode, label, image, commit, root, contract, env_file, bearers, expected)


def plan_suite(
    service: str,
    *,
    suite: str = "acceptance",
    checkout: Path | None = None,
    overrides: Mapping[str, str] | None = None,
    maven_arguments: Sequence[str] = (),
    env_file: str = "",
) -> SuitePlan:
    """Bind ``suite`` and check its target as ``run_suite`` does, and name what it would start.

    Docker and Maven are not looked for. ``env_file`` only names the file in a
    paired run's command; nothing is written here.
    """

    with tempfile.TemporaryDirectory(prefix="spi-test-") as scratch:
        bound = _bind(service, suite, checkout, overrides, Path(scratch))
        try:
            variables = read_env_file(bound.env_file)
        except (OSError, ValueError) as exc:
            raise SuiteNotRun("resolver_failed", f"{bound.env_file.name}: {exc}") from exc
        check_target(service, bound.expected)
        args = bound.arguments(maven_arguments, variables)
        if checkout is None:
            directory = f"{IMAGE_SUITE_ROOT}/{bound.test_dir}"
            command = _docker_run(
                bound.image,
                bound.test_dir,
                env_file or "suite.env",
                args,
                entrypoint=bound.test_entrypoint,
            )
        else:
            suite_dir = _suite_dir(bound.root, bound.test_dir)
            directory = str(suite_dir)
            command = (
                _script(suite_dir, bound.test_entrypoint, args)
                if bound.test_type == "script"
                else _maven(bound.root, args)
            )

    secrets = secret_values(bound.bearers, variables)
    redact = redactor(secrets, named=False)
    return SuitePlan(
        service,
        suite,
        bound.mode,
        bound.label,
        bound.image,
        bound.commit,
        directory,
        tuple(map(redactor(secrets), command)),
        bound.timeout_minutes,
        variables,
        {
            name: "[redacted]" if secret_name(name) and value else redact(value)
            for name, value in variables.items()
        },
        _expiry(bound.bearers.values()),
        tuple(bound.contract.get("suites") or ()),
    )


def run_suite(
    service: str,
    *,
    suite: str = "acceptance",
    checkout: Path | None = None,
    overrides: Mapping[str, str] | None = None,
    maven_arguments: Sequence[str] = (),
    inspect: Callable[[Path, Collection[str], Sequence[str]], dict | None] | None = None,
) -> SuiteResult:
    """Run ``suite`` of ``service`` against the environment; raise SuiteNotRun otherwise.

    ``inspect`` reads the suite directory and its reports before they are
    discarded, given the credentials the run carried so it can leave them out.
    """

    with tempfile.TemporaryDirectory(prefix="spi-test-") as scratch:
        work = Path(scratch)
        bound = _bind(service, suite, checkout, overrides, work)
        root, env_file = bound.root, bound.env_file
        variables: dict[str, str] = {}
        if bound.test_type == "script" or inspect:
            try:
                variables = read_env_file(env_file)
            except (OSError, ValueError) as exc:
                if bound.test_type == "script":
                    raise SuiteNotRun("resolver_failed", f"{env_file.name}: {exc}") from exc
                console.print(
                    f"  [warning]No report: {escape(f'{env_file.name}: {exc}')}[/warning]"
                )
                inspect = None
        secrets = secret_values(bound.bearers, variables)
        test_dir = bound.test_dir
        args = bound.arguments(maven_arguments, variables)
        timeout = bound.timeout_minutes * 60 or None

        revision = check_target(service, bound.expected)
        try:
            if checkout is None:
                reports = work / "reports"
                code = run_paired(
                    bound.image,
                    test_dir,
                    env_file,
                    args,
                    timeout,
                    reports,
                    bound.test_entrypoint,
                    secrets=secrets,
                )
            else:
                code, reports = run_checkout(
                    root,
                    test_dir,
                    env_file,
                    args,
                    timeout,
                    test_type=bound.test_type,
                    entrypoint=bound.test_entrypoint,
                    report_paths=bound.report_paths,
                    secrets=secrets,
                )
        finally:
            env_file.unlink(missing_ok=True)
        passed, verdict = judge(root, code, reports, bound.report_paths)
        tests = count_tests(reports, bound.report_paths)
        # A discarded run leaves nothing with the inspector.
        check_target(service, bound.expected, after=verdict, revision=revision)
        report = inspect(reports, secrets, bound.report_paths) if inspect else None

    declared = tuple(bound.contract.get("suites") or ())
    return SuiteResult(
        service,
        suite,
        bound.mode,
        bound.label,
        bound.image,
        bound.commit,
        passed,
        verdict,
        tests,
        report,
        declared,
    )
