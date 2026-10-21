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
    expected["spec"]["postBuild"]["substituteFrom"].append(
        {"kind": "ConfigMap", "name": "spi-cluster-config", "optional": False}
    )
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
    assert {k: v for k, v in defaults.items() if k.startswith("AIRFLOW_")} == sizing_defaults()[
        "airflow"
    ]
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
    chart_defaults = yaml.safe_load((Path(chart) / "values.yaml").read_text(encoding="utf-8"))
    for component, job_type, probes in (
        ("scheduler", "SchedulerJob", ("startupProbe", "livenessProbe")),
        ("dagProcessor", "DagProcessorJob", ("livenessProbe",)),
    ):
        name = "airflow-scheduler" if component == "scheduler" else "airflow-dag-processor"
        container = deployments[name]["spec"]["template"]["spec"]["containers"][0]
        for probe_name in probes:
            probe = container[probe_name]
            assert "/entrypoint" not in " ".join(probe["exec"]["command"])
            assert probe["exec"]["command"] == [
                "bash",
                "-c",
                f"airflow jobs check --job-type {job_type} --local",
            ]
            settings = chart_defaults[component][probe_name] | release(CORE)["spec"]["values"][
                component
            ].get(probe_name, {})
            assert {k: v for k, v in probe.items() if k != "exec"} == {
                k: v for k, v in settings.items() if k != "command"
            }
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
    embedded = next(
        d
        for d in docs
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "spi-osdu-dag-files"
    )
    assert embedded["data"] == release(CORE)["spec"]["values"]["spiDagFiles"]
    assert "helm.sh/hook" not in embedded["metadata"].get("annotations", {})
    for name in ("airflow-scheduler", "airflow-dag-processor"):
        pod = deployments[name]["spec"]["template"]
        assert pod["metadata"]["annotations"]["checksum/extra-configmaps"]
        loader = next(c for c in pod["spec"]["initContainers"] if c["name"] == "load-osdu-dags")
        assert loader["image"] == "apache/airflow:3.2.2"
        assert loader["securityContext"]["allowPrivilegeEscalation"] is False
        assert loader["securityContext"]["capabilities"]["drop"] == ["ALL"]
        container = pod["spec"]["containers"][0]
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert env["_PIP_ADDITIONAL_REQUIREMENTS"] == (
            "-r /opt/spi/osdu/requirements.txt --require-hashes --only-binary :all:"
        )
        assert env["AZURE_RESOURCE_ID"] == "https://management.azure.com"
    rendered = tmp_path / "rendered.yaml"
    rendered.write_text(result.stdout, encoding="utf-8")
    (tmp_path / "kustomization.yaml").write_text(
        yaml.safe_dump(
            {
                "resources": ["rendered.yaml"],
                "patches": release(CORE)["spec"]["postRenderers"][0]["kustomize"]["patches"],
            }
        ),
        encoding="utf-8",
    )
    processor = next(
        d
        for d in documents(tmp_path)
        if d["metadata"]["name"] == "airflow-dag-processor" and d["kind"] == "Deployment"
    )
    probe = processor["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
    assert "DagProcessorJob" in probe["exec"]["command"][-1]


def test_generator_is_local_only_and_every_embedded_file_survives_replacements():
    assert documents(CORE / ".." / "dags") == []
    assert not any(doc["kind"] == "ConfigMap" for doc in documents(CORE))
    files = release(CORE)["spec"]["values"]["spiDagFiles"]
    source = CORE / ".." / "dags"
    assert set(files) == {
        "catalog.yaml",
        "requirements.txt",
        "load.py",
        "echo_dag.py",
        "airflow_monitoring.py",
    }
    for name, content in files.items():
        path = source / ("smoke" if name in ("echo_dag.py", "airflow_monitoring.py") else "") / name
        assert content == path.read_bytes().decode("utf-8")
        assert "${" not in content, name


def test_airflow_service_urls_match_the_core_service_routes():
    values = release(CORE)["spec"]["values"]
    env = {e["name"]: e["value"] for e in values["scheduler"]["env"]}
    assert values["dagProcessor"]["env"] == values["scheduler"]["env"]
    endpoints = {
        "storage": "/api/storage/v2",
        "search": "/api/search/v2",
        "schema": "/api/schema-service/v1",
        "workflow": "/api/workflow/v1",
        "entitlements": "/api/entitlements/v2",
        "partition": "/api/partition/v1",
        "file": "/api/file/v2",
    }
    for service, path in endpoints.items():
        assert env[f"AIRFLOW_VAR_CORE__SERVICE__{service.upper()}__URL"] == (
            f"http://{service}.osdu.svc.cluster.local{path}"
        )
    assert values["scheduler"]["startupProbe"]["failureThreshold"] == 18
    assert values["dagProcessor"]["livenessProbe"]["initialDelaySeconds"] >= 180
    assert values["dagProcessor"]["livenessProbe"]["failureThreshold"] == 10


@pytest.mark.parametrize(
    "filename",
    ["catalog.yaml", "requirements.txt", "load.py", "echo_dag.py", "airflow_monitoring.py"],
)
def test_any_embedded_file_change_rolls_both_deployments(tmp_path, filename):
    chart = os.environ.get("SPI_AIRFLOW_CHART")
    if not chart or not shutil.which("helm"):
        pytest.skip("set SPI_AIRFLOW_CHART to the unpacked Apache Airflow 1.22 chart")
    values = release(CORE)["spec"]["values"]

    def checksums():
        path = tmp_path / "values.yaml"
        path.write_text(PLACEHOLDER.sub(r"\2", yaml.safe_dump(values)), encoding="utf-8")
        rendered = run_process(
            ["helm", "template", "airflow", chart, "-f", str(path)],
            capture_output=True,
            text=True,
        )
        assert rendered.returncode == 0, rendered.stderr
        return {
            doc["metadata"]["name"]: doc["spec"]["template"]["metadata"]["annotations"][
                "checksum/extra-configmaps"
            ]
            for doc in yaml.safe_load_all(rendered.stdout)
            if doc
            and doc["kind"] == "Deployment"
            and doc["metadata"]["name"] in ("airflow-scheduler", "airflow-dag-processor")
        }

    original = checksums()
    values["spiDagFiles"][filename] += "\n# changed input\n"
    changed = checksums()
    assert set(changed) == {"airflow-scheduler", "airflow-dag-processor"}
    assert all(changed[name] != digest for name, digest in original.items())
