# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Release-tag deployment and source-finalization contracts."""

from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from spi import azure_infra, cli, deploy
from spi.config import Config, Profile
from spi.images import ResolvedImage


def _git_repository(ref_field: str, ref: str, revision: str) -> dict:
    return {
        "spec": {"ref": {ref_field: ref}},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "artifact": {"revision": revision},
        },
    }


def test_resolved_revision_accepts_tag_and_commit():
    sha = "a" * 40

    assert (
        deploy._resolved_revision(
            _git_repository("tag", "v0.6.0", f"v0.6.0@sha1:{sha}"),
            "v0.6.0",
            "tag",
        )
        == sha
    )


def test_resolved_revision_rejects_wrong_ref():
    with pytest.raises(RuntimeError, match="expected"):
        deploy._resolved_revision(
            _git_repository("tag", "v0.5.2", f"v0.5.2@sha1:{'a' * 40}"),
            "v0.6.0",
            "tag",
        )


def test_finalize_suspends_before_record(monkeypatch):
    calls = []
    config = Config.from_env("shared", repo_tag="v0.6.0")
    monkeypatch.setattr(deploy, "_wait_for_git_repository", lambda: calls.append("wait"))
    monkeypatch.setattr(
        deploy,
        "_set_source_suspended",
        lambda suspended, check=True: calls.append(f"suspend:{suspended}:{check}"),
    )
    monkeypatch.setattr(
        deploy,
        "_read_git_repository",
        lambda: _git_repository("tag", "v0.6.0", f"v0.6.0@sha1:{'a' * 40}"),
    )

    def run(command, **kwargs):
        calls.append(command[:3])
        stdout = (
            "/subscriptions/sub/resourceGroups/spi-stack-shared"
            if command[:3]
            == [
                "az",
                "group",
                "show",
            ]
            else ""
        )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(deploy, "run_command", run)

    def record(**kwargs):
        calls.append(("record", kwargs))
        return SimpleNamespace(maintenance=True)

    monkeypatch.setattr(deploy, "upsert_deploy_record", record)
    monkeypatch.setattr(deploy, "display_result", lambda _message: None)

    deploy._finalize_gitops_source(config)

    assert calls.index("suspend:True:True") < next(
        index
        for index, value in enumerate(calls)
        if isinstance(value, tuple) and value[0] == "record"
    )
    record_call = next(
        value for value in calls if isinstance(value, tuple) and value[0] == "record"
    )
    assert record_call[1]["initial_maintenance"] is True


def test_finalize_resuspends_on_reconcile_failure(monkeypatch):
    calls = []
    config = Config.from_env("shared", repo_tag="v0.6.0")
    monkeypatch.setattr(deploy, "_wait_for_git_repository", lambda: None)
    monkeypatch.setattr(
        deploy,
        "_set_source_suspended",
        lambda suspended, check=True: calls.append((suspended, check)),
    )

    def fail(command, **kwargs):
        raise RuntimeError("reconcile failed")

    monkeypatch.setattr(deploy, "run_command", fail)

    with pytest.raises(RuntimeError, match="reconcile failed"):
        deploy._finalize_gitops_source(config)

    assert calls == [(False, True), (True, False)]


def test_up_rejects_explicit_branch_with_tag():
    result = CliRunner().invoke(
        cli.app,
        ["up", "--env", "shared", "--branch", "main", "--tag", "v0.6.0"],
    )

    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_up_rejects_tag_from_source_checkout():
    result = CliRunner().invoke(cli.app, ["up", "--env", "shared", "--tag", "v0.6.0"])

    assert result.exit_code == 2
    assert "released spi wheel" in result.output


def test_up_rejects_wheel_version_that_does_not_match_tag(monkeypatch):
    monkeypatch.setattr(cli, "__version__", "0.6.1")

    result = CliRunner().invoke(cli.app, ["up", "--env", "shared", "--tag", "v0.6.0"])

    assert result.exit_code == 2
    assert "requires spi 0.6.0" in result.output


def test_name_suffix_override_must_match_existing(monkeypatch):
    monkeypatch.setattr("spi.azure_infra.read_rg_suffix_tag", lambda _rg: "abc12")
    monkeypatch.setattr("spi.azure_infra.detect_legacy_keyvault", lambda _rg, _env: False)

    with pytest.raises(typer.BadParameter, match="recorded suffix"):
        cli._resolve_name_suffix("shared", True, requested_suffix="other")


def test_connect_cluster_reuses_hardened_kubeconfig_sequence(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["az", "account", "show"]:
            return SimpleNamespace(returncode=0, stdout="tenant-id\n", stderr="")
        if command[:4] == ["kubectl", "config", "view", "--minify"]:
            return SimpleNamespace(returncode=0, stdout="cluster-user\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(azure_infra, "run_command", run)

    azure_infra.connect_cluster("spi-stack-shared", "spi-stack-shared")

    assert commands[0][:3] == ["az", "aks", "get-credentials"]
    assert commands[1] == ["kubelogin", "convert-kubeconfig", "-l", "azurecli"]
    assert commands[-1][:4] == ["kubectl", "config", "set-credentials", "cluster-user"]
    assert "--exec-env=AZURE_TENANT_ID=tenant-id" in commands[-1]


def _resolved_images() -> dict[str, ResolvedImage]:
    return {
        "storage": ResolvedImage(
            name="storage",
            repository="registry/storage",
            tag="a" * 40,
            created_at="2026-08-27T00:00:00Z",
            digest="sha256:storage",
        )
    }


def test_omitted_image_refresh_preserves_existing_lock(monkeypatch):
    config = Config.from_env("shared", profile=Profile.CORE)
    backfills = []
    monkeypatch.setattr(deploy, "read_lock", lambda required=False: {"data": {"existing": "lock"}})
    monkeypatch.setattr(
        deploy,
        "apply_schema_load_backfill",
        lambda branch: backfills.append(branch),
    )
    resolve = pytest.fail
    monkeypatch.setattr(
        deploy,
        "_resolve_image_lock",
        lambda _branch: resolve("registry lookup must not run"),
    )
    monkeypatch.setattr(
        deploy,
        "apply_image_lock",
        lambda *_args, **_kwargs: resolve("lock write must not run"),
    )

    assert deploy._ensure_image_lock(config, None, "master", {}) == {}
    assert backfills == ["master"]


def test_omitted_image_refresh_creates_missing_lock(monkeypatch):
    config = Config.from_env("shared", profile=Profile.CORE)
    resolved = _resolved_images()
    calls = []
    monkeypatch.setattr(deploy, "read_lock", lambda required=False: None)
    monkeypatch.setattr(deploy, "_resolve_image_lock", lambda branch: resolved)
    monkeypatch.setattr(
        deploy,
        "apply_image_lock",
        lambda images, branch: calls.append((images, branch)) or {},
    )

    deploy._ensure_image_lock(config, None, "master", {})

    assert calls == [(resolved, "master")]


def test_explicit_no_refresh_fails_when_core_lock_is_missing(monkeypatch):
    config = Config.from_env("shared", profile=Profile.CORE)
    monkeypatch.setattr(deploy, "read_lock", lambda required=False: None)

    with pytest.raises(RuntimeError, match="--no-refresh-images"):
        deploy._ensure_image_lock(config, False, "master", {})


def test_explicit_no_refresh_preserves_existing_core_lock(monkeypatch):
    config = Config.from_env("shared", profile=Profile.CORE)
    backfills = []
    monkeypatch.setattr(deploy, "read_lock", lambda required=False: {"data": {}})
    monkeypatch.setattr(
        deploy,
        "apply_schema_load_backfill",
        lambda branch: backfills.append(branch),
    )
    monkeypatch.setattr(
        deploy,
        "apply_image_lock",
        lambda *_args, **_kwargs: pytest.fail("lock write must not run"),
    )

    assert deploy._ensure_image_lock(config, False, "master", {}) == {}
    assert backfills == ["master"]


def test_explicit_refresh_uses_pre_resolved_images(monkeypatch):
    config = Config.from_env("shared", profile=Profile.CORE)
    resolved = _resolved_images()
    calls = []
    monkeypatch.setattr(
        deploy,
        "read_lock",
        lambda required=False: pytest.fail("explicit refresh must use the CAS helper directly"),
    )
    monkeypatch.setattr(
        deploy,
        "apply_image_lock",
        lambda images, branch: calls.append((images, branch)) or {},
    )

    deploy._ensure_image_lock(config, True, "master", resolved)

    assert calls == [(resolved, "master")]


def test_non_core_profile_never_reads_or_writes_image_lock(monkeypatch):
    config = Config.from_env("shared", profile=Profile.MINIMAL)
    monkeypatch.setattr(
        deploy,
        "read_lock",
        lambda required=False: pytest.fail("non-core deploy must not read the lock"),
    )
    monkeypatch.setattr(
        deploy,
        "apply_image_lock",
        lambda *_args, **_kwargs: pytest.fail("non-core deploy must not write the lock"),
    )

    assert deploy._ensure_image_lock(config, None, "master", {}) == {}
