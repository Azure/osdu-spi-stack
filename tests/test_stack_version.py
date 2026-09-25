# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""What Flux has applied, read independently of the deploy record."""

import pytest

from spi import stack_version
from spi.stack_version import RunningVersion, cli_is_behind, running_version, skew_message

# Captured before conftest swaps the module attribute for an offline stub.
_REAL_COLLECT = stack_version.collect_running_version
COMMIT = "fdd4b11cc78b23aea4b5dc6e8f9372b0951b6748"


def _source(ref: dict, revision: str) -> dict:
    return {"spec": {"ref": ref}, "status": {"artifact": {"revision": revision}}}


def _kustomization(revision: str, gating: bool = True) -> dict:
    labels = {} if gating else {"spi-stack.gating": "false"}
    return {"metadata": {"labels": labels}, "status": {"lastAppliedRevision": revision}}


def _stamp(version: str) -> dict:
    return {"data": {"version": version}}


def test_branch_commit_reads_as_the_release_it_descends_from():
    revision = f"main@sha1:{COMMIT}"
    running = running_version(
        _source({"branch": "main"}, revision), [_kustomization(revision)], _stamp("0.19.3")
    )

    assert running == RunningVersion(
        version="0.19.3+fdd4b11cc78b",
        release="0.19.3",
        ref="main",
        commit=COMMIT,
        converged=True,
    )
    assert running.label() == "0.19.3+fdd4b11cc78b"


def test_tag_reads_as_the_tag():
    revision = f"v0.19.3@sha1:{COMMIT}"
    running = running_version(
        _source({"tag": "v0.19.3"}, revision), [_kustomization(revision)], _stamp("0.19.3")
    )

    assert running.version == "v0.19.3"
    assert running.release == "0.19.3"


def test_tag_cut_before_the_stamp_still_names_its_release():
    revision = f"v0.18.0@sha1:{COMMIT}"
    running = running_version(_source({"tag": "v0.18.0"}, revision), [], None)

    assert running.version == "v0.18.0"
    assert running.release == "0.18.0"


def test_tag_upgrade_reports_the_fetched_tag_until_the_source_catches_up():
    old = f"v0.19.3@sha1:{COMMIT}"
    running = running_version(
        _source({"tag": "v0.20.0"}, old), [_kustomization(old)], _stamp("0.19.3")
    )

    assert running.version == "v0.19.3"
    assert running.ref == "v0.19.3"
    assert running.converged is False


def test_tag_without_a_fetched_artifact_reports_no_version():
    running = running_version(_source({"tag": "v0.20.0"}, ""), [], None)

    assert running.version == ""
    assert running.ref == "v0.20.0"
    assert running.converged is False


def test_branch_without_a_stamp_falls_back_to_ref_and_commit():
    revision = f"main@sha1:{COMMIT}"
    running = running_version(_source({"branch": "main"}, revision), [], None)

    assert running.version == ""
    assert running.release == ""
    assert running.label() == "main@fdd4b11cc78b"


def test_a_gating_kustomization_on_the_previous_revision_is_not_converged():
    revision = f"main@sha1:{COMMIT}"
    lagging = _kustomization("main@sha1:6c6207b9681a216b498f5d38202abd07c71545fc")
    running = running_version(
        _source({"branch": "main"}, revision),
        [_kustomization(revision), lagging],
        _stamp("0.19.3"),
    )

    assert running.converged is False


def test_non_gating_kustomizations_do_not_hold_convergence(monkeypatch):
    revision = f"main@sha1:{COMMIT}"
    items = [_kustomization(revision), _kustomization("main@sha1:old", gating=False)]
    reads = iter(
        [
            _source({"branch": "main"}, revision),
            {"items": items},
            _stamp("0.19.3"),
        ]
    )
    monkeypatch.setattr(stack_version, "kubectl_json", lambda _args: next(reads))
    monkeypatch.setattr(stack_version, "gather_reads", lambda calls: [call() for call in calls])

    running = _REAL_COLLECT()

    assert running.converged is True


def test_no_visible_gating_kustomization_is_not_converged():
    revision = f"main@sha1:{COMMIT}"
    running = running_version(_source({"branch": "main"}, revision), [], _stamp("0.19.3"))

    assert running.converged is False


def test_unreadable_source_reads_as_nothing_applied():
    running = running_version(None, [], None)

    assert running == RunningVersion()
    assert running.label() == ""
    assert running.converged is False


@pytest.mark.parametrize(
    ("cli", "release", "behind"),
    [
        ("0.19.0", "0.19.3", True),
        ("0.19.3", "0.19.3", False),
        ("0.20.0", "0.19.3", False),
        ("0.0.0+source", "0.19.3", False),
        ("0.19.0", "", False),
        ("0.19.0", "not-a-version", False),
    ],
)
def test_cli_is_behind(cli, release, behind):
    assert cli_is_behind(cli, release) is behind
    assert bool(skew_message(cli, release)) is behind


def test_skew_message_names_both_versions_and_the_fix():
    assert skew_message("0.19.0", "0.19.3") == (
        "spi 0.19.0 is older than the stack (0.19.3); run 'spi update'."
    )
