# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

from pathlib import Path

import yaml
from _quantities import _millicores

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_DIR = REPO_ROOT / "software" / "components"


def _cpu_requests(value, keys=()):
    if isinstance(value, dict):
        requests = value.get("requests")
        if isinstance(requests, dict) and "cpu" in requests:
            yield ".".join((*keys, "requests", "cpu")), requests["cpu"]
        for key, child in value.items():
            yield from _cpu_requests(child, (*keys, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _cpu_requests(child, (*keys, str(index)))


def test_helmrelease_values_request_at_least_the_cpu_admission_floor():
    below_floor = []

    for manifest_path in sorted(COMPONENTS_DIR.rglob("*.yaml")):
        for document in yaml.safe_load_all(manifest_path.read_text(encoding="utf-8")):
            if not document or document.get("kind") != "HelmRelease":
                continue
            values = document.get("spec", {}).get("values", {})
            for value_path, quantity in _cpu_requests(values):
                if _millicores(quantity) < 100:
                    relative_path = manifest_path.relative_to(REPO_ROOT)
                    below_floor.append(f"{relative_path}:{value_path}={quantity}")

    assert not below_floor, "CPU requests below the 100m admission floor:\n" + "\n".join(
        below_floor
    )
