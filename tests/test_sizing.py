# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Every sizing key reaches a manifest Flux substitutes, with the default the CLI assumes."""

import re
from pathlib import Path

import pytest
import yaml

from spi.images import IMAGE_REGISTRY
from spi.sizing import SIZING_CONFIGMAP, sizing_defaults, sizing_services

REPO_ROOT = Path(__file__).resolve().parent.parent
STACKS = REPO_ROOT / "software" / "stacks" / "osdu"
COMPONENTS = REPO_ROOT / "software" / "components"
PROFILES = STACKS / "profiles"

PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*):=([^}]*)\}")
MIDDLEWARE_MANIFESTS = {
    "elasticsearch": "elasticsearch/cluster.yaml",
    "postgres": "postgres/cluster.yaml",
    "redis": "redis/release.yaml",
    "airflow": "airflow/release.yaml",
}


def _manifest(component: str) -> Path:
    if component in sizing_services():
        return STACKS / IMAGE_REGISTRY[component].file
    return COMPONENTS / MIDDLEWARE_MANIFESTS[component]


def _sizing_placeholders(path: Path) -> dict:
    found: dict = {}
    for key, default in PLACEHOLDER.findall(path.read_text(encoding="utf-8")):
        if key.endswith("_DO_NOT_DISRUPT"):
            continue
        assert found.setdefault(key, default) == default, f"{path.name}: {key} has two defaults"
    return found


@pytest.mark.parametrize("component", sorted(sizing_defaults()))
def test_manifest_carries_every_key_with_its_default(component):
    assert _sizing_placeholders(_manifest(component)) == sizing_defaults()[component]


def test_every_kustomization_applying_a_sized_manifest_reads_the_sizing_configmap():
    """Optional, so an environment that never wrote the ConfigMap keeps its defaults."""
    sized = {_manifest(component).parent for component in sizing_defaults()}
    reading = set()
    for profile in ("minimal", "core"):
        for doc in yaml.safe_load_all((PROFILES / profile / "stack.yaml").read_text()):
            if not doc or doc.get("kind") != "Kustomization":
                continue
            directory = REPO_ROOT / doc["spec"]["path"].removeprefix("./")
            if directory not in sized:
                continue
            sources = (doc["spec"].get("postBuild") or {}).get("substituteFrom", [])
            source = {"kind": "ConfigMap", "name": SIZING_CONFIGMAP, "optional": True}
            assert source in sources, doc["metadata"]["name"]
            reading.add(directory)
    assert reading == sized
