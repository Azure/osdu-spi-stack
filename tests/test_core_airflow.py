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

"""Core changes one Flux owner's path; minimal retains its rendered Airflow."""

import hashlib
import os
import re
import shutil
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

from spi.shell import run_process
from spi.sizing import sizing_defaults

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "software" / "stacks" / "osdu" / "airflow"
MINIMAL = ROOT / "software" / "components" / "airflow"
PROFILES = ROOT / "software" / "stacks" / "osdu" / "profiles"
PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*):=([^}]*)\}")
# kustomize 5.4.3 output at c2d8cfe, with LF line endings.
MINIMAL_SHA256 = "5823dd4939e0b419ed6069715a351aeb097be85889cd53a8c8a864d276a96bff"


@lru_cache
def build(directory: Path) -> str:
    if executable := shutil.which("kustomize"):
        command = [executable, "build"]
    elif executable := shutil.which("kubectl"):
        command = [executable, "kustomize"]
    else:
        pytest.skip("kustomize or kubectl is required for manifest rendering")
    result = run_process([*command, str(directory)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def documents(directory: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(build(directory)) if doc]


def release(directory: Path) -> dict:
    return next(doc for doc in documents(directory) if doc["kind"] == "HelmRelease")


def test_minimal_airflow_is_byte_identical_to_v0_30_2():
    assert hashlib.sha256(build(MINIMAL).encode()).hexdigest() == MINIMAL_SHA256


def test_core_changes_one_existing_flux_owner_not_the_release_identity():
    minimal = {d["metadata"]["name"]: d for d in documents(PROFILES / "minimal")}
    core = {d["metadata"]["name"]: d for d in documents(PROFILES / "core")}
    expected = minimal.pop("spi-airflow")
    expected["spec"]["path"] = "./software/stacks/osdu/airflow"
    assert core["spi-airflow"] == expected
    assert all(core[name] == doc for name, doc in minimal.items())
    assert release(CORE)["metadata"] == release(MINIMAL)["metadata"]
    assert release(CORE)["spec"]["releaseName"] == "airflow"
    assert release(CORE)["spec"]["targetNamespace"] == "platform"
    assert "valuesFrom" not in release(CORE)["spec"]


def test_core_executor_identity_and_fixed_parallelism():
    values = release(CORE)["spec"]["values"]
    assert values["executor"] == "LocalExecutor"
    assert values["workers"]["persistence"]["enabled"] is False
    assert values["allowPodLaunching"] is False
    assert values["config"]["core"] == {
        "parallelism": "2",
        "dags_are_paused_at_creation": "true",
        "hostname_callable": "airflow.utils.net.get_host_ip_address",
    }
    assert values["scheduler"]["serviceAccount"] == {
        "create": False,
        "name": "workload-identity-sa",
    }
    assert values["scheduler"]["labels"]["azure.workload.identity/use"] == "true"
    for component in ("apiServer", "dagProcessor"):
        assert values[component]["serviceAccount"] == {"create": True}
        assert "azure.workload.identity/use" not in values[component].get("labels", {})
    for component in ("scheduler", "dagProcessor"):
        assert values[component]["strategy"]["rollingUpdate"] == {
            "maxSurge": 1,
            "maxUnavailable": 0,
        }


def test_rendered_core_sizing_matches_the_cli_and_has_no_empty_defaults():
    defaults = dict(PLACEHOLDER.findall(build(CORE)))
    assert defaults == sizing_defaults()["airflow"]
    assert all(defaults.values())
    minimal = release(MINIMAL)["spec"]["values"]
    core = release(CORE)["spec"]["values"]
    for component in ("apiServer", "triggerer"):
        assert core[component]["resources"] == minimal[component]["resources"]


def test_chart_renders_scheduler_as_deployment_with_only_scheduler_federated(tmp_path):
    chart = os.environ.get("SPI_AIRFLOW_CHART")
    if not chart or not shutil.which("helm"):
        pytest.skip("set SPI_AIRFLOW_CHART to the unpacked Apache Airflow 1.22 chart")
    values = PLACEHOLDER.sub(r"\2", yaml.safe_dump(release(CORE)["spec"]["values"]))
    path = tmp_path / "values.yaml"
    path.write_text(values, encoding="utf-8")
    result = run_process(
        ["helm", "template", "airflow", chart, "--namespace", "platform", "-f", str(path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    deployments = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Deployment"}
    scheduler = deployments["airflow-scheduler"]["spec"]["template"]
    assert scheduler["spec"]["serviceAccountName"] == "workload-identity-sa"
    assert scheduler["metadata"]["labels"]["azure.workload.identity/use"] == "true"
    for name in ("airflow-dag-processor", "airflow-api-server"):
        pod = deployments[name]["spec"]["template"]
        assert pod["spec"]["serviceAccountName"] == name
        assert "azure.workload.identity/use" not in pod["metadata"]["labels"]
    assert not any(
        d["kind"] == "ServiceAccount" and d["metadata"]["name"] == "workload-identity-sa"
        for d in docs
    )
    roles = [d for d in docs if d["kind"] == "Role"]
    assert not any(
        "pods" in rule.get("resources", [])
        and {"create", "update", "patch", "delete"} & set(rule["verbs"])
        for role in roles
        for rule in role["rules"]
    )
    assert not any(
        subject.get("name") == "workload-identity-sa"
        for doc in docs
        if doc["kind"] == "RoleBinding"
        for subject in doc["subjects"]
    )
