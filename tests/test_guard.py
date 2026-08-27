# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Cluster identity guard bypass output routing.

SPI_SKIP_GUARD=1 still has to tell the operator it bypassed the check, but
`status --json` and `info --json` are meant to be piped into `jq`. The
bypass warning has to land on stderr, or a successful run under the bypass
stops being valid JSON on stdout.
"""

import json
import subprocess

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
    monkeypatch.setattr("spi.info.collect_info", lambda: {"base_url": "https://example.test"})


def _wire_info(monkeypatch):
    monkeypatch.setattr(info, "_read_ingress_config", lambda: {})
    monkeypatch.setattr(info, "_read_osdu_config", lambda: {})
    monkeypatch.setattr(info, "_read_flux_extension_values", lambda: {})
    monkeypatch.setattr(info, "_read_partitions_list", lambda: [])
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
