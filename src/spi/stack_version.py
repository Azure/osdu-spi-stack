# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""The stack release an environment runs, as Flux has applied it."""

from __future__ import annotations

import re
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

from .shell import gather_reads, kubectl_json

# Rendered by software/components/stack-version, which every profile applies.
STACK_VERSION_CONFIGMAP = "spi-stack-version"
STACK_VERSION_NAMESPACE = "osdu-flux"
GIT_REPOSITORY = "osdu-spi-stack-system"
FLUX_NAMESPACE = "osdu-flux"
_RELEASE_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")


@dataclass(frozen=True)
class RunningVersion:
    """What Flux has applied, independent of what the last `spi up` recorded.

    ``version`` is ``vX.Y.Z`` on a tag, ``X.Y.Z+<commit>`` on a branch commit
    that descends from release ``X.Y.Z``, and empty when the tree carries no
    stamp. ``converged`` is true once every gating Kustomization has applied
    the source's current revision.
    """

    version: str = ""
    release: str = ""
    ref: str = ""
    commit: str = ""
    converged: bool = False

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "version": self.version,
            "release": self.release,
            "ref": self.ref,
            "commit": self.commit,
            "converged": self.converged,
        }

    def label(self) -> str:
        """Human form: the version, else ``ref@commit``, else empty."""
        if self.version:
            return self.version
        if self.commit:
            return f"{self.ref}@{self.commit[:12]}" if self.ref else self.commit[:12]
        return ""


def _source_ref(git_repository: dict) -> str:
    ref = (git_repository.get("spec") or {}).get("ref") or {}
    for key in ("tag", "branch", "semver", "name", "commit"):
        if ref.get(key):
            return str(ref[key])
    return ""


def _artifact_revision(git_repository: dict) -> str:
    return str(((git_repository.get("status") or {}).get("artifact") or {}).get("revision") or "")


def _commit(revision: str) -> str:
    # Flux v2 revisions read `<ref>@sha1:<sha>`; bare shas predate that format.
    return revision.rsplit(":", 1)[-1] if revision else ""


def is_gating(kustomization: dict) -> bool:
    labels = (kustomization.get("metadata") or {}).get("labels") or {}
    return labels.get("spi-stack.gating", "true") != "false"


def running_version(
    git_repository: dict | None,
    gating_kustomizations: list[dict],
    stamp: dict | None,
) -> RunningVersion:
    """Combine the source revision, the applied stamp, and convergence."""
    source = git_repository or {}
    revision = _artifact_revision(source)
    requested = _source_ref(source)
    # The spec moves to a new ref before the source fetches it; report what was fetched.
    ref = revision.split("@", 1)[0] if "@" in revision else requested
    ref = ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
    commit = _commit(revision)
    release = str(((stamp or {}).get("data") or {}).get("version") or "").strip()

    if commit and _RELEASE_TAG.fullmatch(ref):
        version = ref
        # Tags cut before the stamp existed still name their release.
        release = release or ref.removeprefix("v")
    elif release and commit:
        version = f"{release}+{commit[:12]}"
    else:
        version = ""

    converged = (
        bool(revision and gating_kustomizations)
        and ref == requested
        and all(
            (item.get("status") or {}).get("lastAppliedRevision") == revision
            for item in gating_kustomizations
        )
    )
    return RunningVersion(
        version=version, release=release, ref=ref, commit=commit, converged=converged
    )


def collect_running_version() -> RunningVersion:
    """Read what Flux has applied; an unreadable object reads as absent."""
    source, kustomizations = gather_reads(
        [
            lambda: kubectl_json(["get", "gitrepository", GIT_REPOSITORY, "-n", FLUX_NAMESPACE]),
            lambda: kubectl_json(
                ["get", "kustomizations.kustomize.toolkit.fluxcd.io", "-n", FLUX_NAMESPACE]
            ),
        ]
    )
    # Read after the snapshot: a stamp older than the applied revision would pair
    # a converged answer with the previous release.
    stamp = kubectl_json(
        ["get", "configmap", STACK_VERSION_CONFIGMAP, "-n", STACK_VERSION_NAMESPACE]
    )
    items = [item for item in (kustomizations or {}).get("items") or [] if isinstance(item, dict)]
    return running_version(source, [item for item in items if is_gating(item)], stamp)


def cli_is_behind(cli_version: str, release: str) -> bool:
    """True when the running CLI predates the stack release it is talking to."""
    if not release or cli_version.startswith("0.0.0"):
        return False
    try:
        return Version(cli_version) < Version(release)
    except InvalidVersion:
        return False


def skew_message(cli_version: str, release: str) -> str:
    """The warning to show when this CLI predates the stack, else empty."""
    if not cli_is_behind(cli_version, release):
        return ""
    return f"spi {cli_version} is older than the stack ({release}); run 'spi update'."
