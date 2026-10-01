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

"""Build a service image from a checkout into the environment's own registry.

The host's Maven builds the JAR and an ACR task builds the checkout's
``build/Dockerfile`` around it, so the image never leaves the environment it
is pinned in. The repository is ``<registry>/local/<service>``.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

import yaml

from .bootstrap import ClusterConfigError, read_cluster_config
from .images import IMAGE_REGISTRY, SCHEMA_LOAD_SERVICE_NAME
from .shell import run_command, run_process

LOCAL_NAMESPACE = "local"
BUILD_PLATFORM = "linux/amd64"
BUILD_DIRECTORY = "build"
DOCKERFILE_PATH = f"{BUILD_DIRECTORY}/Dockerfile"
DESCRIPTOR_PATH = ".spi/service.yaml"
SETTINGS_PATH = ".mvn/community-maven.settings.xml"
TASK_FILE = "acr-task.yaml"
# The profiles and goals the fork template's build runs, without its tests.
MAVEN_ARGUMENTS = ("clean", "install", "-P", "core,azure", "-DskipTests")
PULL_ROLE = "AcrPull"

_MANIFEST_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIRTY_SUFFIX = "-dirty"
# A JAR Maven did not build in this run; git cannot say which commit produced it.
PREBUILT_SUFFIX = "-prebuilt"
_JAR_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+\.jar$")


class BuildError(RuntimeError):
    """Raised when a checkout cannot be built into the environment's registry."""


@dataclass(frozen=True)
class Registry:
    """The connected environment's container registry and where it lives."""

    name: str
    login_server: str
    resource_id: str
    resource_group: str
    subscription: str
    cluster: str


@dataclass(frozen=True)
class BuiltImage:
    """One image an ACR task pushed, identified by its manifest digest."""

    service: str
    repository: str
    digest: str
    tag: str
    commit: str
    dirty: bool
    prebuilt: bool = False

    @property
    def reference(self) -> str:
        return f"{self.repository}@{self.digest}"

    @property
    def suffix(self) -> str:
        return DIRTY_SUFFIX if self.dirty else PREBUILT_SUFFIX if self.prebuilt else ""

    @property
    def source_sha(self) -> str:
        """The commit as a pin records it; only a clean tree Maven just built reads as
        the commit alone."""

        return f"{self.commit}{self.suffix}"


def _az_json(args: Sequence[str], describe: str):
    result = run_process(["az", *args, "-o", "json"], capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or "az failed"
        raise BuildError(f"Could not read {describe}: {detail}")
    try:
        return json.loads((result.stdout or "").strip() or "null")
    except json.JSONDecodeError as exc:
        raise BuildError(f"Could not parse {describe}: {exc}") from exc


def environment_registry() -> Registry:
    """The registry `spi up` provisioned in the connected environment's resource group."""

    try:
        config = read_cluster_config()
    except ClusterConfigError as exc:
        raise BuildError(str(exc)) from exc
    resource_group = config.get("AZURE_RESOURCE_GROUP", "")
    subscription = config.get("AZURE_SUBSCRIPTION_ID", "")
    if not resource_group:
        raise BuildError(
            "spi-cluster-config records no resource group; run 'spi up' on the current "
            "release to refresh it."
        )
    scope = ["--subscription", subscription] if subscription else []
    registries = _az_json(
        ["acr", "list", "--resource-group", resource_group, *scope],
        f"the container registry in {resource_group}",
    )
    found = [entry for entry in registries or [] if isinstance(entry, dict)]
    if len(found) != 1:
        raise BuildError(
            f"{resource_group} holds {len(found)} container registries; a local build needs "
            "exactly the one 'spi up' provisions."
        )
    entry = found[0]
    return Registry(
        name=str(entry.get("name", "")),
        login_server=str(entry.get("loginServer", "")).lower(),
        resource_id=str(entry.get("id", "")),
        resource_group=resource_group,
        subscription=subscription,
        cluster=config.get("AKS_CLUSTER_NAME", ""),
    )


def local_repository(registry: Registry, service: str) -> str:
    """The one repository a local build of ``service`` is pushed to and pinned from."""

    return f"{registry.login_server}/{LOCAL_NAMESPACE}/{service}"


def require_local_manifest(registry: Registry, service: str, digest: str) -> None:
    """Assert the digest exists in the environment's local repository before it is pinned."""

    scope = ["--subscription", registry.subscription] if registry.subscription else []
    image = f"{LOCAL_NAMESPACE}/{service}@{digest}"
    result = run_process(
        ["az", "acr", "repository", "show", "--name", registry.name, "--image", image, *scope]
        + ["--query", "digest", "-o", "tsv"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or (result.stdout or "").strip() != digest:
        detail = (result.stderr or "").strip().splitlines()
        raise BuildError(
            f"{digest} is not in {local_repository(registry, service)}"
            + (f" ({detail[-1]})" if detail else "")
            + f"; build it with 'spi build {service} --source <checkout>'."
        )


def require_cluster_pull(registry: Registry) -> None:
    """Refuse unless the cluster's kubelet identity holds AcrPull on the registry.

    Pods pull through the kubelet identity. `infra/modules/rbac.bicep` grants it
    the role, so an environment provisioned before that grant needs `spi up` once.
    """

    scope = ["--subscription", registry.subscription] if registry.subscription else []
    cluster = _az_json(
        ["aks", "show", "--resource-group", registry.resource_group, "--name", registry.cluster]
        + scope,
        f"cluster {registry.cluster}",
    )
    profile = ((cluster or {}).get("identityProfile") or {}).get("kubeletidentity") or {}
    principal = str(profile.get("objectId", ""))
    if not principal:
        raise BuildError(f"Cluster {registry.cluster} reports no kubelet identity to pull with.")
    held = _az_json(
        ["role", "assignment", "list", "--assignee", principal, "--scope", registry.resource_id]
        + ["--role", PULL_ROLE, *scope],
        f"role assignments on {registry.name}",
    )
    if not held:
        raise BuildError(
            f"Cluster {registry.cluster} cannot pull from {registry.name}: its kubelet identity "
            f"holds no {PULL_ROLE} there. Run 'spi up' on this release to grant it."
        )


def descriptor_service(checkout: Path) -> str:
    """The service the checkout's descriptor names."""

    path = checkout / DESCRIPTOR_PATH
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BuildError(f"{checkout} has no {DESCRIPTOR_PATH}; is it a synced fork?") from exc
    except yaml.YAMLError as exc:
        raise BuildError(f"{path} is not valid YAML: {exc}") from exc
    service = (document or {}).get("service") if isinstance(document, dict) else None
    name = service.get("name") if isinstance(service, dict) else None
    if not isinstance(name, str) or not name:
        raise BuildError(f"{path} declares no service.name.")
    return name


def resolve_jar(checkout: Path, service: str, requested: str = "") -> str:
    """The Spring Boot JAR the Dockerfile copies, relative to the checkout.

    A requested path is taken as given. Otherwise discovery follows the
    template's ``resolve-jar.sh``: the conventional module, else the one Azure
    JAR the build produced, else the one whose module names the service. More
    than one JAR in the conventional module is refused, not guessed at.
    """

    if requested:
        matches = [checkout / requested]
    else:
        matches = sorted(checkout.glob(f"provider/{service}-azure/target/*-spring-boot.jar"))
        if len(matches) > 1:
            found = ", ".join(path.name for path in matches)
            raise BuildError(
                f"provider/{service}-azure/target holds {len(matches)} Spring Boot JARs "
                f"({found}); rebuild with 'clean', or name one with --jar."
            )
    if not matches:
        discovered = sorted(checkout.glob("provider/*-azure/target/*-spring-boot.jar"))
        preferred = [path for path in discovered if service in path.parts[-3]]
        matches = discovered if len(discovered) == 1 else preferred
        if len(matches) != 1:
            found = ", ".join(str(path.relative_to(checkout)) for path in discovered) or "none"
            raise BuildError(
                f"No single Spring Boot JAR for {service} under {checkout} (found: {found}); "
                "build it first, or name it with --jar."
            )
    jar = matches[0].resolve()
    if not jar.is_file() or not jar.is_relative_to(checkout):
        raise BuildError(f"{matches[0]} is not a JAR inside {checkout}.")
    relative = jar.relative_to(checkout).as_posix()
    # The path is written into the task's build line, which splits on spaces.
    if not _JAR_PATH_RE.match(relative):
        raise BuildError(
            f"{relative!r} is not a .jar path of letters, digits, '.', '_', '-', and '/'."
        )
    return relative


def _jar_stamps(checkout: Path, requested: str = "") -> dict[Path, int]:
    """Modification time of each JAR ``resolve_jar`` could select."""

    paths = (
        [checkout / requested]
        if requested
        else checkout.glob("provider/*-azure/target/*-spring-boot.jar")
    )
    return {path.resolve(): path.stat().st_mtime_ns for path in paths if path.is_file()}


def checkout_state(checkout: Path) -> tuple[str, bool]:
    """``(head, dirty)`` of the checkout; a tree git cannot describe is refused."""

    def git(*args: str) -> str | None:
        result = run_process(["git", "-C", str(checkout), *args], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None

    head = git("rev-parse", "HEAD") or ""
    status = git("status", "--porcelain")
    if not _FULL_SHA_RE.match(head) or status is None:
        raise BuildError(f"{checkout} is not a git checkout with a commit to name the build by.")
    return head, bool(status)


def maven_command(checkout: Path, arguments: Sequence[str] = ()) -> list[str]:
    command = ["mvn", "-B", "--no-transfer-progress"]
    settings = checkout / SETTINGS_PATH
    if settings.is_file():
        command += ["--settings", str(settings)]
    return [*command, *(arguments or MAVEN_ARGUMENTS)]


def task_document(image: str, jar: str) -> str:
    """The ACR task: a BuildKit build of the checkout's Dockerfile, then the push.

    ``az acr build`` runs the classic builder, which rejects the Dockerfile's
    ``ADD --checksum`` and ``COPY --chmod``.
    """

    build = (
        f"-t $Registry/{image} -f {DOCKERFILE_PATH} --platform {BUILD_PLATFORM} "
        f"--build-arg JAR_FILE={jar} ."
    )
    document = {
        "version": "v1.1.0",
        "steps": [
            {"build": build, "env": ["DOCKER_BUILDKIT=1"]},
            {"push": [f"$Registry/{image}"]},
        ],
    }
    return yaml.safe_dump(document, sort_keys=False)


def stage_context(checkout: Path, jar: str, image: str, into: Path) -> Path:
    """Copy what the Dockerfile reads into ``into``: its own directory and the JAR."""

    shutil.copytree(checkout / BUILD_DIRECTORY, into / BUILD_DIRECTORY)
    target = into / jar
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(checkout / jar, target)
    (into / TASK_FILE).write_text(task_document(image, jar), encoding="utf-8")
    return into


def _require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise BuildError(f"{name} is not on PATH.")


def require_buildable(service: str, checkout: Path) -> None:
    """Refuse a service the lock does not carry or a checkout of another one."""

    if service not in IMAGE_REGISTRY or service == SCHEMA_LOAD_SERVICE_NAME:
        known = ", ".join(sorted(n for n in IMAGE_REGISTRY if n != SCHEMA_LOAD_SERVICE_NAME))
        raise BuildError(f"Unknown service {service!r}. Known services: {known}")
    if not (checkout / DOCKERFILE_PATH).is_file():
        raise BuildError(f"{checkout} has no {DOCKERFILE_PATH}; is it a synced fork?")
    declared = descriptor_service(checkout)
    if declared != service:
        raise BuildError(
            f"{checkout} builds {declared!r}, not {service!r}; point --source at the fork "
            "that builds it."
        )


def build_image(
    service: str,
    checkout: Path,
    *,
    skip_maven: bool = False,
    maven_arguments: Sequence[str] = (),
    jar: str = "",
) -> BuiltImage:
    """Build ``checkout`` into the environment's registry and return the pushed digest."""

    checkout = checkout.resolve()
    require_buildable(service, checkout)
    for tool in ("git", "az") + (() if skip_maven else ("mvn",)):
        _require_tool(tool)
    commit, dirty = checkout_state(checkout)
    registry = environment_registry()

    prebuilt = skip_maven
    if not skip_maven:
        before = _jar_stamps(checkout, jar)
        compiled = run_command(
            maven_command(checkout, maven_arguments),
            capture_output=False,
            description=f"Build {service} from {checkout.name}",
            check=False,
            cwd=str(checkout),
        )
        if compiled.returncode != 0:
            raise BuildError(f"Maven exited {compiled.returncode}; no image was built.")
        # Maven can rewrite tracked sources, or exit 0 without packaging.
        head, changed = checkout_state(checkout)
        dirty = dirty or changed or head != commit
    jar_path = resolve_jar(checkout, service, jar)
    if not skip_maven:
        packaged = (checkout / jar_path).resolve()
        prebuilt = before.get(packaged) == packaged.stat().st_mtime_ns

    built = BuiltImage(service, local_repository(registry, service), "", "", commit, dirty)
    built = replace(built, prebuilt=prebuilt)
    tag = f"sha-{commit[:12]}{built.suffix}"
    image = f"{LOCAL_NAMESPACE}/{service}:{tag}"
    scope = ["--subscription", registry.subscription] if registry.subscription else []
    with tempfile.TemporaryDirectory(prefix="spi-build-") as scratch:
        context = stage_context(checkout, jar_path, image, Path(scratch))
        ran = run_command(
            ["az", "acr", "run", "--registry", registry.name, "--platform", BUILD_PLATFORM]
            + ["--file", TASK_FILE, *scope, "."],
            capture_output=False,
            description=f"Build {image} in {registry.name}",
            check=False,
            cwd=str(context),
        )
    if ran.returncode != 0:
        raise BuildError(f"The ACR task exited {ran.returncode}; {image} was not pushed.")

    shown = run_process(
        ["az", "acr", "repository", "show", "--name", registry.name, "--image", image, *scope]
        + ["--query", "digest", "-o", "tsv"],
        capture_output=True,
        text=True,
    )
    digest = (shown.stdout or "").strip()
    if shown.returncode != 0 or not _MANIFEST_DIGEST_RE.match(digest):
        detail = (shown.stderr or "").strip().splitlines()
        raise BuildError(
            f"{registry.name} reports no digest for {image}" + (f": {detail[-1]}" if detail else "")
        )
    return replace(built, digest=digest, tag=tag)
