# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""The contract a service publishes, read as the rows a test can protect."""

import io
import json
import urllib.error
from email.message import Message

import pytest

from spi import suite_contract
from spi.suite_contract import ContractUnavailable, fetch_contract, read_contract

DOCUMENT = {
    "openapi": "3.1.0",
    "info": {"title": "Partition Service"},
    "paths": {
        "/partitions/{partitionId}": {
            "parameters": [{"name": "partitionId"}],
            "get": {
                "summary": "Get Partition Info",
                "responses": {
                    "404": {"description": "Not Found"},
                    "200": {"description": "OK"},
                    "500": {"description": "Internal Server Error"},
                    "503": {},
                    "5XX": {},
                    "20": {"description": "not a status"},
                    "2000": {"description": "not a status either"},
                    "default": {"description": "anything else"},
                },
            },
            "delete": {"responses": {"204": {"description": "No Content"}}},
        },
        "/info": {"get": {"responses": {"200": "not an object"}}},
        "/broken": "not an object",
    },
}


def test_a_row_is_an_operation_and_a_response_it_documents_below_500():
    contract = read_contract(json.dumps(DOCUMENT).encode(), "https://gw/api/partition/v1/api-docs")

    assert [row["id"] for row in contract["rows"]] == [
        "GET /partitions/{partitionId} :: 200",
        "GET /partitions/{partitionId} :: 404",
        "DELETE /partitions/{partitionId} :: 204",
        "GET /info :: 200",
    ]
    assert contract["rows"][1] == {
        "id": "GET /partitions/{partitionId} :: 404",
        "operation": "GET /partitions/{partitionId}",
        "behavior": "404",
        "summary": "Get Partition Info",
        "response": "Not Found",
    }
    assert (contract["title"], contract["version"]) == ("Partition Service", "3.1.0")


@pytest.mark.parametrize(
    "body",
    [b"<html>sign in</html>", b"[]", b'{"paths": {}}', b'{"paths": {"/a": {"get": {}}}}', b"\xff"],
)
def test_a_document_with_no_row_is_no_contract(body):
    with pytest.raises(ContractUnavailable):
        read_contract(body, "https://gw/api-docs")


class Answer(io.BytesIO):
    """The document, from the address that served it once redirects were followed."""

    def __init__(self, url: str = "https://gw.example/api/partition/v1/api-docs"):
        super().__init__(json.dumps(DOCUMENT).encode())
        self.url = url

    def geturl(self) -> str:
        return self.url


class TestFetch:
    def test_the_contract_is_read_from_the_services_own_address(self, monkeypatch):
        seen = {}

        def urlopen(request, timeout):
            seen.update(url=request.full_url, timeout=timeout)
            return Answer()

        monkeypatch.setattr(suite_contract.urllib.request, "urlopen", urlopen)

        contract = fetch_contract("https://gw.example/api/partition/v1/")

        assert seen["url"] == "https://gw.example/api/partition/v1/api-docs"
        assert seen["timeout"] > 0
        assert contract["source"] == seen["url"] and len(contract["rows"]) == 4

    @pytest.mark.parametrize("endpoint", ["", "http://gw.example/api/partition/v1/", "file:///etc"])
    def test_an_address_that_is_not_https_is_never_opened(self, monkeypatch, endpoint):
        def urlopen(request, timeout):
            raise AssertionError("opened")

        monkeypatch.setattr(suite_contract.urllib.request, "urlopen", urlopen)

        with pytest.raises(ContractUnavailable):
            fetch_contract(endpoint)

    @pytest.mark.parametrize(
        "failure",
        [
            urllib.error.HTTPError("https://gw", 401, "Unauthorized", Message(), None),
            urllib.error.URLError("no route"),
            TimeoutError("timed out"),
        ],
    )
    def test_a_service_that_does_not_answer_publishes_no_contract(self, monkeypatch, failure):
        def urlopen(request, timeout):
            raise failure

        monkeypatch.setattr(suite_contract.urllib.request, "urlopen", urlopen)

        with pytest.raises(ContractUnavailable):
            fetch_contract("https://gw.example/api/partition/v1/")

    def test_an_answer_redirected_off_https_is_no_contract(self, monkeypatch):
        monkeypatch.setattr(
            suite_contract.urllib.request,
            "urlopen",
            lambda request, timeout: Answer("http://gw.example/api/partition/v1/api-docs"),
        )

        with pytest.raises(ContractUnavailable, match="not https"):
            fetch_contract("https://gw.example/api/partition/v1/")

    def test_a_document_past_the_size_limit_is_refused(self, monkeypatch):
        monkeypatch.setattr(suite_contract, "CONTRACT_BYTES", 64)
        monkeypatch.setattr(
            suite_contract.urllib.request, "urlopen", lambda request, timeout: Answer()
        )

        with pytest.raises(ContractUnavailable, match="larger"):
            fetch_contract("https://gw.example/api/partition/v1/")
