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

"""The contract a service publishes, as rows a test can protect.

A row is one operation and one response the service documents for it. The
rows come from the OpenAPI document the deployed service serves.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

CONTRACT_PATH = "api-docs"
CONTRACT_TIMEOUT_SECONDS = 15
CONTRACT_BYTES = 8 * 1024 * 1024
METHODS = ("get", "put", "post", "patch", "delete", "head", "options")
TEXT_LIMIT = 200
# A documented response a suite can set out to prove: not a server error, not `default`.
_RESPONSE = re.compile(r"[1-4][0-9X]{2}")


class ContractUnavailable(RuntimeError):
    """The service published no contract this CLI can read."""


def row_id(operation: str, behavior: str) -> str:
    return f"{operation} :: {behavior}"


def _text(value: object) -> str:
    return " ".join(str(value or "").split())[:TEXT_LIMIT]


def contract_rows(document: dict) -> list[dict]:
    """One row per operation and documented response below 500, in document order."""

    paths = document.get("paths")
    rows: list[dict] = []
    for path, item in paths.items() if isinstance(paths, dict) else ():
        for method in METHODS:
            operation = item.get(method) if isinstance(item, dict) else None
            if not isinstance(operation, dict):
                continue
            name = f"{method.upper()} {path}"
            responses = operation.get("responses")
            for code, response in sorted(responses.items()) if isinstance(responses, dict) else ():
                if not _RESPONSE.fullmatch(str(code)):
                    continue
                described = response.get("description") if isinstance(response, dict) else ""
                rows.append(
                    {
                        "id": row_id(name, str(code)),
                        "operation": name,
                        "behavior": str(code),
                        "summary": _text(operation.get("summary")),
                        "response": _text(described),
                    }
                )
    return rows


def read_contract(body: bytes, source: str) -> dict:
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContractUnavailable(f"{source} is not JSON: {exc}") from exc
    rows = contract_rows(document) if isinstance(document, dict) else []
    if not rows:
        raise ContractUnavailable(f"{source} documents no operation")
    info = document.get("info") if isinstance(document.get("info"), dict) else {}
    return {
        "source": source,
        "title": _text(info.get("title")),
        "version": _text(document.get("openapi") or document.get("swagger")),
        "rows": rows,
    }


def fetch_contract(endpoint: str) -> dict:
    """The contract the service at ``endpoint`` serves; raise ContractUnavailable otherwise."""

    source = endpoint.rstrip("/") + "/" + CONTRACT_PATH
    if not source.startswith("https://"):
        raise ContractUnavailable(f"{source} is not an https address")
    request = urllib.request.Request(source, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=CONTRACT_TIMEOUT_SECONDS) as response:  # nosec B310
            # A redirect is followed after the address was checked.
            if not response.geturl().startswith("https://"):
                raise ContractUnavailable(f"{source} answers from an address that is not https")
            body = response.read(CONTRACT_BYTES + 1)
    except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
        raise ContractUnavailable(f"{source}: {exc}") from exc
    if len(body) > CONTRACT_BYTES:
        raise ContractUnavailable(f"{source} is larger than {CONTRACT_BYTES} bytes")
    return read_contract(body, source)
