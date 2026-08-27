# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Deploy-record decoding and compare-and-retry behavior."""

import json
import subprocess

import pytest
from typer.testing import CliRunner

from spi import cli, deploy_record
from spi.deploy_record import DeployRecordError


def _object(*, maintenance: str = "true", resource_version: str = "7") -> dict:
    return {
        "metadata": {"resourceVersion": resource_version},
        "data": {
            "ref": "v0.6.0",
            "resolvedCommit": "a" * 40,
            "deployedAt": "2026-08-27T18:00:00Z",
            "cliVersion": "0.6.0",
            "profile": "core",
            "maintenance": maintenance,
        },
    }


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["kubectl"], returncode, stdout, stderr)


def test_read_deploy_record_rejects_invalid_maintenance(monkeypatch):
    monkeypatch.setattr(
        deploy_record,
        "run_process",
        lambda *args, **kwargs: _completed(stdout=json.dumps(_object(maintenance="maybe"))),
    )

    with pytest.raises(DeployRecordError, match="invalid maintenance"):
        deploy_record.read_deploy_record()


def test_upsert_preserves_existing_maintenance(monkeypatch):
    monkeypatch.setattr(deploy_record, "_read_record_object", lambda required=False: _object())
    captured = {}

    def patch_record(obj, record):
        captured["record"] = record
        return True

    monkeypatch.setattr(deploy_record, "_patch_record", patch_record)

    record = deploy_record.upsert_deploy_record(
        ref="v0.7.0",
        resolved_commit="b" * 40,
        deployed_at="2026-08-28T18:00:00Z",
        cli_version="0.7.0",
        profile="core",
        initial_maintenance=False,
    )

    assert record.maintenance is True
    assert captured["record"].ref == "v0.7.0"


def test_upsert_retries_losing_create_race(monkeypatch):
    """A losing create race rereads and preserves the winner's maintenance value."""
    winner = _object(maintenance="true", resource_version="9")
    reads = iter([None, winner])
    monkeypatch.setattr(deploy_record, "_read_record_object", lambda required=False: next(reads))
    monkeypatch.setattr(deploy_record, "_create_record", lambda record: False)
    monkeypatch.setattr(deploy_record.time, "sleep", lambda _seconds: None)

    captured = {}

    def patch_record(obj, record):
        captured["record"] = record
        return True

    monkeypatch.setattr(deploy_record, "_patch_record", patch_record)

    record = deploy_record.upsert_deploy_record(
        ref="v0.7.0",
        resolved_commit="c" * 40,
        deployed_at="2026-08-28T18:00:00Z",
        cli_version="0.7.0",
        profile="core",
        initial_maintenance=False,
    )

    assert record.maintenance is True
    assert captured["record"].maintenance is True


def test_create_record_returns_false_on_already_exists(monkeypatch):
    monkeypatch.setattr(
        deploy_record,
        "run_process",
        lambda *args, **kwargs: _completed(returncode=1, stderr="Error: already exists"),
    )
    record = deploy_record._decode_record(_object())

    assert deploy_record._create_record(record) is False


def test_create_record_raises_on_non_conflict_failure(monkeypatch):
    monkeypatch.setattr(
        deploy_record,
        "run_process",
        lambda *args, **kwargs: _completed(returncode=1, stderr="Forbidden"),
    )
    record = deploy_record._decode_record(_object())

    with pytest.raises(DeployRecordError, match="Forbidden"):
        deploy_record._create_record(record)


def test_set_maintenance_retries_conflict(monkeypatch):
    objects = iter([_object(resource_version="7"), _object(resource_version="8")])
    monkeypatch.setattr(
        deploy_record,
        "_read_record_object",
        lambda required=False: next(objects),
    )
    attempts = []
    monkeypatch.setattr(
        deploy_record,
        "_patch_record",
        lambda obj, record: (
            attempts.append(obj["metadata"]["resourceVersion"]) or len(attempts) > 1
        ),
    )
    monkeypatch.setattr(deploy_record.time, "sleep", lambda _seconds: None)

    record = deploy_record.set_maintenance(False)

    assert record.maintenance is False
    assert attempts == ["7", "8"]


def test_patch_uses_resource_version_test(monkeypatch):
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        return _completed()

    monkeypatch.setattr(deploy_record, "run_command", run)
    current = deploy_record._decode_record(_object())

    assert deploy_record._patch_record(_object(), current) is True

    payload = json.loads(captured["command"][-1])
    assert payload[0] == {
        "op": "test",
        "path": "/metadata/resourceVersion",
        "value": "7",
    }


def test_maintenance_requires_existing_record(monkeypatch):
    def missing(required=False):
        raise DeployRecordError("not found")

    monkeypatch.setattr(deploy_record, "_read_record_object", missing)

    with pytest.raises(DeployRecordError, match="not found"):
        deploy_record.set_maintenance(True)


@pytest.mark.parametrize(
    ("command", "enabled", "message"),
    [
        ("set", True, "maintenance enabled"),
        ("clear", False, "maintenance cleared"),
    ],
)
def test_maintenance_cli(monkeypatch, command, enabled, message):
    calls = []
    monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-stack-shared")
    monkeypatch.setattr(
        deploy_record,
        "set_maintenance",
        lambda value: calls.append(value),
    )

    result = CliRunner().invoke(cli.app, ["maintenance", command])

    assert result.exit_code == 0
    assert calls == [enabled]
    assert message in result.output
