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

"""Core DAG checks use one caller's own token and prove Storage provenance."""

import base64
import copy
import io
import json
import urllib.error
import urllib.parse
import uuid
from dataclasses import replace
from email.message import Message
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from spi import cli, dag_testing
from spi.identity import PersonToken
from spi.pins import PinError
from spi.testing import SuiteNotRun
from spi.token import MintedToken

DEPLOY = "00000000-0000-0000-0000-000000000001"
PERSON = "alice@example.com"
BASE = "https://osdu.example"
ENDPOINTS = {
    "workflow": BASE + "/api/workflow/v1/",
    "storage": BASE + "/api/storage/v2/",
    "schema": BASE + "/api/schema-service/v1/",
    "entitlements": BASE + "/api/entitlements/v2/",
    "legal": BASE + "/api/legal/v1/",
}
FACTS: dict[str, Any] = {
    "environment": {"name": "dev", "profile": "core", "running": {"commit": "a" * 40}},
    "endpoints": ENDPOINTS,
    "entitlements_domain": "dataservices.energy",
    "partitions": [
        {"name": "first", "primary": True, "legal_tag": "first-demo-legaltag"},
        {"name": "second", "primary": False, "legal_tag": "second-demo-legaltag"},
    ],
}


def started_run(request) -> bool:
    return request.get_method() == "POST" and request.full_url.endswith("/workflowRun")


def jwt(user: str) -> str:
    claims = {"iss": "https://sts.windows.net/tenant/", "oid": "object-id", "appid": DEPLOY}
    if user != DEPLOY:
        claims["unique_name"] = user
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "header." + encoded + ".signature"


class Response(io.BytesIO):
    def __init__(self, body: object, status: int = 200):
        super().__init__(json.dumps(body).encode())
        self.status = status


@pytest.fixture
def environment(monkeypatch):
    state: dict[str, Any] = {
        "facts": copy.deepcopy(FACTS),
        "calls": [],
        "minted": [],
        "people": [],
        "guards": 0,
        "statuses": {"Echo_DAG": ["queued", "success"], "Osdu_ingest": ["running", "finished"]},
        "runs": {},
        "record": None,
        "manifest": None,
        "clock": 0.0,
        "owner": None,
        "submitted_by": None,
        "read_delay": 0,
        "schema_status": 200,
        "impersonation": True,
        "conflict": False,
        "collision": False,
        "changed": False,
        "rollout": False,
        "borrowed": False,
        "submit_status": 200,
        "http_failure": False,
        "fact_reads": 0,
        "invalid_tags": [],
        "legal_status": 200,
        "group_domain": "first.dataservices.energy",
    }

    def guard():
        state["guards"] += 1

    def facts():
        state["fact_reads"] += 1
        result = copy.deepcopy(state["facts"])
        if state["changed"] and state["fact_reads"] > 1:
            result["environment"]["running"]["commit"] = "b" * 40
        return result

    def check_target(service, expected, **kwargs):
        if kwargs.get("after") and state["rollout"]:
            raise SuiteNotRun("target_changed", f"{service} rolled out", 2)
        return "7"

    def mint(**kwargs):
        state["minted"].append(kwargs)
        return MintedToken(jwt(DEPLOY), "9999999999", DEPLOY, "spi-deployer", "audience")

    def person():
        state["people"].append(True)
        return PersonToken(jwt(PERSON), "9999999999", "audience", PERSON, "unique_name")

    def sleep(seconds):
        state["clock"] += seconds

    def answer(request, code, body):
        if code >= 400:
            raise urllib.error.HTTPError(
                request.full_url, code, "refused", Message(), io.BytesIO(json.dumps(body).encode())
            )
        return Response(body, code)

    def http(request, timeout):
        assert timeout == dag_testing.HTTP_TIMEOUT
        state["calls"].append(request)
        if state["http_failure"]:
            raise urllib.error.URLError("unreachable")
        url = urllib.parse.urlsplit(request.full_url)
        path = urllib.parse.unquote(url.path)
        method = request.get_method()
        who = PERSON if request.get_header("Authorization") == "Bearer " + jwt(PERSON) else DEPLOY
        if path == "/api/entitlements/v2/groups":
            partition = request.get_header("Data-partition-id")
            domain = state["group_domain"].replace("first", partition, 1)
            groups = [{"name": "users", "email": f"users@{domain}"}]
            if state["impersonation"]:
                groups.append({"name": "users.datalake.impersonation"})
            return answer(request, 200, {"groups": groups})
        if path == "/api/legal/v1/legaltags:validate":
            body = {"invalidLegalTags": state["invalid_tags"]}
            return answer(request, state["legal_status"], body)
        if path.startswith("/api/schema-service/v1/schema/"):
            return answer(request, state["schema_status"], {"type": "object"})
        if path.startswith("/api/storage/v2/records/"):
            if state["collision"]:
                return answer(request, 200, {"id": path.rsplit("/", 1)[-1]})
            if state["record"] is None:
                return answer(request, 404, {"message": "not found"})
            if state["read_delay"]:
                state["read_delay"] -= 1
                return answer(request, 404, {"message": "not visible yet"})
            return answer(request, 200, state["record"])
        parts = path.removeprefix("/api/workflow/v1/workflow/").split("/")
        dag = parts[0]
        assert dag in dag_testing.DAG_IDS
        if len(parts) == 1:
            return answer(
                request,
                200,
                {
                    "workflowName": dag,
                    "registrationInstructions": {
                        "dagName": "another" if state["conflict"] else dag
                    },
                },
            )
        assert parts[1] == "workflowRun"
        if method == "POST":
            body = json.loads(request.data)
            state["runs"][dag] = body["runId"]
            if state["submit_status"] != 200:
                return answer(request, state["submit_status"], {"message": "not allowed"})
            if dag == "Osdu_ingest":
                state["manifest"] = body["executionContext"]["manifest"]
                state["record"] = copy.deepcopy(state["manifest"]["ReferenceData"][0])
                state["record"]["createUser"] = state["owner"] or who
            return answer(
                request,
                200,
                {
                    "runId": body["runId"],
                    "submittedBy": state["submitted_by"] or who,
                    "status": "submitted",
                },
            )
        assert parts[2] == state["runs"][dag]
        statuses = state["statuses"][dag]
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        return answer(
            request,
            200,
            {
                "runId": state["runs"][dag],
                "status": status,
                "submittedBy": state["submitted_by"] or who,
            },
        )

    values = iter([uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3)])
    monkeypatch.setattr(dag_testing, "uuid", SimpleNamespace(uuid4=lambda: next(values)))
    monkeypatch.setattr(
        dag_testing, "time", SimpleNamespace(monotonic=lambda: state["clock"], sleep=sleep)
    )
    monkeypatch.setattr(dag_testing, "require_deployable", guard)
    monkeypatch.setattr(dag_testing, "collect_facts", facts)
    monkeypatch.setattr(dag_testing, "read_lock", lambda: {"metadata": {}, "data": {}})
    monkeypatch.setattr(
        dag_testing,
        "decode_pins",
        lambda _: (
            {"workflow": SimpleNamespace(ephemeral=True, run_id="borrow")}
            if state["borrowed"]
            else {}
        ),
    )
    monkeypatch.setattr(dag_testing, "check_target", check_target)
    monkeypatch.setattr(dag_testing, "mint_token", mint)
    monkeypatch.setattr(dag_testing, "person_token", person)
    monkeypatch.setattr(dag_testing.urllib.request, "urlopen", http)
    return state


@pytest.mark.parametrize("caller,user", [("deploy", DEPLOY), ("me", PERSON)])
@pytest.mark.parametrize("partition", ["first", "second"])
def test_echo_then_ingestion_runs_as_the_selected_caller_in_any_partition(
    environment, caller, user, partition
):
    result = dag_testing.run_dags(caller=caller, partition=partition, timeout=10)
    assert result.outcome == "passed" and result.exit_code == 0
    assert result.user_id == user and result.partition == partition
    assert [check.state for check in result.checks] == ["passed", "passed"]
    assert result.checks[1].create_user == user
    assert environment["minted"] == ([{"caller": "deploy"}] if caller == "deploy" else [])
    assert environment["people"] == ([True] if caller == "me" else [])
    assert all(
        req.get_header("Authorization") == "Bearer " + jwt(user) for req in environment["calls"]
    )
    assert all(req.get_header("Data-partition-id") == partition for req in environment["calls"])
    posts = [req for req in environment["calls"] if started_run(req)]
    assert [urllib.parse.urlsplit(req.full_url).path for req in posts] == [
        "/api/workflow/v1/workflow/Echo_DAG/workflowRun",
        "/api/workflow/v1/workflow/Osdu_ingest/workflowRun",
    ]
    record = environment["manifest"]["ReferenceData"][0]
    assert record["id"].startswith(f"{partition}:reference-data--UnitOfMeasure:spi-dag-smoke-")
    assert record["legal"]["legaltags"] == [partition + "-demo-legaltag"]
    assert record["acl"]["owners"] == [f"data.default.owners@{partition}.dataservices.energy"]
    assert "createUser" not in record
    for req in posts:
        context = json.loads(req.data)["executionContext"]
        assert context["data-partition-id"] == partition
        assert "userId" not in context and "authToken" not in context
        assert req.get_header("On-behalf-of") is None
    assert not any(req.get_method() in ("PUT", "PATCH", "DELETE") for req in environment["calls"])


def test_default_partition_is_the_primary(environment):
    result = dag_testing.run_dags(timeout=10)
    assert result.partition == "first"


@pytest.mark.parametrize(
    "normalized_fields",
    [("submitted_by",), ("owner",), ("submitted_by", "owner")],
)
def test_mixed_case_person_matches_lowercased_workflow_and_storage_ids(
    environment, monkeypatch, normalized_fields
):
    user_id = "Alice@Example.COM"
    monkeypatch.setattr(
        dag_testing,
        "person_token",
        lambda: PersonToken(jwt(user_id), "9999999999", "audience", user_id, "unique_name"),
    )
    environment["submitted_by"] = environment["owner"] = user_id
    for field in normalized_fields:
        environment[field] = user_id.casefold()

    result = dag_testing.run_dags(caller="me", timeout=10)

    assert result.outcome == "passed" and result.exit_code == 0
    assert result.user_id == user_id
    assert [check.state for check in result.checks] == ["passed", "passed"]
    assert result.checks[1].create_user == environment["owner"]


@pytest.mark.parametrize(
    "path,body,code",
    [
        ("/groups", {"groups": None}, "caller_not_authorized"),
        ("/workflow/Echo_DAG", {"registrationInstructions": []}, "registration_conflict"),
    ],
)
def test_malformed_preflight_responses_refuse_without_starting_a_run(
    environment, monkeypatch, path, body, code
):
    original = dag_testing.Api.request

    def request(api, service, method, requested, payload=None):
        if requested == path:
            return 200, body
        return original(api, service, method, requested, payload)

    monkeypatch.setattr(dag_testing.Api, "request", request)
    with pytest.raises(SuiteNotRun) as error:
        dag_testing.run_dags(timeout=10)
    assert error.value.code == code
    assert not environment["runs"]


def test_storage_provenance_cannot_be_the_workload_identity(environment):
    environment["owner"] = "osdu-workload-identity"
    result = dag_testing.run_dags(caller="me", timeout=10)
    assert result.outcome == "failed" and result.exit_code == 3
    assert result.code == "caller_mismatch"
    assert "createUser='osdu-workload-identity'" in result.detail
    assert result.checks[0].state == "passed"


def test_echo_failure_does_not_start_ingestion(environment):
    environment["statuses"]["Echo_DAG"] = ["failed"]
    result = dag_testing.run_dags(timeout=10)
    assert result.exit_code == 3 and result.code == "workflow_failed"
    assert result.checks[1].state == "skipped"
    assert "Osdu_ingest" not in environment["runs"]
    assert environment["record"] is None


def test_a_workflow_timeout_is_finite_and_keeps_the_run_id(environment):
    environment["statuses"]["Echo_DAG"] = ["running"]
    result = dag_testing.run_dags(timeout=10)
    assert result.code == "workflow_timeout"
    assert result.checks[0].run_id
    assert environment["clock"] == 10
    assert result.checks[1].state == "skipped"


def test_run_submission_must_name_the_same_authenticated_caller(environment):
    environment["submitted_by"] = "somebody-else"
    result = dag_testing.run_dags(caller="me", timeout=10)
    assert result.code == "caller_mismatch" and result.exit_code == 3
    assert "submittedBy=" in result.detail


def test_successful_ingestion_waits_for_record_visibility(environment):
    environment["read_delay"] = 2
    result = dag_testing.run_dags(timeout=10)
    assert result.outcome == "passed"
    assert result.checks[1].seconds == 15


@pytest.mark.parametrize(
    "setting,value,code",
    [
        ("impersonation", False, "impersonation_missing"),
        ("schema_status", 404, "schema_missing"),
        ("conflict", True, "registration_conflict"),
        ("collision", True, "record_exists"),
    ],
)
def test_failed_prerequisite_starts_no_workflow(environment, setting, value, code):
    environment[setting] = value
    with pytest.raises(SuiteNotRun) as error:
        dag_testing.run_dags(timeout=10)
    assert error.value.code == code and error.value.exit_code == 2
    assert not any(started_run(req) for req in environment["calls"])


def test_unknown_partition_is_refused_before_minting(environment):
    with pytest.raises(SuiteNotRun) as error:
        dag_testing.run_dags(partition="unknown")
    assert error.value.code == "unknown_partition"
    assert environment["minted"] == environment["people"] == environment["calls"] == []


def test_non_core_environment_is_refused(environment):
    environment["facts"]["environment"]["profile"] = "minimal"
    with pytest.raises(SuiteNotRun) as error:
        dag_testing.run_dags()
    assert error.value.code == "profile_not_core"


def test_missing_legal_seed_is_not_reconstructed_or_created(environment):
    environment["facts"]["partitions"][0]["legal_tag"] = ""
    with pytest.raises(SuiteNotRun) as error:
        dag_testing.run_dags()
    assert error.value.code == "legal_tag_missing"
    assert environment["calls"] == []


def test_non_deployable_environment_is_refused_before_minting(environment, monkeypatch):
    def refuse():
        raise PinError("maintenance")

    monkeypatch.setattr(dag_testing, "require_deployable", refuse)
    with pytest.raises(SuiteNotRun) as error:
        dag_testing.run_dags()
    assert error.value.code == "not_deployable"
    assert environment["minted"] == environment["calls"] == []


def test_borrowed_workflow_is_refused_before_minting(environment):
    environment["borrowed"] = True
    with pytest.raises(SuiteNotRun) as error:
        dag_testing.run_dags()
    assert error.value.code == "service_borrowed"
    assert environment["minted"] == []


@pytest.mark.parametrize("change", ["rollout", "changed"])
def test_changed_target_discards_the_verdict_but_keeps_check_details(environment, change):
    environment[change] = True
    result = dag_testing.run_dags(timeout=10)
    assert result.outcome == "refused" and result.code == "target_changed" and result.exit_code == 2
    assert all(check.state == "passed" for check in result.checks)
    assert result.checks[1].create_user == DEPLOY


def test_dry_run_only_reads_and_does_not_return_a_verdict(environment):
    result = dag_testing.run_dags(caller="me", partition="second", dry_run=True)
    assert result.outcome == "planned" and result.exit_code == 0
    assert all(check.state == "planned" for check in result.checks)
    writes = [req for req in environment["calls"] if req.get_method() != "GET"]
    assert [req.full_url for req in writes] == [BASE + "/api/legal/v1/legaltags:validate"]
    assert environment["record"] is None
    assert result.checks[1].record_id


def test_bearer_is_never_forwarded_to_a_redirect_target(environment):
    dag_testing.run_dags(timeout=10)
    for request in environment["calls"]:
        assert "Authorization" not in request.headers
        assert request.get_header("Authorization")


def test_response_details_and_json_results_redact_credentials():
    actor = dag_testing.Caller("me", PERSON, "private-bearer-value")
    api = dag_testing.Api(ENDPOINTS, actor, "first")
    detail = api.detail(403, {"error": "Authorization: Bearer private-bearer-value"})
    assert "private-bearer-value" not in detail
    assert "private-bearer-value" not in repr(actor)


def test_http_transport_error_is_not_an_unhandled_exception(environment):
    environment["http_failure"] = True
    with pytest.raises(SuiteNotRun) as error:
        dag_testing.run_dags()
    assert error.value.code == "request_failed"


@pytest.fixture
def invoke(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")

    def run(**kwargs):
        calls.append(kwargs)
        return dag_testing.DagSmokeResult(
            "passed",
            None,
            "createUser is the caller",
            0,
            kwargs["caller"],
            PERSON,
            kwargs["partition"] or "first",
            FACTS["environment"],
            (
                dag_testing.DagCheck("Echo_DAG", state="passed", run_id="echo-run"),
                dag_testing.DagCheck(
                    "Osdu_ingest",
                    state="passed",
                    run_id="ingest-run",
                    record_id="record",
                    create_user=PERSON,
                ),
            ),
        )

    monkeypatch.setattr(dag_testing, "run_dags", run)
    return CliRunner(), calls


def test_cli_selects_the_builtin_dag_check_and_its_caller(invoke):
    runner, calls = invoke
    result = runner.invoke(
        cli.app,
        ["test", "dags", "--as", "me", "--partition", "second", "--timeout", "120", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["caller"] == "me" and calls[0]["partition"] == "second"
    assert calls[0]["timeout"] == 120
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload["outcome"] == "passed" and payload["mode"] == "stack"
    assert payload["caller"] == {"as": "me", "id": PERSON}
    assert payload["checks"][1]["createUser"] == PERSON


def test_cli_prints_the_caller_as_literal_text(invoke, monkeypatch):
    runner, _ = invoke
    original = dag_testing.run_dags

    def run(**kwargs):
        return replace(original(**kwargs), user_id="[bold]caller[/bold]")

    monkeypatch.setattr(dag_testing, "run_dags", run)
    result = runner.invoke(cli.app, ["test", "dags"])
    assert result.exit_code == 0, result.output
    assert "[bold]caller[/bold]" in result.output


@pytest.mark.parametrize(
    "options",
    [
        ["--source", "."],
        ["--suite", "all"],
        ["--set", "X=1"],
        ["--review"],
        ["--env-file", "vars"],
        ["--", "-Dtest=Something"],
    ],
)
def test_dag_mode_refuses_service_suite_options_before_any_work(invoke, options):
    runner, calls = invoke
    result = runner.invoke(cli.app, ["test", "dags", *options])
    assert result.exit_code == 1
    assert calls == []
    assert "descriptor, Maven and review options" in " ".join(result.output.split())


def test_service_suite_refuses_dag_only_options(invoke):
    runner, calls = invoke
    result = runner.invoke(cli.app, ["test", "storage", "--as", "me"])
    assert result.exit_code == 1
    assert "apply only" in result.output and calls == []


def test_cli_preserves_failure_exit_and_machine_readable_code(invoke, monkeypatch):
    runner, _ = invoke

    def refuse(**kwargs):
        raise SuiteNotRun("impersonation_missing", "reapply the editor preset", 2)

    monkeypatch.setattr(dag_testing, "run_dags", refuse)
    result = runner.invoke(cli.app, ["test", "dags", "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload["outcome"] == "refused" and payload["code"] == "impersonation_missing"


def _names(result):
    return [(step.state, step.name) for step in result.trace.steps]


def test_trace_records_every_check_in_order_with_its_evidence(environment):
    environment["read_delay"] = 2
    result = dag_testing.run_dags(timeout=10)
    assert [state for state, _ in _names(result)] == ["held"] * 13
    assert [step.phase for step in result.trace.steps] == ["before"] * 8 + ["runs"] * 4 + ["after"]
    stored, submitted = result.trace.stored, result.trace.submitted
    assert stored is not None and submitted is not None
    assert stored["createUser"] == DEPLOY and submitted["id"] == result.checks[1].record_id
    ingest = [event for event, _ in result.checks[1].events]
    assert ingest[0] == "submitted" and ingest[-2:] == ["finished", "stored"]
    assert "read back after 10 s" in result.trace.steps[10].evidence


def test_a_missing_record_fails_its_check_and_leaves_provenance_unreached(environment, monkeypatch):
    monkeypatch.setattr(dag_testing.Api, "request", _without_records(dag_testing.Api.request))
    result = dag_testing.run_dags(timeout=10)
    assert result.code == "record_missing"
    states = dict((name, state) for state, name in _names(result))
    assert states[dag_testing.RECORD_STEP] == "failed"
    assert states[dag_testing.PROVENANCE_STEP] == "not reached"
    assert states["Target unchanged"] == "held"
    assert result.checks[1].events[-1][0] == "record_missing"


def _without_records(original):
    def request(api, service, method, path, payload=None):
        if service == "storage":
            return 404, {"message": "not found"}
        return original(api, service, method, path, payload)

    return request


def test_a_refused_preflight_keeps_the_checks_that_held_and_the_refusal(environment):
    environment["schema_status"] = 404
    trace = dag_testing.Trace()
    with pytest.raises(SuiteNotRun):
        dag_testing.run_dags(timeout=10, trace=trace)
    assert [step.state for step in trace.steps] == ["held"] * 4 + ["refused"]
    assert trace.partition == "first" and trace.user_id == DEPLOY


@pytest.fixture
def reports(monkeypatch, tmp_path):
    from spi import suite_report

    monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
    monkeypatch.setattr(suite_report, "report_folder", lambda: tmp_path)
    monkeypatch.setattr(suite_report, "show", lambda path: False)
    return tmp_path


def test_report_draws_the_real_run_and_holds_no_token(environment, reports):
    result = CliRunner().invoke(cli.app, ["test", "dags", "--as", "me", "--report", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.splitlines()[-1])
    page = (reports / "spi-test-dags-first.html").read_text()
    assert payload["report"] == str(reports / "spi-test-dags-first.html")
    assert environment["runs"]["Osdu_ingest"] in page and "createUser is the caller" in page
    assert jwt(PERSON) not in page and jwt(PERSON).split(".")[1] not in page


def test_the_scoreboard_passes_over_a_dag_page(environment, reports):
    from spi.suite_page import read_pages

    result = CliRunner().invoke(cli.app, ["test", "dags", "--report"])
    assert result.exit_code == 0, result.output
    assert [path.name for path in reports.glob("*.html")] == ["spi-test-dags-first.html"]
    assert read_pages(reports.glob("*.html")) == []


def test_a_refused_smoke_still_writes_its_page(environment, reports):
    environment["impersonation"] = False
    result = CliRunner().invoke(cli.app, ["test", "dags", "--report", "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload["code"] == "impersonation_missing" and payload["report"]
    page = (reports / "spi-test-dags-first.html").read_text()
    assert "Not run (impersonation_missing)" in page
    assert "The record" not in page and "Where the time went" not in page


def test_a_report_that_cannot_be_written_keeps_the_verdict(environment, reports, monkeypatch):
    from spi import dag_page

    def broken(facts):
        raise RuntimeError("cannot draw")

    monkeypatch.setattr(dag_page, "render", broken)
    environment["owner"] = "somebody-else"
    result = CliRunner().invoke(cli.app, ["test", "dags", "--report", "--json"])
    assert result.exit_code == 3
    assert "report" not in json.loads(result.stdout.splitlines()[-1])


def test_dry_run_refuses_a_report(invoke):
    runner, calls = invoke
    result = runner.invoke(cli.app, ["test", "dags", "--dry-run", "--report"])
    assert result.exit_code == 1 and calls == []
    assert "nothing to report" in result.output


def test_json_carries_what_the_page_draws(environment, reports):
    result = CliRunner().invoke(cli.app, ["test", "dags", "--json"])
    payload = json.loads(result.stdout.splitlines()[-1])
    assert [step["state"] for step in payload["steps"]] == ["held"] * 13
    assert payload["stored"]["createUser"] == DEPLOY and payload["submitted"]["id"]
    assert set(payload["targets"]) == {"workflow", "storage"}


def test_a_report_refused_before_a_partition_says_so(environment, reports):
    environment["facts"]["environment"]["profile"] = "minimal"
    result = CliRunner().invoke(cli.app, ["test", "dags", "--report"])
    assert result.exit_code == 2
    assert "No report" in result.output and list(reports.glob("*.html")) == []


def test_an_invalid_legal_tag_is_refused_with_legals_reason(environment):
    environment["invalid_tags"] = [{"name": "first-demo-legaltag", "reason": "LegalTag expired"}]
    with pytest.raises(SuiteNotRun) as error:
        dag_testing.run_dags(timeout=10)
    assert error.value.code == "legal_tag_invalid" and error.value.exit_code == 2
    assert "first-demo-legaltag: LegalTag expired" in str(error.value)
    assert not environment["runs"]


def test_a_legal_tag_legal_will_not_validate_for_the_caller_is_not_checked(environment):
    environment["legal_status"] = 403
    result = dag_testing.run_dags(timeout=10)
    assert result.outcome == "passed"
    assert ("not checked", "Legal tag is valid") in _names(result)


def test_an_acl_outside_the_callers_domain_is_refused(environment):
    environment["group_domain"] = "first.contoso.com"
    with pytest.raises(SuiteNotRun) as error:
        dag_testing.run_dags(timeout=10)
    assert error.value.code == "acl_domain_mismatch"
    named = str(error.value).split("group domain ")[1].split(";")[0]
    assert named == "first.contoso.com" and not environment["runs"]
