# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Version and maintenance state for a deployed SPI environment."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from .console import display_yaml
from .shell import kubectl_apply_yaml, run_command, run_process

DEPLOY_RECORD_CONFIGMAP = "spi-deploy-record"
DEPLOY_RECORD_NAMESPACE = "osdu-flux"
_CAS_ATTEMPTS = 5


class DeployRecordError(RuntimeError):
    """Raised when the deploy record cannot be read or updated safely."""


@dataclass(frozen=True)
class DeployRecord:
    ref: str
    resolved_commit: str
    deployed_at: str
    cli_version: str
    profile: str
    maintenance: bool

    def to_data(self) -> dict[str, str]:
        return {
            "ref": self.ref,
            "resolvedCommit": self.resolved_commit,
            "deployedAt": self.deployed_at,
            "cliVersion": self.cli_version,
            "profile": self.profile,
            "maintenance": "true" if self.maintenance else "false",
        }

    def to_dict(self) -> dict[str, str | bool]:
        return asdict(self)


def _read_record_object(required: bool = False) -> dict | None:
    result = run_process(
        [
            "kubectl",
            "get",
            "configmap",
            DEPLOY_RECORD_CONFIGMAP,
            "-n",
            DEPLOY_RECORD_NAMESPACE,
            "--ignore-not-found",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or "kubectl failed"
        raise DeployRecordError(f"Could not read {DEPLOY_RECORD_CONFIGMAP}: {detail}")
    if not result.stdout.strip():
        if required:
            raise DeployRecordError(
                f"ConfigMap {DEPLOY_RECORD_CONFIGMAP} not found in {DEPLOY_RECORD_NAMESPACE}"
            )
        return None
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DeployRecordError(f"Could not parse {DEPLOY_RECORD_CONFIGMAP}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DeployRecordError(f"ConfigMap {DEPLOY_RECORD_CONFIGMAP} returned an invalid object")
    return parsed


def _decode_record(obj: dict) -> DeployRecord:
    data = obj.get("data") or {}
    if not isinstance(data, dict):
        raise DeployRecordError(f"ConfigMap {DEPLOY_RECORD_CONFIGMAP} has invalid data")

    required = ("ref", "resolvedCommit", "deployedAt", "cliVersion", "profile", "maintenance")
    missing = [key for key in required if not isinstance(data.get(key), str) or not data[key]]
    if missing:
        raise DeployRecordError(
            f"ConfigMap {DEPLOY_RECORD_CONFIGMAP} is missing required fields: "
            + ", ".join(missing)
        )

    raw_maintenance = data["maintenance"].lower()
    if raw_maintenance not in {"true", "false"}:
        raise DeployRecordError(
            f"ConfigMap {DEPLOY_RECORD_CONFIGMAP} has invalid maintenance value "
            f"{data['maintenance']!r}"
        )

    return DeployRecord(
        ref=data["ref"],
        resolved_commit=data["resolvedCommit"],
        deployed_at=data["deployedAt"],
        cli_version=data["cliVersion"],
        profile=data["profile"],
        maintenance=raw_maintenance == "true",
    )


def read_deploy_record(required: bool = False) -> DeployRecord | None:
    obj = _read_record_object(required=required)
    return _decode_record(obj) if obj is not None else None


def _is_conflict(output: str) -> bool:
    lowered = output.lower()
    return (
        "conflict" in lowered
        or "test failed" in lowered
        or "object has been modified" in lowered
    )


def _patch_record(obj: dict, record: DeployRecord) -> bool:
    resource_version = (obj.get("metadata") or {}).get("resourceVersion", "")
    if not resource_version:
        raise DeployRecordError(
            f"ConfigMap {DEPLOY_RECORD_CONFIGMAP} has no resourceVersion"
        )
    operation = "replace" if "data" in obj else "add"
    patch = [
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": resource_version,
        },
        {"op": operation, "path": "/data", "value": record.to_data()},
    ]
    result = run_command(
        [
            "kubectl",
            "patch",
            "configmap",
            DEPLOY_RECORD_CONFIGMAP,
            "-n",
            DEPLOY_RECORD_NAMESPACE,
            "--type=json",
            "-p",
            json.dumps(patch),
        ],
        description=f"Update {DEPLOY_RECORD_CONFIGMAP}",
        check=False,
    )
    if result.returncode == 0:
        return True
    detail = (result.stderr or result.stdout or "").strip()
    if _is_conflict(detail):
        return False
    raise DeployRecordError(
        f"Could not update {DEPLOY_RECORD_CONFIGMAP}: {detail or 'kubectl failed'}"
    )


def upsert_deploy_record(
    *,
    ref: str,
    resolved_commit: str,
    deployed_at: str,
    cli_version: str,
    profile: str,
    initial_maintenance: bool,
) -> DeployRecord:
    """Write a deploy record while preserving an existing maintenance flag."""

    for attempt in range(_CAS_ATTEMPTS):
        obj = _read_record_object(required=False)
        maintenance = initial_maintenance
        if obj is not None:
            maintenance = _decode_record(obj).maintenance

        record = DeployRecord(
            ref=ref,
            resolved_commit=resolved_commit,
            deployed_at=deployed_at,
            cli_version=cli_version,
            profile=profile,
            maintenance=maintenance,
        )

        if obj is None:
            manifest = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": DEPLOY_RECORD_CONFIGMAP,
                    "namespace": DEPLOY_RECORD_NAMESPACE,
                    "labels": {"app.kubernetes.io/managed-by": "osdu-spi-stack"},
                },
                "data": record.to_data(),
            }
            display_yaml(json.dumps(manifest, indent=2), f"ConfigMap: {DEPLOY_RECORD_CONFIGMAP}")
            kubectl_apply_yaml(
                json.dumps(manifest),
                f"apply {DEPLOY_RECORD_CONFIGMAP} ConfigMap",
            )
            return record

        if _patch_record(obj, record):
            return record
        time.sleep(0.2 * (2**attempt))

    raise DeployRecordError(
        f"Could not update {DEPLOY_RECORD_CONFIGMAP} after {_CAS_ATTEMPTS} conflicts"
    )


def set_maintenance(enabled: bool) -> DeployRecord:
    """Set maintenance on an existing deploy record using compare-and-retry."""

    for attempt in range(_CAS_ATTEMPTS):
        obj = _read_record_object(required=True)
        assert obj is not None
        current = _decode_record(obj)
        if current.maintenance == enabled:
            return current
        updated = DeployRecord(
            ref=current.ref,
            resolved_commit=current.resolved_commit,
            deployed_at=current.deployed_at,
            cli_version=current.cli_version,
            profile=current.profile,
            maintenance=enabled,
        )
        if _patch_record(obj, updated):
            return updated
        time.sleep(0.2 * (2**attempt))

    raise DeployRecordError(
        f"Could not update maintenance after {_CAS_ATTEMPTS} conflicts"
    )
