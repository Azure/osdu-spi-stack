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

"""Contract tests for the osdu-spi-init bootstrap chart.

Renders the chart with Helm and asserts the per-partition Job fan-out and the
legal-init contract: the partition-prefixed tag name, the Key Vault name
sourced from osdu-config, and embedded init scripts that at least compile.
Skipped when Helm is not installed.
"""

import shutil
from pathlib import Path

import pytest
import yaml

from spi.shell import run_process

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "software" / "charts" / "osdu-spi-init"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="Helm not installed")


def _render(partitions: list[str]) -> list[dict]:
    set_args = []
    for i, partition in enumerate(partitions):
        set_args += ["--set", f"partitions[{i}]={partition}"]
    result = run_process(
        ["helm", "template", "osdu-spi-init", str(CHART_DIR), *set_args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _jobs(docs: list[dict], component: str) -> list[dict]:
    return [
        doc
        for doc in docs
        if doc.get("kind") == "Job"
        and doc["metadata"]["labels"].get("app.kubernetes.io/component") == component
    ]


def test_legal_init_renders_one_job_per_partition():
    docs = _render(["opendes", "second"])
    jobs = _jobs(docs, "legal-init")
    assert [job["metadata"]["name"] for job in jobs] == [
        "legal-init-opendes",
        "legal-init-second",
    ]


def test_legal_init_job_contract():
    docs = _render(["opendes"])
    job = _jobs(docs, "legal-init")[0]
    pod = job["spec"]["template"]

    assert pod["metadata"]["labels"]["azure.workload.identity/use"] == "true"
    container = pod["spec"]["containers"][0]
    env = {entry["name"]: entry for entry in container["env"]}
    assert env["PARTITION"]["value"] == "opendes"
    # The default tag is partition-prefixed from the chart's legalTag value.
    assert env["LEGAL_TAG"]["value"] == "opendes-demo-legaltag"
    kv_ref = env["KEYVAULT_NAME"]["valueFrom"]["configMapKeyRef"]
    assert (kv_ref["name"], kv_ref["key"]) == ("osdu-config", "KEYVAULT_NAME")
    assert container["command"] == ["python", "/scripts/init_legal.py"]


def test_init_scripts_compile():
    """The scripts ConfigMap embeds Python sources as YAML block scalars; a
    stray indent or quote breaks them only at Job runtime. Compile each one."""
    docs = _render(["opendes"])
    scripts = next(
        doc
        for doc in docs
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "osdu-spi-init-scripts"
    )
    assert "init_legal.py" in scripts["data"]
    for name, source in scripts["data"].items():
        compile(source, name, "exec")
