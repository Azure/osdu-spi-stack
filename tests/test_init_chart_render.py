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
legal-init contract, then executes the rendered init_legal.py against a
routed fake of urlopen so every typed outcome is covered. Skipped when Helm
is not installed.
"""

import ast
import email.message
import importlib.util
import io
import json
import shutil
import sys
import time
import types
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from spi.shell import run_process

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "software" / "charts" / "osdu-spi-init"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="Helm not installed")


def _render(partitions: list[str], extra_sets: dict[str, str] | None = None) -> list[dict]:
    set_args = []
    for i, partition in enumerate(partitions):
        set_args += ["--set", f"partitions[{i}]={partition}"]
    for key, value in (extra_sets or {}).items():
        set_args += ["--set", f"{key}={value}"]
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


@pytest.fixture(scope="module")
def init_scripts() -> dict[str, str]:
    """The osdu-spi-init-scripts ConfigMap data, rendered once for the module."""
    docs = _render(["opendes"])
    configmap = next(
        doc
        for doc in docs
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "osdu-spi-init-scripts"
    )
    return configmap["data"]


def test_legal_init_renders_one_job_per_partition():
    docs = _render(["opendes", "second"])
    jobs = _jobs(docs, "legal-init")
    assert [job["metadata"]["name"] for job in jobs] == [
        "legal-init-opendes",
        "legal-init-second",
    ]


def test_component_split_matches_the_two_releases():
    """The gating osdu-spi-init release must never render a legal Job, and the
    non-gating osdu-spi-legal release must render nothing but legal Jobs:
    helm-controller waits for every Job in a release, so this split is what
    keeps a legal failure from blocking schema-load."""
    gating = _render(["opendes"], {"legalEnabled": "false"})
    gating_names = sorted(doc["metadata"]["name"] for doc in gating)
    assert gating_names == [
        "entitlements-init-opendes",
        "osdu-spi-init-partition-records",
        "osdu-spi-init-scripts",
        "partition-init-opendes",
    ]

    legal = _render(["opendes"], {"coreEnabled": "false"})
    assert [doc["metadata"]["name"] for doc in legal] == ["legal-init-opendes"]


def _script_constants(source: str) -> dict:
    """Module-level literal constants, read without importing: init_legal.py
    pulls auth and wait off /scripts, which only exists inside the Job."""
    return {
        target.id: node.value.value
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def test_legal_release_declares_no_volume_it_does_not_own():
    """The legal release renders no ConfigMaps of its own. It mounts the core
    release's scripts, which dependsOn guarantees, but must not declare the
    partition-records volume it never mounts: kubelet fails a pod on any
    declared volume whose ConfigMap is missing, with no outcome line."""
    legal = _jobs(_render(["opendes"], {"coreEnabled": "false"}), "legal-init")[0]
    pod = legal["spec"]["template"]["spec"]
    assert [volume["name"] for volume in pod["volumes"]] == ["scripts"]

    core = _jobs(_render(["opendes"], {"legalEnabled": "false"}), "partition-init")[0]
    core_pod = core["spec"]["template"]["spec"]
    assert [volume["name"] for volume in core_pod["volumes"]] == [
        "scripts",
        "partition-records",
    ]


def test_legal_init_deadline_covers_its_wait_budget(init_scripts):
    """The legal Job must outlive init_legal.py's worst case, recomputed from
    the script's constants: every gate's attempts at a delay plus a socket
    timeout, then the authenticated calls. A shorter deadline kills the pod
    before a typed outcome is printed."""
    const = _script_constants(init_scripts["init_legal.py"])
    attempts = (
        const["LEGAL_INFO_ATTEMPTS"]
        + const["PARTITION_RECORD_ATTEMPTS"]
        + const["LEGAL_AUTHZ_ATTEMPTS"]
    )
    budget = (
        attempts * (const["WAIT_DELAY"] + const["WAIT_SOCKET_TIMEOUT"]) + const["REQUEST_BUDGET"]
    )

    docs = _render(["opendes"])
    legal = _jobs(docs, "legal-init")[0]
    core = _jobs(docs, "partition-init")[0]
    assert legal["spec"]["activeDeadlineSeconds"] >= budget
    assert core["spec"]["activeDeadlineSeconds"] == 600


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


def test_init_scripts_compile(init_scripts):
    """The scripts ConfigMap embeds Python sources as YAML block scalars; a
    stray indent or quote breaks them only at Job runtime. Compile each one."""
    assert "init_legal.py" in init_scripts
    for name, source in init_scripts.items():
        compile(source, name, "exec")


# --- init_legal.py execution harness -----------------------------------------

_BASE_ENV = {
    "PARTITION": "opendes",
    "LEGAL_TAG": "opendes-demo-legaltag",
    "KEYVAULT_NAME": "kv-test",
    "LEGAL_HOST": None,
    "PARTITION_HOST": None,
}

_BLOB_ENDPOINT = "https://acct.blob.core.windows.net/"

# Must match the legal service's AZURE_STORAGE_CONTAINER_NAME (services/legal.yaml).
_CONFIG_CONTAINER = "legal-service-azure-configuration"


class _Unrouted(BaseException):
    """Not an Exception, so a routing gap surfaces instead of being swallowed
    by the script's own `except Exception` boundary as unexpected_error."""


@dataclass
class _Call:
    route: str
    url: str
    method: str
    headers: dict[str, str]
    body: bytes | None


@dataclass
class _Result:
    exit_code: int
    stdout: str
    calls: list[_Call]

    def routed(self, route: str) -> list[_Call]:
        return [call for call in self.calls if call.route == route]


class _FakeResponse:
    def __init__(self, code: int, body: bytes):
        self._code = code
        self._body = body

    def getcode(self) -> int:
        return self._code

    def read(self) -> bytes:
        return self._body


def _responds(code: int, body: bytes = b"{}"):
    def handler(url: str) -> _FakeResponse:
        return _FakeResponse(code, body)

    return handler


def _http_error(code: int, body: bytes = b"denied"):
    def handler(url: str):
        raise urllib.error.HTTPError(url, code, "error", email.message.Message(), io.BytesIO(body))

    return handler


def _transport_error(reason: str = "connection refused"):
    def handler(url: str):
        raise urllib.error.URLError(reason)

    return handler


# The properties init_legal.py POSTs. Pinned to the script itself by
# test_legal_init_accepts_an_existing_tag_that_matches, so a change to
# tag_body() cannot leave the verification fixtures agreeing with nothing.
_TAG_PROPERTIES = {
    "countryOfOrigin": ["US"],
    "contractId": "No Contract Related",
    "expirationDate": "2099-01-01",
    "dataType": "Public Domain Data",
    "originator": "OSDU",
    "securityClassification": "Public",
    "exportClassification": "EAR99",
    "personalData": "No Personal Data",
}


def _existing_tag(**overrides) -> bytes:
    return json.dumps(
        {"name": "opendes-demo-legaltag", "properties": {**_TAG_PROPERTIES, **overrides}}
    ).encode()


_DEFAULT_ROUTES = {
    "legal_info": _responds(200),
    "partition_record": _responds(200),
    "keyvault": _responds(200, json.dumps({"value": _BLOB_ENDPOINT}).encode()),
    "blob_upload": _responds(201),
    "legaltags_get": _responds(200, b"[]"),
    "legaltags_post": _responds(201, b'{"name": "opendes-demo-legaltag"}'),
    "legaltags_by_name": _responds(200, _existing_tag()),
    "legaltags_validate": _responds(200, b'{"invalidLegalTags": []}'),
}


def _fake_get_token(resource: str = "https://management.azure.com/") -> str:
    return "fake-token"


def _route_of(url: str, method: str) -> str:
    if url.endswith("/api/legal/v1/info"):
        return "legal_info"
    if "/api/partition/v1/partitions/" in url:
        return "partition_record"
    if ".vault.azure.net/secrets/" in url:
        return "keyvault"
    if url.endswith("/Legal_COO.json"):
        return "blob_upload"
    if url.endswith("/legaltags"):
        return "legaltags_get" if method == "GET" else "legaltags_post"
    if url.endswith("/legaltags:validate"):
        return "legaltags_validate"
    if "/api/legal/v1/legaltags/" in url:
        return "legaltags_by_name"
    raise _Unrouted(f"{method} {url}")


@pytest.fixture
def legal_init(init_scripts, tmp_path, monkeypatch, capsys):
    """Return a `run()` that executes the rendered init_legal.py in-process.

    The real wait.py is imported so the retry gates are genuinely exercised;
    auth.py is replaced by a stub so no token is ever minted.
    """
    wait_path = tmp_path / "wait.py"
    wait_path.write_text(init_scripts["wait.py"], encoding="utf-8")
    script_path = tmp_path / "init_legal.py"
    script_path.write_text(init_scripts["init_legal.py"], encoding="utf-8")

    def run(routes: dict | None = None, env: dict | None = None, token=None) -> _Result:
        for name, value in {**_BASE_ENV, **(env or {})}.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)

        table = {**_DEFAULT_ROUTES, **(routes or {})}
        calls: list[_Call] = []

        def urlopen(req, timeout=None):
            route = _route_of(req.full_url, req.get_method())
            headers = {key.lower(): value for key, value in req.headers.items()}
            calls.append(_Call(route, req.full_url, req.get_method(), headers, req.data))
            return table[route](req.full_url)

        auth = types.ModuleType("auth")
        auth.__dict__["get_token"] = token or _fake_get_token
        monkeypatch.setitem(sys.modules, "auth", auth)

        spec = importlib.util.spec_from_file_location("wait", wait_path)
        assert spec is not None and spec.loader is not None
        wait = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "wait", wait)
        spec.loader.exec_module(wait)

        # The script prepends /scripts to sys.path; hand it a copy to mutate.
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.setattr(time, "sleep", lambda seconds: None)
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)

        source = compile(script_path.read_text(encoding="utf-8"), str(script_path), "exec")
        capsys.readouterr()
        try:
            exec(source, {"__name__": "__main__", "__file__": str(script_path)})
            exit_code = 0
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 1
        return _Result(exit_code, capsys.readouterr().out, calls)

    return run


def test_legal_init_creates_tag(legal_init):
    result = legal_init()
    assert result.exit_code == 0
    assert "legal-init outcome: created" in result.stdout

    put = result.routed("blob_upload")[0]
    assert put.method == "PUT"
    assert put.url == f"{_BLOB_ENDPOINT.rstrip('/')}/{_CONFIG_CONTAINER}/Legal_COO.json"
    # Storage rejects a bearer-authorized request that carries no request date.
    assert put.headers["x-ms-date"].endswith("GMT")
    assert put.headers["x-ms-blob-type"] == "BlockBlob"

    # The staged catalog overrides legal's defaults, so keeping the tag's
    # country inside it means the tag validates regardless of what those
    # defaults carry for it.
    catalog_countries = {entry["alpha2"] for entry in json.loads(put.body)}
    post = result.routed("legaltags_post")[0]
    tag_countries = set(json.loads(post.body)["properties"]["countryOfOrigin"])
    assert tag_countries <= catalog_countries, (
        f"default tag declares {sorted(tag_countries - catalog_countries)} "
        "absent from the staged COO catalog"
    )


_CONFLICT = {"legaltags_post": _http_error(409, b"already exists")}


def test_legal_init_accepts_an_existing_tag_that_matches(legal_init):
    """A 409 is success only after the stored tag is checked, so the check has
    to actually run: both verification calls are asserted here, and the fixture
    they answer with is pinned to the body the script POSTs."""
    result = legal_init(_CONFLICT)
    assert result.exit_code == 0
    assert "legal-init outcome: already_exists" in result.stdout
    assert len(result.routed("legaltags_by_name")) == 1
    assert len(result.routed("legaltags_validate")) == 1

    post = result.routed("legaltags_post")[0]
    assert json.loads(post.body)["properties"] == _TAG_PROPERTIES


def test_legal_init_rejects_an_existing_tag_configured_differently(legal_init):
    """A taken name is not proof of convergence: a same-name tag carrying
    another country would let the acceptance suite reference a tag the stack
    never agreed to create."""
    result = legal_init(
        {**_CONFLICT, "legaltags_by_name": _responds(200, _existing_tag(countryOfOrigin=["MY"]))}
    )
    assert result.exit_code == 1
    assert "legal-init outcome: legal_tag_conflict" in result.stdout
    assert "countryOfOrigin" in result.stdout
    assert result.routed("legaltags_validate") == []


def test_legal_init_rejects_an_existing_tag_legal_calls_invalid(legal_init):
    """Matching properties are not enough; legal owns the validity verdict, so
    an expired tag with the right shape still fails."""
    invalid = json.dumps(
        {"invalidLegalTags": [{"name": "opendes-demo-legaltag", "reason": "Expired"}]}
    ).encode()
    result = legal_init({**_CONFLICT, "legaltags_validate": _responds(200, invalid)})
    assert result.exit_code == 1
    assert "legal-init outcome: legal_tag_conflict" in result.stdout
    assert "Expired" in result.stdout


def test_legal_init_reports_a_conflict_when_verification_cannot_run(legal_init):
    """An unverifiable tag is not a verified one. The run must not fall through
    to the generic boundary and report already_exists or unexpected_error."""
    result = legal_init({**_CONFLICT, "legaltags_by_name": _http_error(404, b"not found")})
    assert result.exit_code == 1
    assert "legal-init outcome: legal_tag_conflict" in result.stdout


def test_legal_init_fails_on_rejected_tag(legal_init):
    result = legal_init({"legaltags_post": _http_error(400, b"invalid properties")})
    assert result.exit_code == 1
    assert "legal-init outcome: legal_tag_rejected" in result.stdout


def test_legal_init_fails_when_token_acquisition_fails(legal_init):
    """An unusable token is not a staging failure. The tag POST needs one too,
    so the run ends on auth_failed rather than continuing to a POST that cannot
    succeed and reporting the catalog as the problem."""

    def get_token(resource: str = "https://management.azure.com/") -> str:
        raise SystemExit("Token acquisition failed: 401 unauthorized_client")

    result = legal_init(token=get_token)
    assert result.exit_code == 1
    assert "legal-init outcome: auth_failed" in result.stdout
    assert result.routed("legaltags_post") == []


def test_legal_init_creates_the_tag_even_when_keyvault_denies(legal_init):
    """Catalog staging must not be able to cost the tag. The tag is what the
    Job exists to produce; the catalog only widens which countries validate,
    and the run still exits non-zero so the gap is visible."""
    result = legal_init({"keyvault": _http_error(403, b"Forbidden")})
    assert result.exit_code == 1
    assert "legal-init outcome: catalog_not_staged" in result.stdout
    assert result.routed("blob_upload") == []
    assert len(result.routed("legaltags_post")) == 1


def test_legal_init_creates_the_tag_even_when_blob_upload_denies(legal_init):
    result = legal_init({"blob_upload": _http_error(403, b"AuthorizationFailure")})
    assert result.exit_code == 1
    assert "legal-init outcome: catalog_not_staged" in result.stdout
    assert len(result.routed("legaltags_post")) == 1


def test_legal_init_creates_the_tag_when_keyvault_body_is_malformed(legal_init):
    """The invariant is staging-local, not exception-type-specific: a 2xx from
    Key Vault carrying an unusable body must cost the catalog, not the tag."""
    result = legal_init({"keyvault": _responds(200, b"<html>not json</html>")})
    assert result.exit_code == 1
    assert "legal-init outcome: catalog_not_staged" in result.stdout
    assert result.routed("blob_upload") == []
    assert len(result.routed("legaltags_post")) == 1


def test_legal_init_catalog_carries_the_country_the_suite_tests(legal_init):
    """The legal acceptance suite creates MY tags expecting 201, and its own
    uploader skips a blob that already exists, so our staged catalog is what
    those tests read. Malaysia has to be present at Client consent required:
    legal's default entry for MY is "Default", which is rejected."""
    put = legal_init().routed("blob_upload")[0]
    catalog = {entry["alpha2"]: entry for entry in json.loads(put.body)}
    assert catalog["MY"]["residencyRisk"] == "Client consent required"


def test_legal_init_fails_when_legal_never_authorizes(legal_init):
    """If entitlements never grants the bootstrap identity access, the authz
    gate must time out with its own code rather than POST an unauthorized tag."""
    result = legal_init({"legaltags_get": _http_error(403, b"not authorized")})
    assert result.exit_code == 1
    assert "legal-init outcome: legal_authz_timeout" in result.stdout
    assert result.routed("legaltags_post") == []

    gate = result.routed("legaltags_get")[0]
    assert gate.headers["authorization"] == "Bearer fake-token"
    assert gate.headers["data-partition-id"] == "opendes"


def test_legal_init_fails_on_transport_error(legal_init):
    result = legal_init({"legaltags_post": _transport_error()})
    assert result.exit_code == 1
    assert "legal-init outcome: unexpected_error" in result.stdout


def test_legal_init_fails_on_missing_config(legal_init):
    result = legal_init(env={"LEGAL_TAG": None})
    assert result.exit_code == 1
    assert "legal-init outcome: config_missing" in result.stdout
    assert result.calls == []


def test_legal_init_times_out_waiting_for_legal(legal_init):
    result = legal_init({"legal_info": _responds(503)})
    assert result.exit_code == 1
    assert "legal-init outcome: service_timeout" in result.stdout
    assert result.routed("keyvault") == []


def test_legal_init_times_out_waiting_for_partition_record(legal_init):
    result = legal_init({"partition_record": _responds(404)})
    assert result.exit_code == 1
    assert "legal-init outcome: partition_record_timeout" in result.stdout
    assert result.routed("keyvault") == []
