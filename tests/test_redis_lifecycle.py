# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Redis data-lifecycle contracts across profile transitions."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE = REPO_ROOT / "software" / "components" / "redis" / "release.yaml"


def _redis_values() -> dict:
    document = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    return document["spec"]["values"]


def test_redis_pvcs_delete_on_scale_down_and_statefulset_removal():
    values = _redis_values()

    for role in ("master", "replica"):
        policy = values[role]["persistentVolumeClaimRetentionPolicy"]
        assert policy == {
            "enabled": True,
            "whenScaled": "Delete",
            "whenDeleted": "Delete",
        }
        assert values[role]["persistence"]["enabled"] is True
