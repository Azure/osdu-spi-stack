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

"""Workflow talks to the stack's Airflow 3 API server with its own login.

The stack runs Airflow 3, which no longer serves the Airflow 2 REST API, so
Workflow must render in Airflow 3 mode. Its credentials come from a CLI-seeded
Secret whose password the Airflow create-user job also reads.
"""

import re
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from spi import secrets
from spi.shell import run_process

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "software" / "charts" / "osdu-spi-service"
WORKFLOW_RELEASE = REPO_ROOT / "software" / "stacks" / "osdu" / "services" / "workflow.yaml"
AIRFLOW_RELEASE = REPO_ROOT / "software" / "components" / "airflow" / "release.yaml"

AIRFLOW_URL = "http://airflow-api-server.platform.svc.cluster.local:8080/airflow"
WORKFLOW_SECRET = "workflow-airflow-credentials"

# Flux postBuild substitution: defaults render as-is, required keys as a stand-in.
_DEFAULTED = re.compile(r"\$\{[A-Z][A-Z0-9_]*:=([^}]*)\}")
_REQUIRED = re.compile(r"\$\{[A-Z][A-Z0-9_]*\}")


def _substituted(path: Path) -> dict:
    text = _DEFAULTED.sub(r"\1", path.read_text(encoding="utf-8"))
    return yaml.safe_load(_REQUIRED.sub("substituted", text))


@pytest.fixture(scope="module")
def workflow_env() -> dict[str, dict]:
    if shutil.which("helm") is None:
        pytest.skip("Helm not installed")
    values = _substituted(WORKFLOW_RELEASE)["spec"]["values"]
    with tempfile.TemporaryDirectory() as tmp:
        values_file = Path(tmp) / "values.yaml"
        values_file.write_text(yaml.safe_dump(values), encoding="utf-8")
        result = run_process(
            ["helm", "template", "workflow", str(CHART_DIR), "-f", str(values_file)],
            capture_output=True,
            text=True,
        )
    assert result.returncode == 0, result.stderr
    for doc in yaml.safe_load_all(result.stdout):
        if doc and doc.get("kind") == "Deployment":
            container = doc["spec"]["template"]["spec"]["containers"][0]
            return {entry["name"]: entry for entry in container["env"]}
    raise AssertionError("no Deployment rendered")


def test_workflow_renders_in_airflow3_mode(workflow_env):
    assert workflow_env["OSDU_AIRFLOW_VERSION"]["value"] == "airflow3"
    assert workflow_env["OSDU_AIRFLOW_AIRFLOW3_URL"]["value"] == AIRFLOW_URL
    assert workflow_env["FEATURE_FLAG_ALLOW_HTTP_AIRFLOW"]["value"] == "true"


def test_workflow_renders_no_airflow2_setting(workflow_env):
    assert "OSDU_AIRFLOW_VERSION2_ENABLED" not in workflow_env
    assert "OSDU_AIRFLOW_URL" not in workflow_env


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("OSDU_AIRFLOW_AIRFLOW3_USERNAME", "username"),
        ("OSDU_AIRFLOW_AIRFLOW3_PASSWORD", "password"),
    ],
)
def test_workflow_reads_airflow_login_from_secret(workflow_env, name, key):
    entry = workflow_env[name]
    assert "value" not in entry
    assert entry["valueFrom"] == {"secretKeyRef": {"name": WORKFLOW_SECRET, "key": key}}


def _applied_secrets(monkeypatch) -> dict[tuple[str, str], dict]:
    applied: dict[tuple[str, str], dict] = {}
    monkeypatch.setattr(
        secrets,
        "_kubectl_apply_secret",
        lambda namespace, name, literals: applied.__setitem__((namespace, name), literals),
    )
    seed = {key: f"seed-{key}" for key in secrets.SEED_KEYS}
    secrets._create_platform_secrets(seed)
    secrets._create_osdu_secrets(seed)
    return applied


def test_cli_writes_workflow_airflow_login_from_one_seed_value(monkeypatch):
    applied = _applied_secrets(monkeypatch)
    password = "seed-airflow_workflow_password"

    assert applied[("osdu", WORKFLOW_SECRET)] == {"username": "osdu-workflow", "password": password}
    assert applied[("platform", "airflow-api-credentials")]["workflow-password"] == password
    assert applied[("platform", "airflow-api-credentials")]["password"] != password


def test_create_user_job_creates_workflow_user_with_user_role():
    job = _substituted(AIRFLOW_RELEASE)["spec"]["values"]["createUserJob"]
    script = job["args"][2]

    assert re.search(r"users create -r User -u osdu-workflow\b", script)
    assert 'reset-password -u osdu-workflow -p "$AIRFLOW_WORKFLOW_PW"' in script
    env = {entry["name"]: entry for entry in job["env"]}
    assert env["AIRFLOW_WORKFLOW_PW"]["valueFrom"] == {
        "secretKeyRef": {"name": "airflow-api-credentials", "key": "workflow-password"}
    }


def test_seed_adds_workflow_password_without_regenerating_existing_keys(monkeypatch):
    existing = {key: f"kept-{key}" for key in secrets.SEED_KEYS}
    del existing["airflow_workflow_password"]
    written: list[dict] = []
    monkeypatch.setattr(secrets, "_get_seed", lambda: dict(existing))
    monkeypatch.setattr(secrets, "_create_seed", written.append)

    seed = secrets.get_or_create_seed()

    assert {key: seed[key] for key in existing} == existing
    assert seed["airflow_workflow_password"]
    assert written == [seed]
