# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Cluster identity guard output routing.

SPI_SKIP_GUARD=1 still has to tell the operator it bypassed the check, but
`status --json` and `info --json` are meant to be piped into `jq`. The
bypass warning has to land on stderr, or a successful run under the bypass
stops being valid JSON on stdout. A guard failure under `--json` follows the
same rule: the diagnosis goes to stderr and stdout stays empty.
"""

import json
import subprocess

import pytest
from typer.testing import CliRunner

from spi import cli, info, status
from spi.deploy_record import DeployRecord

runner = CliRunner()


def _fake_context(name: str = "spi-stack-dev1") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["kubectl"], 0, f"{name}\n", "")


def _wire_status(monkeypatch):
    condition = {
        "type": "Ready",
        "status": "True",
        "reason": "ReconciliationSucceeded",
        "message": "Applied revision",
    }
    kustomizations = {
        "items": [
            {
                "metadata": {"name": "spi-osdu-services", "labels": {"spi-stack.layer": "5"}},
                "status": {"conditions": [condition]},
            }
        ]
    }
    record = DeployRecord(
        ref="v0.6.0",
        resolved_commit="a" * 40,
        deployed_at="2026-08-27T18:00:00Z",
        cli_version="0.6.0",
        profile="core",
        maintenance=False,
    )

    def required(args, description):
        if "kustomizations" in args:
            return kustomizations
        return {"spec": {"suspend": False}}

    monkeypatch.setattr(status, "_required_kubectl_json", required)
    monkeypatch.setattr(status, "read_deploy_record", lambda required=False: record)
    monkeypatch.setattr(status, "_optional_configmap", lambda name, namespace: None)
    monkeypatch.setattr("spi.info.collect_base_url", lambda: "https://example.test")


def _wire_info(monkeypatch):
    monkeypatch.setattr(info, "_read_ingress_config", lambda: {})
    monkeypatch.setattr(info, "_read_osdu_config", lambda: {})
    monkeypatch.setattr(info, "_read_flux_extension_values", lambda: {})
    monkeypatch.setattr(info, "_read_cluster_config", lambda: {})
    monkeypatch.setattr(info, "_read_init_values_yaml", lambda: "")
    monkeypatch.setattr(info, "_read_deploy_record", lambda: None)
    monkeypatch.setattr(info, "_read_image_lock", lambda: None)
    monkeypatch.setattr("spi.guard.get_suspend_status", lambda: False)


def test_status_json_is_parseable_under_guard_bypass(monkeypatch):
    monkeypatch.setenv("SPI_SKIP_GUARD", "1")
    monkeypatch.setattr("spi.guard.run_process", lambda *a, **k: _fake_context())
    _wire_status(monkeypatch)

    result = runner.invoke(cli.app, ["status", "--json"])

    # json.loads succeeds only if stdout is pure JSON, with no bypass warning mixed in.
    assert json.loads(result.stdout)["apiVersion"] == "spi.osdu.dev/v1"
    assert "Cluster guard bypassed" in result.stderr


def test_info_json_is_parseable_under_guard_bypass(monkeypatch):
    monkeypatch.setenv("SPI_SKIP_GUARD", "1")
    monkeypatch.setattr("spi.guard.run_process", lambda *a, **k: _fake_context())
    _wire_info(monkeypatch)

    result = runner.invoke(cli.app, ["info", "--json"])

    assert json.loads(result.stdout)["apiVersion"] == "spi.osdu.dev/v1"
    assert "Cluster guard bypassed" in result.stderr


def _guard_fails_on_foreign_context(monkeypatch):
    monkeypatch.delenv("SPI_SKIP_GUARD", raising=False)
    monkeypatch.setattr("spi.guard.run_process", lambda *a, **k: _fake_context("kind-dev"))


def _guard_fails_without_fingerprint(monkeypatch):
    monkeypatch.delenv("SPI_SKIP_GUARD", raising=False)

    def run_process(argv, **kwargs):
        if argv[:3] == ["kubectl", "config", "current-context"]:
            return _fake_context()
        return subprocess.CompletedProcess(argv, 1, "", "not found")

    monkeypatch.setattr("spi.guard.run_process", run_process)
    monkeypatch.setattr("spi.guard.kubectl_json", lambda args: None)


def _guard_fails_without_context(monkeypatch):
    monkeypatch.delenv("SPI_SKIP_GUARD", raising=False)
    monkeypatch.setattr(
        "spi.guard.run_process",
        lambda *a, **k: subprocess.CompletedProcess(["kubectl"], 1, "", "no context"),
    )


@pytest.mark.parametrize(
    "fail_guard, message",
    [
        (_guard_fails_on_foreign_context, "does not look like an spi-stack cluster"),
        (_guard_fails_without_fingerprint, "has no spi-stack deployment"),
        (_guard_fails_without_context, "Cannot determine kubectl context"),
    ],
)
@pytest.mark.parametrize("command", ["status", "info"])
def test_json_guard_failure_leaves_stdout_empty(monkeypatch, command, fail_guard, message):
    fail_guard(monkeypatch)

    result = runner.invoke(cli.app, [command, "--json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert message in result.stderr


def test_token_guard_failure_leaves_stdout_empty(monkeypatch):
    _guard_fails_on_foreign_context(monkeypatch)

    result = runner.invoke(cli.app, ["token"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "does not look like an spi-stack cluster" in result.stderr


def test_human_status_guard_failure_stays_on_stdout(monkeypatch):
    _guard_fails_on_foreign_context(monkeypatch)

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 1
    assert "does not look like an spi-stack cluster" in result.stdout
