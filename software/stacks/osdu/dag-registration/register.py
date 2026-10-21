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

"""Register the exact parsed catalog as system workflows, then activate its DAGs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import yaml
from load import declared_dags

HTTP_TIMEOUT = 20
POLL_SECONDS = 5


class RegistrationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def send(method: str, url: str, headers: dict, body: object = None) -> tuple[int, object]:
    data = (
        body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
    )
    request = Request(url, data=data, method=method)
    for name, value in headers.items():
        if name.lower() == "authorization":
            request.add_unredirected_header(name, value)
        else:
            request.add_header(name, value)
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT) as response:
            status, raw = response.status, response.read()
    except HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        payload = json.loads(raw) if raw else None
    except (ValueError, UnicodeDecodeError):
        payload = raw.decode(errors="replace")
    return status, payload


def access_token(status: int, payload: object, service: str) -> str:
    # Airflow 3's POST /auth/token answers 201 Created; Entra answers 200.
    if status not in (200, 201) or not isinstance(payload, dict) or not payload.get("access_token"):
        raise RegistrationError(
            "authentication_failed", f"{service} token endpoint returned HTTP {status}"
        )
    return str(payload["access_token"])


def osdu_token() -> str:
    # Match the init chart's v1 exchange, including its accepted management audience.
    body = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": os.environ["AZURE_CLIENT_ID"],
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": Path(os.environ["AZURE_FEDERATED_TOKEN_FILE"]).read_text().strip(),
            "resource": "https://management.azure.com/",
        }
    ).encode()
    status, payload = send(
        "POST",
        f"https://login.microsoftonline.com/{quote(os.environ['AZURE_TENANT_ID'], safe='')}/oauth2/token",
        {"Content-Type": "application/x-www-form-urlencoded"},
        body,
    )
    return access_token(status, payload, "OSDU")


def airflow_token(base_url: str) -> str:
    status, payload = send(
        "POST",
        base_url + "/auth/token",
        {"Content-Type": "application/json"},
        {"username": os.environ["AIRFLOW_USERNAME"], "password": os.environ["AIRFLOW_PASSWORD"]},
    )
    return access_token(status, payload, "Airflow")


def response_excerpt(payload: object, bearer: str) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    if bearer:
        text = text.replace(bearer, "[redacted]")
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+\b", "[redacted]", text)
    return " ".join(text.split())[:200]


class Api:
    def __init__(
        self, base_url: str, token: Callable[[], str], deadline: float, *, service: str = "API"
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.bearer = ""
        self.deadline = deadline
        self.service = service

    def request(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        partition: str | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> tuple[int, object]:
        url = self.base_url + path
        refreshed = False
        previous = ""
        last_response = ""
        detail = f"{method} {url}: request deadline exceeded"
        while True:
            if time.monotonic() >= self.deadline:
                raise RegistrationError("request_failed", detail)
            try:
                if not self.bearer:
                    self.bearer = self.token()
                    if time.monotonic() >= self.deadline:
                        raise RegistrationError("request_failed", detail)
                headers = {
                    "Authorization": "Bearer " + self.bearer,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
                if partition is not None:
                    headers["data-partition-id"] = partition
                status, payload = send(method, url, headers, body)
                if status in expected:
                    return status, payload
                detail = (
                    f"{method} {url} returned HTTP {status}: "
                    f"{response_excerpt(payload, self.bearer)}"
                )
                last_response = detail
                if status == 401 and not refreshed:
                    self.bearer = ""
                    refreshed = True
                    continue
                authorization_wait = self.service in ("Workflow", "Partition") and status in (
                    401,
                    403,
                )
                if not (authorization_wait or status == 429 or status >= 500):
                    raise RegistrationError("request_failed", detail)
                waiting = (
                    f"Waiting for {self.service} authorization: {method} {path} HTTP {status}"
                    if authorization_wait
                    else ""
                )
                if waiting and waiting != previous:
                    print(waiting, flush=True)
                previous = waiting
            except (OSError, URLError) as exc:
                detail = f"{method} {url}: {type(exc).__name__}"
                if last_response:
                    detail += f"; last response: {last_response}"
                previous = ""
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise RegistrationError("request_failed", detail)
            time.sleep(min(POLL_SECONDS, remaining))


def collection(api: Api, path: str, field: str, **query: str) -> list[dict]:
    items: list[dict] = []
    while True:
        params = urlencode({**query, "limit": 100, "offset": len(items)})
        _, payload = api.request("GET", path + "?" + params)
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get(field), list)
            or not isinstance(payload.get("total_entries"), int)
            or any(not isinstance(item, dict) for item in payload[field])
        ):
            raise RegistrationError("invalid_response", f"{path}: invalid {field} collection")
        page = payload[field]
        items.extend(page)
        if len(items) >= payload["total_entries"]:
            return items
        if not page or len(items) > 10_000:
            raise RegistrationError(
                "invalid_response", f"{path}: incomplete or unbounded pagination"
            )


def parsed_since(dag: dict, since: datetime | None) -> bool:
    if not dag.get("last_parsed_time"):
        return False
    if since is None:
        return True
    try:
        parsed = datetime.fromisoformat(dag["last_parsed_time"].replace("Z", "+00:00"))
        return parsed >= since
    except (ValueError, TypeError):
        return False


def wait_for_parsed(
    airflow: Api, declared: set[str], timeout: float, since: datetime | None = None
) -> list[dict]:
    deadline = time.monotonic() + timeout
    previous = ""
    while True:
        dags = collection(airflow, "/api/v2/dags", "dags", exclude_stale="false", order_by="dag_id")
        errors = collection(airflow, "/api/v2/importErrors", "import_errors")
        if any(
            not isinstance(d.get("dag_id"), str) or not isinstance(d.get("is_stale"), bool)
            for d in dags
        ):
            raise RegistrationError("invalid_response", "Airflow omitted dag_id or is_stale")
        active = [dag for dag in dags if not dag["is_stale"]]
        parsed = {dag["dag_id"] for dag in active}
        pending = sorted(dag["dag_id"] for dag in active if not parsed_since(dag, since))
        broken = sorted(dag["dag_id"] for dag in active if dag.get("has_import_errors"))
        if parsed == declared and not errors and not pending and not broken:
            print(
                f"Airflow parsed exactly the {len(declared)} declared DAGs, 0 import errors",
                flush=True,
            )
            return dags
        detail = (
            f"missing={sorted(declared - parsed)}, undeclared={sorted(parsed - declared)}, "
            f"awaiting fresh parse={pending}, import errors="
            f"{[e.get('filename', '<unknown>') for e in errors] + broken}"
        )
        if time.monotonic() >= deadline:
            raise RegistrationError("parse_mismatch", detail)
        if detail != previous:
            print("Waiting for Airflow: " + detail, flush=True)
            previous = detail
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))


def partitions_from(api: Api) -> list[str]:
    _, payload = api.request("GET", "/partitions")
    if (
        not isinstance(payload, list)
        or not payload
        or any(not isinstance(name, str) or not name for name in payload)
    ):
        raise RegistrationError("invalid_response", "Partition returned no usable partition list")
    return sorted(set(payload))


def verify_system_record(workflow: Api, name: str, partitions: list[str]) -> None:
    for partition in partitions:
        _, record = workflow.request(
            "GET", "/workflow/" + quote(name, safe=""), partition=partition
        )
        _, listing = workflow.request(
            "GET", "/workflow?" + urlencode({"prefix": name}), partition=partition
        )
        if not isinstance(listing, list) or any(not isinstance(item, dict) for item in listing):
            raise RegistrationError(
                "invalid_response", f"{partition}/{name}: invalid workflow list"
            )
        matches = [item for item in listing if item.get("workflowName") == name]
        # Workflow hides isSystemWorkflow in JSON. Its list concatenates tenant
        # and system records; after a system POST/409, a second match is a shadow.
        if len(matches) > 1:
            raise RegistrationError(
                "registration_shadowed",
                f"{partition}/{name}: partition record shadows the system workflow",
            )
        if len(matches) != 1 or not isinstance(record, dict) or record != matches[0]:
            raise RegistrationError(
                "registration_unverified", f"{partition}/{name}: cannot prove the system read-back"
            )
        instructions = record.get("registrationInstructions")
        dag_name = instructions.get("dagName") if isinstance(instructions, dict) else None
        if dag_name != name:
            raise RegistrationError(
                "registration_conflict",
                f"{partition}/{name}: system dagName is {dag_name!r}, expected {name!r}",
            )


def set_paused(airflow: Api, name: str, paused: bool) -> None:
    _, body = airflow.request(
        "PATCH", "/api/v2/dags/" + quote(name, safe=""), {"is_paused": paused}
    )
    if (
        not isinstance(body, dict)
        or body.get("dag_id") != name
        or body.get("is_paused") is not paused
    ):
        raise RegistrationError(
            "activation_failed", f"{name}: Airflow did not confirm is_paused={paused}"
        )


def register(
    catalog: object,
    airflow: Api,
    workflow: Api,
    partition_api: Api,
    *,
    timeout: float = 900,
    since: datetime | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    declared = declared_dags(catalog)
    wait_for_parsed(airflow, declared, timeout, since)
    partitions = partitions_from(partition_api)
    for name in sorted(declared):
        status, _ = workflow.request(
            "POST",
            "/workflow/system",
            {
                "workflowName": name,
                "description": "SPI core DAG: " + name,
                "registrationInstructions": {"dagName": name},
            },
            expected=(200, 201, 409),
        )
        verify_system_record(workflow, name, partitions)
        print(
            f"{name}: " + ("verified existing system workflow" if status == 409 else "created"),
            flush=True,
        )
    # Registration can take time across partitions. Recheck before any activation.
    dags = wait_for_parsed(airflow, declared, max(0, deadline - time.monotonic()), since)
    retired = sorted(dag["dag_id"] for dag in dags if dag["dag_id"] not in declared)
    for name in retired:
        set_paused(airflow, name, True)
        print(f"{name}: paused (not in catalog)", flush=True)
    for name in sorted(declared):
        set_paused(airflow, name, False)
    print(
        f"dag-registration outcome: registered {len(declared)}, unpaused {len(declared)}, paused {len(retired)}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path("/opt/spi/catalog/catalog.yaml"))
    args = parser.parse_args()
    try:
        timeout = int(os.environ.get("WAIT_SECONDS", "900"))
        if timeout <= 0:
            raise ValueError("WAIT_SECONDS must be positive")
        since = datetime.now(timezone.utc)
        deadline = time.monotonic() + timeout
        airflow_url = os.environ["AIRFLOW_URL"].rstrip("/")
        airflow = Api(airflow_url, lambda: airflow_token(airflow_url), deadline, service="Airflow")
        workflow = Api(os.environ["WORKFLOW_URL"], osdu_token, deadline, service="Workflow")
        partition = Api(os.environ["PARTITION_URL"], osdu_token, deadline, service="Partition")
        catalog = yaml.safe_load(args.catalog.read_text(encoding="utf-8"))
        register(catalog, airflow, workflow, partition, timeout=timeout, since=since)
    except RegistrationError as exc:
        print(f"dag-registration outcome: {exc.code}: {exc}", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:
        print(f"dag-registration outcome: failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
