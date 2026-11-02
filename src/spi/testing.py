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
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Collection, Iterable, Mapping, Sequence

from rich.markup import escape

from .console import console
from .images import (
    IMAGE_REGISTRY,
    SCHEMA_LOAD_SERVICE_NAME,
    ImageNotFoundError,
    ImageResolutionError,
    acceptance_digest_key,
    azure_source,
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
from .shell import prepare_command, run_command, run_process
from .suite_report import REPORT_DIRS, redactor, secret_name, secret_values

DESCRIPTOR_PATH = ".spi/service.yaml"
RESOLVER_PATH = ".github/actions/acceptance-resolver/resolve.py"
VERDICT_PATH = ".github/actions/acceptance-image/suite-verdict.py"
TRUSTSTORE_PATH = ".github/actions/acceptance-image/forward-truststore.sh"
SETTINGS_PATH = ".mvn/community-maven.settings.xml"
MACHINERY = (DESCRIPTOR_PATH, RESOLVER_PATH, VERDICT_PATH)
REPORT_SCHEMA = 1
SUITE_PLATFORM = "linux/amd64"
# The acceptance image bakes each suite under its working directory, as the lane reads it.
IMAGE_SUITE_ROOT = "/suite"
TRUST_MOUNT = "/spi-trust"
ENGINES = ("docker", "podman")
# Where a container reaches the host's loopback when its engine runs in a VM.
HOST_ALIASES = {"docker": "host.docker.internal", "podman": "host.containers.internal"}
_HOST_NETWORK = sys.platform.startswith("linux")
# The first template whose resolver takes --forward-host and whose trust store script
# takes CONTAINER_ENGINE and --host-jdk.
FORWARD_ENGINE = (1, 3, 0)
FORWARD_READY_SECONDS = 30
FORWARD_REOPEN_SECONDS = 2
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
    # ``name host:port`` for each in-cluster service the suite reaches through a port-forward.
    forwards: tuple[str, ...] = ()
    # Vault values among ``variables``: they outlive the bearers' expiry.
    vault_secrets: int = 0


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
    engine: str = "docker"
    forwards: tuple[dict, ...] = ()
    secrets: tuple[str, ...] = ()
    forward_host: str = "localhost"

    @property
    def host_network(self) -> bool:
        """A paired container on the host's own network reaches the forward on localhost."""
        return bool(self.forwards) and self.mode == "paired" and self.forward_host == "localhost"

    @property
    def test_dir(self) -> str:
        return str(self.contract.get("test_dir", ""))

    @property
    def timeout_minutes(self) -> int:
        return int(self.contract.get("timeout_minutes") or 0)

    def arguments(self, maven_arguments: Sequence[str]) -> list[str]:
        return list(maven_arguments) or list(self.contract.get("maven_arguments") or [])


def _fork_canonical(lock: dict, service: str) -> tuple[tuple[str, str, str] | None, str]:
    """``(source_repo, repository, commit)`` of the fork canonical ``service`` runs, or why not."""

    pin = decode_pins(lock).get(service)
    if pin is not None:
        return None, f"{service} runs a pinned image, not its canonical"
    mirror = decode_canonical_sources(lock).get(service, "")
    source = mirror or azure_source(service)
    trusted = decode_trusted_repos(lock).get(service, "")
    if mirror and trusted.lower() != mirror.lower():
        return None, f"{service} follows {mirror} but {trusted or 'no fork'} is trusted"
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


def fetch_machinery(
    source_repo: str, sha: str, into: Path, files: Sequence[str] = MACHINERY
) -> Path:
    """Write the commit's descriptor, resolver, and verdict script (or ``files``) under ``into``."""

    for relative in files:
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


def _resolver(
    root: Path,
    suite: str,
    arguments: Sequence[str],
    env: Mapping[str, str],
    report_path: Path,
    held: Iterable[str],
) -> dict:
    """Run the commit's resolver; return its report, or raise with its refusal's category."""

    result = run_process(
        [
            sys.executable,
            str(root / RESOLVER_PATH),
            *arguments,
            "--suite",
            suite,
            "--descriptor",
            str(root / DESCRIPTOR_PATH),
            "--report",
            str(report_path),
        ],
        env=dict(env),
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
        redact = redactor(held)
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
    return report


def suite_contract(root: Path, suite: str, work: Path) -> dict:
    """The resolver's contract-only report: what ``suite`` needs before it can be bound."""

    return _resolver(
        root,
        suite,
        ["--contract-only"],
        _host_env(("PATH",), WINDOWS_PROCESS_ENV),
        work / "contract.json",
        (),
    )


def resolve_suite(
    service: str,
    root: Path,
    suite: str,
    facts: Path,
    work: Path,
    bearers: Mapping[str, str],
    overrides: Mapping[str, str],
    *,
    secrets: Path | None = None,
    held: Iterable[str] = (),
    forward_host: str = "",
) -> tuple[dict, Path]:
    """Run the resolver in ``run`` mode; return its report and the env file.

    The descriptor must name ``service``, or a checkout of another fork would
    run its suite under this service's guards.

    The resolver lets an explicit variable win over every fact, so it sees
    only PATH, the bearers, and the caller's overrides (plus the Windows startup set).
    """

    env_file = work / "suite.env"
    env = {**_host_env(("PATH",), WINDOWS_PROCESS_ENV), **bearers, **overrides}
    arguments = ["--mode", "run", "--facts", str(facts), "--env-file", str(env_file)]
    if secrets is not None:
        arguments += ["--secrets", str(secrets)]
    if forward_host:
        arguments += ["--forward-host", forward_host]
    report = _resolver(
        root,
        suite,
        arguments,
        env,
        work / "report.json",
        (*secret_values(bearers, overrides), *held),
    )
    declared = report.get("service")
    if declared != service:
        raise SuiteNotRun(
            "descriptor_mismatch",
            f"the descriptor declares service {declared!r}, not {service!r}; "
            "point --source at the fork that builds it",
        )
    return report, env_file


def _engine_version(report: Mapping) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(report.get("engine_version", "")).split("."))
    except ValueError:
        return ()


def fetch_secrets(names: Sequence[str], facts: Mapping, into: Path) -> tuple[Path, tuple[str, ...]]:
    """Read ``names`` from the environment's vault into an owner-only JSON file.

    Returns the file and the values, so every message the run prints can leave them out.
    """

    vault = str((facts.get("azure") or {}).get("keyvault") or "")
    if not re.fullmatch(r"[A-Za-z0-9-]{3,24}", vault):
        raise _refused("env_not_ready", "the environment publishes no usable azure.keyvault")
    _require_tool("az")
    values: dict[str, str] = {}
    for name in names:
        result = run_process(
            [
                "az",
                "keyvault",
                "secret",
                "show",
                "--vault-name",
                vault,
                "--name",
                name,
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
        )
        try:
            value = (
                json.loads(result.stdout or "{}").get("value") if result.returncode == 0 else None
            )
        except (json.JSONDecodeError, AttributeError):
            value = None
        if not isinstance(value, str) or not value:
            stderr = (result.stderr or "").strip().splitlines()
            why = stderr[-1] if stderr else "no value"
            raise SuiteNotRun("secret_unavailable", f"Key Vault secret {name} in {vault}: {why}")
        # As the lane reads it: an env file cannot carry the newline a vault value often ends with.
        values[name] = value.rstrip("\n")
    path = into / "secrets.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(values, handle)
    return path, tuple(values.values())


def container_engine() -> str:
    """``SPI_CONTAINER_ENGINE``, else the first of docker and podman on PATH."""

    chosen = os.environ.get("SPI_CONTAINER_ENGINE", "").strip().lower()
    if chosen:
        if chosen not in ENGINES:
            raise SuiteNotRun(
                "engine_unknown",
                f"SPI_CONTAINER_ENGINE={chosen} is not one of {', '.join(ENGINES)}",
            )
        return chosen
    return next((engine for engine in ENGINES if shutil.which(engine)), ENGINES[0])


def forward_host(engine: str, paired: bool) -> str:
    """Where the suite reaches a port-forward the host opened.

    ``SPI_FORWARD_HOST`` overrides it for a paired run, for an engine whose
    host network is not this host's (rootless Docker, Docker Desktop on Linux).
    """

    if not paired:
        return "localhost"
    chosen = os.environ.get("SPI_FORWARD_HOST", "").strip()
    if chosen:
        if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", chosen):
            raise SuiteNotRun(
                "forward_host_invalid", f"SPI_FORWARD_HOST={chosen} is not a hostname"
            )
        return chosen
    return "localhost" if _HOST_NETWORK else HOST_ALIASES[engine]


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _start_forward(command: list[str], log: Path) -> subprocess.Popen:
    with log.open("ab") as handle:
        return subprocess.Popen(
            prepare_command(command),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )


@contextmanager
def forwarding(targets: Sequence[Mapping], work: Path) -> Generator[None, None, None]:
    """Hold a ``kubectl port-forward`` to each target, reopened if it drops, for the block."""

    stopped = threading.Event()
    # The live attempt for each target, with where its own output starts in the log.
    running: dict[str, tuple[subprocess.Popen, int]] = {}
    threads = []

    def hold(name: str, command: list[str], log: Path) -> None:
        while not stopped.is_set():
            offset = log.stat().st_size
            proc = _start_forward(command, log)
            running[name] = (proc, offset)
            while proc.poll() is None and not stopped.wait(0.5):
                pass
            if stopped.is_set():
                break
            stopped.wait(FORWARD_REOPEN_SECONDS)

    try:
        for target in targets:
            name, port = str(target["name"]), int(target["port"])
            log = work / f"forward-{name}.log"
            log.write_bytes(b"")
            command = [
                "kubectl",
                "port-forward",
                "-n",
                str(target["namespace"]),
                f"pod/{target['pod']}",
                f"{port}:{port}",
            ]
            thread = threading.Thread(target=hold, args=(name, command, log), daemon=True)
            thread.start()
            threads.append(thread)
            deadline = time.monotonic() + FORWARD_READY_SECONDS
            # Ready only while the live attempt owns the IPv4 listener: its own output holds the
            # bind line and the port answers; an exited attempt or another process never counts.
            marker = f"Forwarding from 127.0.0.1:{port} ".encode()
            while time.monotonic() < deadline:
                attempt = running.get(name)
                if (
                    attempt is not None
                    and attempt[0].poll() is None
                    and marker in log.read_bytes()[attempt[1] :]
                    and _listening(port)
                ):
                    break
                time.sleep(0.5)
            else:
                said = log.read_text(errors="replace").strip().splitlines()[-3:]
                raise SuiteNotRun(
                    "forward_failed",
                    f"the port-forward to {name} ({target['namespace']}/{target['pod']}:{port}) "
                    "did not open" + (f": {' / '.join(said)}" if said else ""),
                )
        yield
    finally:
        stopped.set()
        # Joined first, so no thread reopens a forward after it is ended.
        for thread in threads:
            thread.join(timeout=5)
        for proc, _ in running.values():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


def trust_store(root: Path, image: str, ports: Sequence[int], into: Path, engine: str) -> Path:
    """The suite JVM's trust store with each forwarded CA, built by the commit's own script.

    ``image`` is the suite image whose JDK reads it, or empty for the host JDK Maven uses.
    """

    script = root / TRUSTSTORE_PATH
    # Physical, as the script mounts it: a VM engine may share /private and not /tmp.
    out = (into / "trust").resolve()
    built = run_process(
        ["bash", str(script), image or "--host-jdk", str(out), *map(str, ports)],
        env={**os.environ, "CONTAINER_ENGINE": engine},
        capture_output=True,
        text=True,
    )
    if built.returncode != 0 or not (out / "cacerts").is_file():
        stderr = (built.stderr or "").strip().splitlines()
        raise SuiteNotRun(
            "trust_failed",
            "the forwarded CA could not be added to the suite trust store"
            + (f": {stderr[-1]}" if stderr else ""),
        )
    return out


def _trust_options(store: str) -> str:
    return f"-Djavax.net.ssl.trustStore={store}/cacerts -Djavax.net.ssl.trustStorePassword=changeit"


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
    *,
    engine: str = "docker",
    host_network: bool = False,
    trust: Path | None = None,
) -> list[str]:
    name = ["--name", container] if container else []
    network = ["--network", "host"] if host_network else []
    mount = (
        [
            "-v",
            f"{trust}:{TRUST_MOUNT}:ro",
            "-e",
            f"JAVA_TOOL_OPTIONS={_trust_options(TRUST_MOUNT)}",
        ]
        if trust
        else []
    )
    return [
        engine,
        "run",
        *name,
        "--platform",
        SUITE_PLATFORM,
        *network,
        *mount,
        "--env-file",
        env_file,
        "-e",
        f"SUITE_DIR={test_dir}",
        image,
        *args,
    ]


def pull_paired(image: str, engine: str) -> None:
    _require_tool(engine)
    pulled = run_command(
        [engine, "pull", "--platform", SUITE_PLATFORM, image],
        capture_output=False,
        description="Pull the paired acceptance image",
        check=False,
    )
    if pulled.returncode != 0:
        raise SuiteNotRun("pull_failed", f"{engine} could not pull {image}")


def run_paired(
    image: str,
    test_dir: str,
    env_file: Path,
    args: Sequence[str],
    timeout: int | None,
    reports: Path,
    *,
    engine: str = "docker",
    host_network: bool = False,
    trust: Path | None = None,
) -> int:
    """Run the suite in the paired image, already pulled; copy its reports out before the
    container goes."""

    container = f"spi-test-{uuid.uuid4().hex[:12]}"
    try:
        ran = run_command(
            _docker_run(
                image,
                test_dir,
                str(env_file),
                args,
                container,
                engine=engine,
                host_network=host_network,
                trust=trust,
            ),
            capture_output=False,
            description=f"Run {test_dir}",
            check=False,
            timeout=timeout,
        )
        copied = run_process(
            [engine, "cp", f"{container}:{IMAGE_SUITE_ROOT}/{test_dir}", str(reports)],
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
        _remove_container(container, strict=False, engine=engine)
        raise
    _remove_container(container, engine=engine)
    return ran.returncode


def _remove_container(container: str, *, strict: bool = True, engine: str = "docker") -> None:
    """Remove the suite container, which a timeout leaves running with the suite's bearers.

    A failed removal refuses the run; ``strict=False`` only warns, for a run
    already failing with another error.
    """

    removed = run_process([engine, "rm", "-f", container], capture_output=True, text=True)
    detail = (removed.stderr or "").strip()
    if removed.returncode == 0 or "no such container" in detail.lower():
        return
    message = (
        f"container {container} could not be removed ({detail or f'exit {removed.returncode}'})"
        f" and may still be running with the suite's bearers; run '{engine} rm -f {container}'"
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


def _maven(checkout: Path, args: Sequence[str]) -> list[str]:
    settings = checkout / SETTINGS_PATH
    command = ["mvn", "-B", "--no-transfer-progress"]
    if settings.is_file():
        command += ["--settings", str(settings)]
    return [*command, *args]


def run_checkout(
    checkout: Path,
    test_dir: str,
    env_file: Path,
    args: Sequence[str],
    timeout: int | None,
    *,
    trust: Path | None = None,
) -> tuple[int, Path]:
    """Run the suite with the host's Maven, as the image's entrypoint would."""

    _require_tool("mvn")
    suite_dir = _suite_dir(checkout, test_dir)
    _clear_reports(suite_dir)
    env = _host_env(MAVEN_BASE_ENV, WINDOWS_PROCESS_ENV + WINDOWS_PROFILE_ENV)
    env.update(read_env_file(env_file))
    if trust:
        env["JAVA_TOOL_OPTIONS"] = _trust_options(str(trust))
    ran = run_command(
        _maven(checkout, args),
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

    facts_body = collect_facts()
    facts = work / "facts.json"
    facts.write_text(json.dumps(facts_body), encoding="utf-8")
    bearers = mint_bearers()

    needs = suite_contract(root, suite, work)
    paired = checkout is None
    engine = container_engine() if paired else "docker"
    host = "localhost"
    if (needs.get("contract") or {}).get("forwards"):
        if _ON_WINDOWS:
            raise SuiteNotRun(
                "forwards_unsupported",
                f"suite {suite} reaches a forwarded service, which spi test does not run "
                "on Windows yet; the lane runs it",
            )
        host = forward_host(engine, paired)
        if _engine_version(needs) < FORWARD_ENGINE:
            if host != "localhost":
                lacks = (
                    "binds it only on localhost, which a "
                    f"{engine} container on this host cannot reach"
                )
            elif not paired:
                lacks = "builds its trust store only from a suite image, not the host JDK"
            elif engine != "docker":
                lacks = f"builds its trust store with docker, not {engine}"
            else:
                lacks = ""
            if lacks:
                raise SuiteNotRun(
                    "resolver_outdated",
                    f"suite {suite} reaches a forwarded service, but this commit's machinery "
                    f"(engine {needs.get('engine_version') or 'unknown'}) {lacks}; "
                    "sync the fork's template",
                )
        if target is not None and paired:
            fetch_machinery(target.source_repo, commit, root, (TRUSTSTORE_PATH,))
        elif not (root / TRUSTSTORE_PATH).is_file():
            raise SuiteNotRun(
                "machinery_missing",
                f"{root} has no {TRUSTSTORE_PATH}; it predates forwarded suites",
            )
    secrets_file: Path | None = None
    held: tuple[str, ...] = ()
    names = list((needs.get("key_vault") or {}).get("secret_names") or [])
    if names:
        secrets_file, held = fetch_secrets(names, facts_body, work)
    try:
        report, env_file = resolve_suite(
            service,
            root,
            suite,
            facts,
            work,
            bearers,
            dict(overrides or {}),
            secrets=secrets_file,
            held=held,
            forward_host="" if host == "localhost" else host,
        )
    finally:
        if secrets_file is not None:
            secrets_file.unlink(missing_ok=True)
    return _Binding(
        mode,
        label,
        image,
        commit,
        root,
        report.get("contract") or {},
        env_file,
        bearers,
        expected,
        engine,
        tuple(report.get("forwards") or ()),
        held,
        host,
    )


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

    Docker and Maven are not looked for; a suite that names vault secrets reads them,
    so ``az`` and vault access are needed as for a run. ``env_file`` only names the file in a
    paired run's command; nothing is written here.
    """

    with tempfile.TemporaryDirectory(prefix="spi-test-") as scratch:
        bound = _bind(service, suite, checkout, overrides, Path(scratch))
        try:
            variables = read_env_file(bound.env_file)
        except (OSError, ValueError) as exc:
            raise SuiteNotRun("resolver_failed", f"{bound.env_file.name}: {exc}") from exc
        check_target(service, bound.expected)
        args = bound.arguments(maven_arguments)
        if checkout is None:
            directory = f"{IMAGE_SUITE_ROOT}/{bound.test_dir}"
            command = _docker_run(
                bound.image,
                bound.test_dir,
                env_file or "suite.env",
                args,
                engine=bound.engine,
                host_network=bound.host_network,
            )
        else:
            directory = str(_suite_dir(bound.root, bound.test_dir))
            command = _maven(bound.root, args)

    forwards = tuple(
        f"{target['name']} {bound.forward_host}:{target['port']}" for target in bound.forwards
    )
    secrets = (*secret_values(bound.bearers, variables), *bound.secrets)
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
        forwards,
        len(bound.secrets),
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

    with tempfile.TemporaryDirectory(prefix="spi-test-") as scratch:
        work = Path(scratch)
        bound = _bind(service, suite, checkout, overrides, work)
        root, env_file = bound.root, bound.env_file
        secrets: tuple[str, ...] = ()
        if inspect:
            try:
                secrets = (*secret_values(bound.bearers, read_env_file(env_file)), *bound.secrets)
            except (OSError, ValueError) as exc:
                # Nothing of a run is shown when its credentials cannot be named.
                console.print(
                    f"  [warning]No report: {escape(f'{env_file.name}: {exc}')}[/warning]"
                )
                inspect = None
        test_dir = bound.test_dir
        args = bound.arguments(maven_arguments)
        timeout = bound.timeout_minutes * 60 or None

        revision = check_target(service, bound.expected)
        try:
            if checkout is None:
                pull_paired(bound.image, bound.engine)
            if bound.forwards:
                for tool in ("kubectl", "bash", "openssl"):
                    _require_tool(tool)
            with forwarding(bound.forwards, work):
                trust = None
                if bound.forwards:
                    ports = [int(target["port"]) for target in bound.forwards]
                    trust = trust_store(root, bound.image, ports, work, bound.engine)
                if checkout is None:
                    reports = work / "reports"
                    code = run_paired(
                        bound.image,
                        test_dir,
                        env_file,
                        args,
                        timeout,
                        reports,
                        engine=bound.engine,
                        host_network=bound.host_network,
                        trust=trust,
                    )
                else:
                    code, reports = run_checkout(
                        root, test_dir, env_file, args, timeout, trust=trust
                    )
        finally:
            env_file.unlink(missing_ok=True)
        passed, verdict = judge(root, code, reports)
        tests = count_tests(reports)
        # A discarded run leaves nothing with the inspector.
        check_target(service, bound.expected, after=verdict, revision=revision)
        report = inspect(reports, secrets) if inspect else None

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
