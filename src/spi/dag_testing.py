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

"""Stack-owned Workflow smoke: execute Echo, then ingest and verify caller provenance."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .bootstrap import ClusterConfigError
from .identity import IdentityError, decode_claims, person_token, projected_user_id
from .pins import PinError, decode_pins, read_lock, require_deployable
from .suite_report import redactor
from .testing import SuiteNotRun, check_target, collect_facts, target_state
from .token import TokenError, mint_token
from .users import IMPERSONATION_GROUP

MANIFEST_KIND = "osdu:wks:Manifest:1.0.0"
RECORD_KIND = "osdu:wks:reference-data--UnitOfMeasure:1.0.0"
DAG_IDS = ("Echo_DAG", "Osdu_ingest")
TARGET_SERVICES = ("workflow", "storage")
HTTP_TIMEOUT = 30
POLL_INTERVAL = 5.0
SUCCESS_STATES = {"success", "finished"}
ACTIVE_STATES = {"submitted", "queued", "running", "scheduled"}


@dataclass(frozen=True)
class Caller:
    name: str
    user_id: str
    token: str = field(repr=False)


@dataclass
class DagCheck:
    dag: str
    state: str = "planned"
    run_id: str = ""
    code: str = ""
    detail: str = ""
    record_id: str = ""
    create_user: str = ""
    seconds: float = 0

    def as_dict(self) -> dict:
        return {
            "dag": self.dag,
            "state": self.state,
            "runId": self.run_id or None,
            "code": self.code or None,
            "detail": self.detail,
            "recordId": self.record_id or None,
            "createUser": self.create_user or None,
            "seconds": round(self.seconds, 3),
        }


@dataclass(frozen=True)
class DagSmokeResult:
    outcome: str
    code: str | None
    detail: str
    exit_code: int
    caller: str
    user_id: str
    partition: str
    environment: dict
    checks: tuple[DagCheck, ...]

    def facts(self) -> dict:
        return {
            "mode": "stack",
            "check": "core-dags",
            "caller": {"as": self.caller, "id": self.user_id},
            "partition": self.partition,
            "environment": self.environment,
            "checks": [check.as_dict() for check in self.checks],
            "tests": {
                "passed": sum(c.state == "passed" for c in self.checks),
                "failed": sum(c.state == "failed" for c in self.checks),
                "skipped": sum(c.state == "skipped" for c in self.checks),
            },
        }


class CheckFailed(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def _refuse(code: str, detail: str) -> SuiteNotRun:
    return SuiteNotRun(code, detail, exit_code=2)


def authenticate(name: str) -> Caller:
    try:
        if name == "me":
            person = person_token()
            return Caller(name, person.user_id, person.access_token)
        minted = mint_token(caller="deploy")
        user_id, _ = projected_user_id(decode_claims(minted.access_token))
        if not user_id:
            raise SuiteNotRun(
                "identity_unknown", "The minted token has no caller the mesh projects."
            )
        return Caller(name, user_id, minted.access_token)
    except (IdentityError, TokenError, ClusterConfigError) as exc:
        raise SuiteNotRun("token_failed", str(exc)) from exc


class Api:
    def __init__(self, endpoints: dict[str, str], caller: Caller, partition: str):
        self.endpoints = endpoints
        self.caller = caller
        self.partition = partition
        self.redact = redactor([caller.token], named=False)

    def detail(self, status: int, body: Any) -> str:
        return self.redact(f"HTTP {status}: {body}")[:400]

    def request(
        self, service: str, method: str, path: str, body: dict | None = None
    ) -> tuple[int, Any]:
        url = self.endpoints[service].rstrip("/") + path
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "data-partition-id": self.partition,
            },
        )
        request.add_unredirected_header("Authorization", f"Bearer {self.caller.token}")
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise CheckFailed("request_failed", self.redact(f"{method} {url}: {exc}")) from None
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = raw.decode(errors="replace")
        return status, payload


def build_manifest(partition: str, domain: str, legal_tag: str, nonce: str) -> dict:
    """One new reference record, without caller or system-managed metadata."""
    marker = "spi-dag-smoke-" + nonce
    return {
        "kind": MANIFEST_KIND,
        "ReferenceData": [
            {
                "id": f"{partition}:reference-data--UnitOfMeasure:{marker}",
                "kind": RECORD_KIND,
                "acl": {
                    "owners": [f"data.default.owners@{partition}.{domain}"],
                    "viewers": [f"data.default.viewers@{partition}.{domain}"],
                },
                "legal": {"legaltags": [legal_tag], "otherRelevantDataCountries": ["US"]},
                "data": {"Name": marker, "Code": marker},
            }
        ],
    }


def _target(facts: dict, partition: str | None) -> tuple[dict, dict[str, str], str]:
    if facts.get("environment", {}).get("profile") != "core":
        raise _refuse("profile_not_core", "Core DAG smoke requires a core environment.")
    partitions = facts.get("partitions") or []
    selected = (
        next((p for p in partitions if p.get("name") == partition), None)
        if partition
        else (
            next((p for p in partitions if p.get("primary")), None) or next(iter(partitions), None)
        )
    )
    if selected is None:
        raise _refuse(
            "unknown_partition", f"Partition {partition!r} is not declared by this environment."
        )
    if not selected.get("legal_tag"):
        raise _refuse(
            "legal_tag_missing", f"{selected['name']}: the default legal tag is not seeded."
        )
    domain = facts.get("entitlements_domain", "")
    if not domain:
        raise _refuse("env_not_ready", "Entitlements has no published domain.")
    endpoints = {}
    for service in ("workflow", "storage", "schema", "entitlements"):
        endpoint = facts.get("endpoints", {}).get(service, "")
        parsed = urllib.parse.urlsplit(endpoint)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise _refuse("env_not_ready", f"{service} has no usable HTTP endpoint in spi info.")
        endpoints[service] = endpoint
    return selected, endpoints, domain


def _capture_targets() -> dict[str, tuple[dict, str]]:
    try:
        lock = read_lock()
        if lock is None:
            raise PinError("The image lock is missing.")
        pins = decode_pins(lock)
        targets = {}
        for service in TARGET_SERVICES:
            pin = pins.get(service)
            if pin and pin.ephemeral:
                raise _refuse("service_borrowed", f"{service} is borrowed by run {pin.run_id}.")
            state = target_state(lock, service)
            targets[service] = (state, check_target(service, state))
        return targets
    except PinError as exc:
        raise SuiteNotRun("cluster_unreadable", str(exc)) from exc


def _preflight(api: Api, record: dict) -> None:
    status, groups = api.request("entitlements", "GET", "/groups")
    if status != 200 or not isinstance(groups, dict) or not isinstance(groups.get("groups"), list):
        raise _refuse("caller_not_authorized", api.detail(status, groups))
    names = {
        g.get("name") or str(g.get("email", "")).partition("@")[0]
        for g in groups["groups"]
        if isinstance(g, dict)
    }
    if IMPERSONATION_GROUP not in names:
        raise _refuse(
            "impersonation_missing",
            f"{api.partition}: {api.caller.user_id} is not in {IMPERSONATION_GROUP}. "
            "Reapply the intended spi users editor/admin/ops preset, or wait for the deploy identity's members seed.",
        )
    for dag in DAG_IDS:
        status, workflow = api.request("workflow", "GET", "/workflow/" + dag)
        if status != 200 or not isinstance(workflow, dict):
            raise _refuse(
                "dag_not_registered",
                f"{dag}: {api.detail(status, workflow)}; inspect spi-osdu-dag-registration.",
            )
        instructions = workflow.get("registrationInstructions")
        if not isinstance(instructions, dict) or instructions.get("dagName") != dag:
            raise _refuse(
                "registration_conflict", f"{api.partition}/{dag} resolves to another DAG."
            )
    for kind in (MANIFEST_KIND, RECORD_KIND):
        status, schema = api.request(
            "schema", "GET", "/schema/" + urllib.parse.quote(kind, safe="")
        )
        if status != 200 or not isinstance(schema, dict):
            raise _refuse(
                "schema_missing", f"{kind}: {api.detail(status, schema)}; inspect schema-load."
            )
    status, body = api.request(
        "storage", "GET", "/records/" + urllib.parse.quote(record["id"], safe="")
    )
    if status == 200:
        raise _refuse("record_exists", f"Refusing to overwrite smoke record {record['id']}.")
    if status != 404:
        raise _refuse("storage_unavailable", api.detail(status, body))


def _submitted_by(api: Api, body: dict, dag: str) -> None:
    if body.get("submittedBy") != api.caller.user_id:
        raise CheckFailed(
            "caller_mismatch",
            f"{dag}: submittedBy={body.get('submittedBy')!r}, expected {api.caller.user_id!r}",
        )


def _run_workflow(
    api: Api, check: DagCheck, context: dict, timeout: int, progress: Callable[[str], None]
) -> None:
    check.run_id = str(uuid.uuid4())
    path = "/workflow/" + check.dag + "/workflowRun"
    status, body = api.request(
        "workflow", "POST", path, {"runId": check.run_id, "executionContext": context}
    )
    if status not in (200, 201) or not isinstance(body, dict):
        raise CheckFailed("workflow_rejected", f"{check.dag}: {api.detail(status, body)}")
    if body.get("runId") != check.run_id:
        raise CheckFailed(
            "invalid_response", f"{check.dag}: Workflow did not return the requested runId."
        )
    _submitted_by(api, body, check.dag)
    progress(f"{check.dag}: submitted run {check.run_id}")
    deadline = time.monotonic() + timeout
    previous = ""
    while True:
        status, body = api.request("workflow", "GET", path + "/" + check.run_id)
        if status == 200 and isinstance(body, dict):
            if body.get("runId") != check.run_id:
                raise CheckFailed(
                    "invalid_response", f"{check.dag}: Workflow returned another runId."
                )
            _submitted_by(api, body, check.dag)
            state = str(body.get("status", "")).lower()
            if state in SUCCESS_STATES:
                return
            if state == "failed":
                raise CheckFailed("workflow_failed", f"{check.dag} run {check.run_id} failed.")
            if state not in ACTIVE_STATES:
                raise CheckFailed(
                    "invalid_response", f"{check.dag}: unknown Workflow status {state!r}."
                )
            detail = state
        elif status in (404, 429) or status >= 500:
            detail = api.detail(status, body)
        else:
            raise CheckFailed("workflow_unreadable", f"{check.dag}: {api.detail(status, body)}")
        if detail != previous:
            progress(f"{check.dag}: {detail}")
            previous = detail
        if time.monotonic() >= deadline:
            raise CheckFailed(
                "workflow_timeout", f"{check.dag} run {check.run_id} timed out ({detail})."
            )
        time.sleep(min(POLL_INTERVAL, max(0, deadline - time.monotonic())))


def _verify_record(api: Api, record: dict, timeout: int) -> str:
    deadline = time.monotonic() + min(timeout, 60)
    path = "/records/" + urllib.parse.quote(record["id"], safe="")
    while True:
        status, body = api.request("storage", "GET", path)
        if status == 200 and isinstance(body, dict):
            if (
                body.get("id") != record["id"]
                or body.get("kind") != record["kind"]
                or not isinstance(body.get("data"), dict)
                or any(body["data"].get(key) != value for key, value in record["data"].items())
            ):
                raise CheckFailed(
                    "record_mismatch", f"{record['id']}: stored record differs from the manifest."
                )
            if body.get("createUser") != api.caller.user_id:
                raise CheckFailed(
                    "caller_mismatch",
                    f"{record['id']}: createUser={body.get('createUser')!r}, expected {api.caller.user_id!r}",
                )
            return body["createUser"]
        if status not in (404, 429) and status < 500:
            raise CheckFailed("record_unreadable", f"{record['id']}: {api.detail(status, body)}")
        if time.monotonic() >= deadline:
            raise CheckFailed(
                "record_missing", f"{record['id']}: no stored record after the successful run."
            )
        time.sleep(min(POLL_INTERVAL, max(0, deadline - time.monotonic())))


def _environment_id(facts: dict) -> tuple:
    environment = facts.get("environment") or {}
    return (
        environment.get("name"),
        environment.get("profile"),
        (environment.get("running") or {}).get("commit"),
    )


def run_dags(
    *,
    caller: str = "deploy",
    partition: str | None = None,
    timeout: int = 900,
    dry_run: bool = False,
    progress: Callable[[str], None] = lambda _: None,
) -> DagSmokeResult:
    if caller not in ("deploy", "me") or timeout < 1:
        raise SuiteNotRun("invalid_options", "Choose deploy or me and a positive timeout.")
    try:
        require_deployable()
    except PinError as exc:
        raise _refuse("not_deployable", str(exc)) from exc
    facts = collect_facts()
    selected, endpoints, domain = _target(facts, partition)
    targets = _capture_targets()
    actor = authenticate(caller)
    api = Api(endpoints, actor, selected["name"])
    manifest = build_manifest(selected["name"], domain, selected["legal_tag"], uuid.uuid4().hex)
    record = manifest["ReferenceData"][0]
    try:
        _preflight(api, record)
    except CheckFailed as exc:
        raise SuiteNotRun(exc.code, str(exc)) from exc
    checks = (DagCheck(DAG_IDS[0]), DagCheck(DAG_IDS[1], record_id=record["id"]))
    result_fields = {
        "caller": caller,
        "user_id": actor.user_id,
        "partition": selected["name"],
        "environment": facts.get("environment", {}),
        "checks": checks,
    }
    if dry_run:
        return DagSmokeResult(
            "planned",
            None,
            "Two Workflow runs; one retained smoke record. Nothing ran.",
            0,
            **result_fields,
        )
    progress(f"Core DAG smoke as {actor.user_id} in {selected['name']}; retains {record['id']}")
    for check in checks:
        start = time.monotonic()
        context: dict = {"data-partition-id": selected["name"]}
        if check.dag == "Osdu_ingest":
            context["manifest"] = manifest
        try:
            _run_workflow(api, check, context, timeout, progress)
            if check.dag == "Osdu_ingest":
                check.create_user = _verify_record(api, record, timeout)
            check.state = "passed"
            check.detail = (
                "createUser matches the caller" if check.create_user else "Workflow run completed"
            )
        except CheckFailed as exc:
            check.state, check.code, check.detail = "failed", exc.code, api.redact(str(exc))
        check.seconds = time.monotonic() - start
        progress(f"{check.dag}: {check.state}: {check.detail}")
        if check.state == "failed":
            if check.dag == "Echo_DAG":
                checks[1].state, checks[1].detail = (
                    "skipped",
                    "Echo failed; ingestion was not started",
                )
            break
    try:
        for service, (expected, revision) in targets.items():
            check_target(service, expected, revision=revision, after="core DAG smoke")
        require_deployable()
        if _environment_id(collect_facts()) != _environment_id(facts):
            raise _refuse(
                "target_changed", "The environment or stack revision changed during the DAG smoke."
            )
    except (SuiteNotRun, PinError) as exc:
        return DagSmokeResult("refused", "target_changed", str(exc), 2, **result_fields)
    failed = next((check for check in checks if check.state == "failed"), None)
    if failed:
        return DagSmokeResult("failed", failed.code, failed.detail, 3, **result_fields)
    return DagSmokeResult(
        "passed",
        None,
        "Echo and manifest ingestion passed; createUser is the caller.",
        0,
        **result_fields,
    )
