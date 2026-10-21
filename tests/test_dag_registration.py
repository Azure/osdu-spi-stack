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

"""System records are read back per partition before any DAG activation."""

import importlib.util
import io
import shutil
import sys
import time
from datetime import datetime, timezone
from http.client import HTTPMessage
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler

import pytest
import yaml

from spi.shell import run_process

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "software" / "stacks" / "osdu"
REGISTRATION = STACK / "dag-registration"
DAGS = STACK / "dags"
IDS = {"Echo_DAG", "airflow_monitoring", "Osdu_ingest", "manifest_ingestion"}


@pytest.mark.parametrize("header", ["Authorization", "authorization"])
def test_registration_bearers_are_not_forwarded_on_redirect(registration, monkeypatch, header):
    captured = []

    class Response(io.BytesIO):
        status = 200

    def open_request(request, **kwargs):
        captured.append(request)
        return Response(b"{}")

    monkeypatch.setattr(registration, "urlopen", open_request)
    registration.send("GET", "https://workflow.example/workflow", {header: "Bearer private"})
    request = captured[0]
    assert request.get_header("Authorization") == "Bearer private"
    redirected = HTTPRedirectHandler().redirect_request(
        request, io.BytesIO(), 302, "Moved", HTTPMessage(), "https://another.example/"
    )
    assert redirected is not None
    assert redirected.get_header("Authorization") is None


@pytest.fixture
def registration(monkeypatch) -> Any:
    monkeypatch.syspath_prepend(str(DAGS))
    spec = importlib.util.spec_from_file_location(
        "spi_dag_registration", REGISTRATION / "register.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def catalog():
    return yaml.safe_load((DAGS / "catalog.yaml").read_text(encoding="utf-8"))


class FakeApi:
    def __init__(self, kind, calls):
        self.kind = kind
        self.calls = calls
        self.dags = [
            {
                "dag_id": name,
                "is_stale": False,
                "is_paused": True,
                "last_parsed_time": "2030-01-01T00:00:00Z",
            }
            for name in sorted(IDS)
        ]
        self.errors = []
        self.post_status = 200
        self.conflict = ""
        self.shadow = ""
        self.shadow_dag = "Other_DAG"
        self.readback_changed = False

    def record(self, name):
        return {
            "workflowId": name,
            "workflowName": name,
            "registrationInstructions": {"dagName": self.conflict or name},
            "createdBy": "osdu-identity",
            "creationTimestamp": 1000,
            "version": 1,
        }

    def request(self, method, path, body=None, *, partition=None, expected=(200,)):
        self.calls.append((self.kind, method, path, body, partition))
        parsed = urlsplit(path)
        if self.kind == "partition":
            assert path == "/partitions" and partition is None
            return 200, ["first", "second"]
        if self.kind == "airflow":
            if method == "PATCH":
                assert isinstance(body, dict)
                return 200, {"dag_id": path.rsplit("/", 1)[-1], **body}
            if parsed.path == "/api/v2/importErrors":
                return 200, {"import_errors": self.errors, "total_entries": len(self.errors)}
            assert parsed.path == "/api/v2/dags"
            assert parse_qs(parsed.query)["exclude_stale"] == ["false"]
            return 200, {"dags": self.dags, "total_entries": len(self.dags)}
        if method == "POST":
            assert path == "/workflow/system" and partition is None
            assert expected == (200, 201, 409)
            assert isinstance(body, dict)
            return self.post_status, self.record(body["workflowName"])
        if parsed.path == "/workflow":
            name = parse_qs(parsed.query)["prefix"][0]
            records = [self.record(name)]
            if partition == self.shadow:
                records.insert(
                    0,
                    {**self.record(name), "registrationInstructions": {"dagName": self.shadow_dag}},
                )
            return 200, records
        assert method == "GET" and partition in ("first", "second")
        name = parsed.path.rsplit("/", 1)[-1]
        record = self.record(name)
        if partition == self.shadow:
            record["registrationInstructions"] = {"dagName": self.shadow_dag}
        if self.readback_changed:
            record["creationTimestamp"] = 2000
        return 200, record


@pytest.fixture
def apis():
    calls = []
    return FakeApi("airflow", calls), FakeApi("workflow", calls), FakeApi("partition", calls), calls


@pytest.mark.parametrize("post_status", [200, 201, 409])
def test_create_and_matching_409_require_readback_in_every_partition(
    registration, catalog, apis, capsys, post_status
):
    airflow, workflow, partition, calls = apis
    workflow.post_status = post_status
    registration.register(catalog, airflow, workflow, partition, timeout=0)
    reads = {
        (path, tenant)
        for api, method, path, _, tenant in calls
        if api == "workflow" and method == "GET" and "?" not in path
    }
    assert reads == {
        (f"/workflow/{name}", tenant) for name in IDS for tenant in ("first", "second")
    }
    patches = [call for call in calls if call[1] == "PATCH"]
    assert len(patches) == 4
    assert all(call[3] == {"is_paused": False} for call in patches)
    first_patch = next(i for i, call in enumerate(calls) if call[1] == "PATCH")
    assert all(call[0] != "workflow" for call in calls[first_patch:])
    assert not any(call[1] == "DELETE" for call in calls)
    assert "dag-registration outcome: registered 4, unpaused 4" in capsys.readouterr().out


def test_conflicting_409_never_overwrites_adopts_or_unpauses(registration, catalog, apis):
    airflow, workflow, partition, calls = apis
    workflow.post_status = 409
    workflow.conflict = "Other_DAG"
    with pytest.raises(registration.RegistrationError, match="Other_DAG") as error:
        registration.register(catalog, airflow, workflow, partition, timeout=0)
    assert error.value.code == "registration_conflict"
    assert not any(call[1] in ("PATCH", "PUT", "DELETE") for call in calls)


@pytest.mark.parametrize("shadow_dag", ["Other_DAG", "Echo_DAG"])
def test_shadowing_partition_record_is_reported_not_accepted_or_deleted(
    registration, catalog, apis, shadow_dag
):
    airflow, workflow, partition, calls = apis
    workflow.post_status = 409
    workflow.shadow = "second"
    workflow.shadow_dag = shadow_dag
    with pytest.raises(registration.RegistrationError, match="second/Echo_DAG.*shadows") as error:
        registration.register(catalog, airflow, workflow, partition, timeout=0)
    assert error.value.code == "registration_shadowed"
    assert not any(call[1] in ("PATCH", "PUT", "DELETE") for call in calls)


def test_changed_readback_is_not_proof_of_a_system_record(registration, catalog, apis):
    airflow, workflow, partition, calls = apis
    workflow.post_status = 409
    workflow.readback_changed = True
    with pytest.raises(registration.RegistrationError, match="cannot prove") as error:
        registration.register(catalog, airflow, workflow, partition, timeout=0)
    assert error.value.code == "registration_unverified"
    assert not any(call[1] == "PATCH" for call in calls)


@pytest.mark.parametrize("failure", ["missing", "undeclared", "import-error", "not-parsed"])
def test_parse_mismatch_registers_and_unpauses_nothing(registration, catalog, apis, failure):
    airflow, workflow, partition, calls = apis
    if failure == "missing":
        airflow.dags.pop()
    elif failure == "undeclared":
        airflow.dags.append(
            {
                "dag_id": "not_declared",
                "is_stale": False,
                "last_parsed_time": "2030-01-01T00:00:00Z",
            }
        )
    elif failure == "import-error":
        airflow.errors = [{"filename": "core-ingestion/osdu-ingest-r3.py", "stacktrace": "broken"}]
    else:
        airflow.dags[0]["last_parsed_time"] = None
    with pytest.raises(registration.RegistrationError, match="missing=.*undeclared=") as error:
        registration.register(catalog, airflow, workflow, partition, timeout=0)
    assert error.value.code == "parse_mismatch"
    assert all(call[0] == "airflow" and call[1] == "GET" for call in calls)


def test_registration_waits_for_a_parse_after_its_start(registration, catalog, apis):
    airflow, workflow, partition, calls = apis
    with pytest.raises(registration.RegistrationError, match="awaiting fresh parse"):
        registration.register(
            catalog,
            airflow,
            workflow,
            partition,
            timeout=0,
            since=datetime(2031, 1, 1, tzinfo=timezone.utc),
        )
    assert not any(call[1] == "POST" for call in calls)


def test_removed_stale_dag_is_paused_and_never_deleted(registration, catalog, apis):
    airflow, workflow, partition, calls = apis
    airflow.dags.append({"dag_id": "retired", "is_stale": True, "is_paused": False})
    registration.register(catalog, airflow, workflow, partition, timeout=0)
    patches = [call for call in calls if call[1] == "PATCH"]
    assert patches[0][2:4] == ("/api/v2/dags/retired", {"is_paused": True})
    assert len(patches) == 5
    assert not any(call[1] == "DELETE" for call in calls)


def test_paginated_airflow_collection_is_complete(registration):
    class Pages:
        def request(self, method, path):
            offset = int(parse_qs(urlsplit(path).query)["offset"][0])
            return 200, {
                "dags": [{"dag_id": str(i)} for i in range(offset, min(offset + 100, 104))],
                "total_entries": 104,
            }

    assert len(registration.collection(Pages(), "/api/v2/dags", "dags")) == 104


def test_system_post_never_inherits_the_partition_header(registration, monkeypatch):
    seen = []

    def send(method, url, headers, body):
        seen.append((method, url, headers, body))
        return 200, {}

    monkeypatch.setattr(registration, "send", send)
    api = registration.Api(
        "http://workflow/api/workflow/v1", lambda: "test-token", time.monotonic() + 60
    )
    api.request("GET", "/workflow/Echo_DAG", partition="first")
    api.request("POST", "/workflow/system", {"workflowName": "Echo_DAG"})
    assert seen[0][2]["data-partition-id"] == "first"
    assert "data-partition-id" not in seen[1][2]
    assert seen[1][2]["Authorization"] == "Bearer test-token"


def test_expired_airflow_login_refreshes_once(registration, monkeypatch):
    tokens, statuses = [], iter([401, 200])

    def token():
        tokens.append("token")
        return "test-token"

    monkeypatch.setattr(registration, "send", lambda *args: (next(statuses), {}))
    api = registration.Api("http://airflow/airflow", token, time.monotonic() + 60)
    assert api.request("GET", "/api/v2/dags")[0] == 200
    assert len(tokens) == 2


def test_osdu_identity_exchange_matches_the_init_jobs(registration, monkeypatch, tmp_path):
    assertion = tmp_path / "assertion"
    assertion.write_text("projected-assertion", encoding="utf-8")
    for key, value in {
        "AZURE_CLIENT_ID": "osdu-client",
        "AZURE_TENANT_ID": "tenant",
        "AZURE_FEDERATED_TOKEN_FILE": str(assertion),
    }.items():
        monkeypatch.setenv(key, value)
    seen = []

    def send(*args):
        seen.append(args)
        return 200, {"access_token": "result"}

    monkeypatch.setattr(registration, "send", send)
    assert registration.osdu_token() == "result"
    method, url, headers, body = seen[0]
    assert method == "POST" and url == "https://login.microsoftonline.com/tenant/oauth2/token"
    assert parse_qs(body.decode()) == {
        "grant_type": ["client_credentials"],
        "client_id": ["osdu-client"],
        "client_assertion_type": ["urn:ietf:params:oauth:client-assertion-type:jwt-bearer"],
        "client_assertion": ["projected-assertion"],
        "resource": ["https://management.azure.com/"],
    }
    assert "data-partition-id" not in headers


def test_failure_ends_with_a_typed_outcome_line(registration, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["register.py", "--catalog", str(DAGS / "catalog.yaml")])
    for name in ("AIRFLOW_URL", "WORKFLOW_URL", "PARTITION_URL"):
        monkeypatch.setenv(name, "http://service")

    def fail(*args, **kwargs):
        raise registration.RegistrationError("registration_conflict", "Echo_DAG: another DAG")

    monkeypatch.setattr(registration, "register", fail)
    assert registration.main() == 1
    assert "dag-registration outcome: registration_conflict: Echo_DAG" in capsys.readouterr().err


def build(directory: Path) -> list[dict]:
    if tool := shutil.which("kustomize"):
        command = [tool, "build"]
    elif tool := shutil.which("kubectl"):
        command = [tool, "kustomize"]
    else:
        pytest.skip("kustomize or kubectl required")
    result = run_process([*command, str(directory)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def test_registration_job_uses_the_catalog_hash_and_existing_identity():
    docs = build(REGISTRATION)
    job = next(d for d in docs if d["kind"] == "Job")
    catalog = next(d for d in docs if d["kind"] == "ConfigMap" and "catalog.yaml" in d["data"])
    assert job["metadata"]["name"] == catalog["metadata"]["name"]
    assert job["metadata"]["name"].startswith("dag-registration-")
    assert job["metadata"]["namespace"] == "platform"
    assert 900 < job["spec"]["activeDeadlineSeconds"] < 35 * 60
    assert "ttlSecondsAfterFinished" not in job["spec"]
    pod = job["spec"]["template"]
    assert pod["metadata"]["labels"]["azure.workload.identity/use"] == "true"
    assert pod["spec"]["serviceAccountName"] == "workload-identity-sa"
    container = pod["spec"]["containers"][0]
    assert container["image"] == "apache/airflow:3.2.2"
    assert container["resources"]["requests"]["cpu"] == "100m"
    env = {e["name"]: e for e in container["env"]}
    assert env["AIRFLOW_USERNAME"]["value"] == "osdu-workflow"
    assert env["AIRFLOW_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "airflow-api-credentials",
        "key": "workflow-password",
    }
    assert not any(d["metadata"]["name"] == "spi-osdu-dag-files-source" for d in docs)


def test_catalog_change_renames_the_job_without_a_second_catalog_source(tmp_path):
    for name in ("dags", "dag-registration"):
        shutil.copytree(STACK / name, tmp_path / name, ignore=shutil.ignore_patterns("__pycache__"))
    before = next(d for d in build(tmp_path / "dag-registration") if d["kind"] == "Job")
    path = tmp_path / "dags" / "catalog.yaml"
    content = path.read_text(encoding="utf-8").replace("Echo_DAG", "Echo_changed")
    path.write_text(content, encoding="utf-8")
    after = next(d for d in build(tmp_path / "dag-registration") if d["kind"] == "Job")
    assert before["metadata"]["name"] != after["metadata"]["name"]


def test_registration_is_non_gating_layer_six_and_does_not_wait_for_members():
    owner = next(
        d
        for d in build(STACK / "profiles" / "core")
        if d["metadata"]["name"] == "spi-osdu-dag-registration"
    )
    assert owner["metadata"]["labels"] == {"spi-stack.layer": "6", "spi-stack.gating": "false"}
    assert {d["name"] for d in owner["spec"]["dependsOn"]} == {"spi-airflow", "spi-osdu-init"}
    assert owner["spec"]["wait"] is True
    assert owner["spec"]["prune"] is True
    assert owner["spec"]["force"] is True
