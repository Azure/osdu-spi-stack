# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Machine-readable status and deployability contract."""

import json

from typer.testing import CliRunner

from spi import cli, status
from spi.deploy_record import DeployRecord


def _kustomizations(ready: bool = True) -> dict:
    condition = {
        "type": "Ready",
        "status": "True" if ready else "False",
        "reason": "ReconciliationSucceeded" if ready else "Progressing",
        "message": "Applied revision" if ready else "Waiting for HelmRelease",
    }
    return {
        "items": [
            {
                "metadata": {
                    "name": "spi-osdu-services",
                    "labels": {"spi-stack.layer": "5"},
                },
                "status": {"conditions": [condition]},
            }
        ]
    }


def _record(maintenance: bool = False) -> DeployRecord:
    return DeployRecord(
        ref="v0.6.0",
        resolved_commit="a" * 40,
        deployed_at="2026-08-27T18:00:00Z",
        cli_version="0.6.0",
        profile="core",
        maintenance=maintenance,
    )


def _wire(monkeypatch, *, ready=True, record=_record(), lock=None, suspended=True):
    def required(args, description):
        if "kustomizations" in args:
            return _kustomizations(ready)
        return {"spec": {"suspend": suspended}}

    monkeypatch.setattr(status, "_required_kubectl_json", required)
    monkeypatch.setattr(status, "read_deploy_record", lambda required=False: record)
    monkeypatch.setattr(
        status,
        "_optional_configmap",
        lambda name, namespace: lock,
    )
    monkeypatch.setattr("spi.info.collect_info", lambda: {"base_url": "https://example.test"})


def test_deployable_status_contract(monkeypatch):
    _wire(monkeypatch)

    snapshot = status.collect_status()
    payload = snapshot.to_dict()

    assert snapshot.deployable is True
    assert status.status_exit_code(snapshot) == 0
    assert payload["apiVersion"] == "spi.osdu.dev/v1"
    assert payload["reason"] is None
    assert payload["stack"]["resolvedCommit"] == "a" * 40
    assert payload["baseUrl"] == "https://example.test"


def test_non_ready_kustomization_precedes_maintenance(monkeypatch):
    _wire(monkeypatch, ready=False, record=_record(maintenance=True))

    snapshot = status.collect_status()

    assert snapshot.deployable is False
    assert snapshot.reason is not None
    assert snapshot.reason.code == "kustomization_not_ready"
    assert status.status_exit_code(snapshot) == 2


def test_maintenance_blocks_ready_environment(monkeypatch):
    _wire(monkeypatch, record=_record(maintenance=True))

    snapshot = status.collect_status()

    assert snapshot.ready is True
    assert snapshot.deployable is False
    assert snapshot.reason is not None
    assert snapshot.reason.code == "maintenance"


def test_missing_record_fails_closed(monkeypatch):
    _wire(monkeypatch, record=None)

    snapshot = status.collect_status()

    assert snapshot.ready is True
    assert snapshot.deployable is False
    assert snapshot.reason is not None
    assert snapshot.reason.code == "missing_deploy_record"


def test_image_lock_summary_includes_pins(monkeypatch):
    pin = {
        "mr": "42",
        "branch": "fix/x",
        "repository": "registry/storage",
        "tag": "b" * 40,
        "canonical_repository": "registry/storage-master",
        "canonical_tag": "c" * 40,
        "canonical_created_at": "then",
        "canonical_digest": "sha256:old",
        "applied_at": "now",
    }
    lock = {
        "metadata": {
            "annotations": {
                "spi-stack.osdu.dev/pins": json.dumps({"storage": pin}),
            }
        },
        "data": {
            "IMAGE_BRANCH": "master",
            "IMAGE_RESOLVED_AT": "now",
            "IMAGE_COUNT": "14",
        },
    }
    _wire(monkeypatch, lock=lock)

    images = status.collect_status().to_dict()["images"]

    assert images == {
        "branch": "master",
        "resolvedAt": "now",
        "count": 14,
        "pinnedServices": ["storage"],
    }


def test_status_json_uses_contract_exit_code(monkeypatch):
    _wire(monkeypatch, record=_record(maintenance=True))
    monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-stack-shared")

    result = CliRunner().invoke(cli.app, ["status", "--json"])

    assert result.exit_code == 2
    assert json.loads(result.output)["reason"]["code"] == "maintenance"


def test_status_rejects_watch_with_json():
    result = CliRunner().invoke(cli.app, ["status", "--watch", "--json"])

    assert result.exit_code == 2
    assert "--watch cannot be combined" in result.output


def test_watch_status_retries_after_transient_contract_read_error(monkeypatch):
    calls = 0

    def render():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise status.StatusError("Flux CRDs are not available yet")
        raise KeyboardInterrupt

    monkeypatch.setattr(status, "render_status", render)
    monkeypatch.setattr(status.time, "sleep", lambda _interval: None)

    status.watch_status(interval=0)

    assert calls == 2
