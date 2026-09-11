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
import tempfile
import time
import types
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, cast

import pytest
import yaml
from _quantities import _millicores

from spi.shell import run_process
from spi.templates import ENTITLEMENTS_MEMBERS_GENERATION, entitlements_members_job_name

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


_MEMBERS = {"entitlementsMembers[0]": "deployer-client-id"}


def test_component_split_matches_the_three_releases():
    """The gating osdu-spi-init release must never render a legal or members
    Job, and the non-gating osdu-spi-legal and osdu-spi-members releases must
    render nothing but their own Jobs: helm-controller waits for every Job in
    a release, so this split is what keeps a seeding failure from blocking
    schema-load."""
    gating = _render(["opendes"], {"legalEnabled": "false", "membersEnabled": "false", **_MEMBERS})
    gating_names = sorted(doc["metadata"]["name"] for doc in gating)
    assert gating_names == [
        "entitlements-init-opendes",
        "osdu-spi-init-partition-records",
        "osdu-spi-init-scripts",
        "partition-init-opendes",
    ]

    legal = _render(["opendes"], {"coreEnabled": "false", "membersEnabled": "false", **_MEMBERS})
    assert [doc["metadata"]["name"] for doc in legal] == ["legal-init-opendes"]

    members = _render(["opendes"], {"coreEnabled": "false", "legalEnabled": "false", **_MEMBERS})
    assert [doc["metadata"]["name"] for doc in members] == [
        entitlements_members_job_name("opendes", ["deployer-client-id"])
    ]


def test_members_job_renders_one_per_partition_under_the_cli_computed_name():
    """The CLI reads the Job back by a name it computes from the same member
    list, so the chart's hash expression and templates.py must agree."""
    members = ["b-client-id", "a-client-id"]
    docs = _render(
        ["opendes", "second"],
        {
            "coreEnabled": "false",
            "legalEnabled": "false",
            "entitlementsMembers[0]": members[0],
            "entitlementsMembers[1]": members[1],
        },
    )
    jobs = _jobs(docs, "entitlements-members")

    assert [job["metadata"]["name"] for job in jobs] == [
        entitlements_members_job_name("opendes", members),
        entitlements_members_job_name("second", members),
    ]
    for job in jobs:
        env = {
            e["name"]: e.get("value")
            for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert env["MEMBERS"] == "a-client-id,b-client-id"
        assert job["metadata"]["labels"]["osdu.spi/partition"] == env["PARTITION"]
        assert [v["name"] for v in job["spec"]["template"]["spec"]["volumes"]] == ["scripts"]


def test_members_generation_agrees_between_chart_and_cli():
    """The chart's default generation and the CLI constant feed the same
    hash; a bump on one side alone would make spi info miss every seeded Job."""
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
    assert values["membersGeneration"] == ENTITLEMENTS_MEMBERS_GENERATION


def test_members_job_never_renders_without_members():
    """A values ConfigMap written before the CLI carried a deploy identity
    must render nothing, not a Job that seeds nobody."""
    docs = _render(["opendes"])
    assert _jobs(docs, "entitlements-members") == []


def test_no_access_identity_never_reaches_the_chart():
    """The no-access identity exists so 403 tests have a caller entitlements
    has never met; the only value the chart takes is the member list."""
    rendered = json.dumps(_render(["opendes"], _MEMBERS))
    assert "noaccess" not in rendered.lower()
    assert "no_access" not in rendered.lower()


def _script_constants(source: str) -> dict:
    """Module-level literal constants, read without importing: init_legal.py
    pulls auth and wait off /scripts, which only exists inside the Job."""
    constants: dict = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        try:
            value = ast.literal_eval(node.value)
        except ValueError:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value
    return constants


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


def test_jobs_request_the_cpu_admission_will_grant_them():
    """Deployment Safeguards raises a Job's CPU request to 100m at admission,
    and a Job's pod template is immutable. A lower request leaves the live Job
    differing from the release manifest in a field Helm can only close by
    patching, which the API server rejects, so every later upgrade of the
    release fails and so does its rollback."""
    docs = _render(["opendes"], {"entitlementsMembers[0]": "deployer-client-id"})
    jobs = [doc for doc in docs if doc.get("kind") == "Job"]
    assert {job["metadata"]["labels"]["app.kubernetes.io/component"] for job in jobs} == {
        "partition-init",
        "entitlements-init",
        "legal-init",
        "entitlements-members",
    }
    for job in jobs:
        pod = job["spec"]["template"]["spec"]
        for container in pod["containers"] + pod.get("initContainers", []):
            requested = container["resources"]["requests"]["cpu"]
            assert _millicores(requested) >= 100, job["metadata"]["name"]


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


# auth.get_token's urlopen call in scripts.yaml uses timeout=60.
_TOKEN_TIMEOUT = 60


def test_members_deadline_covers_its_wait_budget(init_scripts):
    """The members Job renders with its own deadline; the script's wait plus
    REQUEST_BUDGET must fit inside it, and REQUEST_BUDGET itself must cover
    the token exchange and the calls the script actually makes, or the pod
    dies before printing a typed outcome."""
    const = _script_constants(init_scripts["init_members.py"])
    budget = (
        const["INFO_ATTEMPTS"] * (const["WAIT_DELAY"] + const["WAIT_SOCKET_TIMEOUT"])
        + const["REQUEST_BUDGET"]
    )
    calls = 1 + len(const["CREATED_GROUPS"]) + 2 * len(const["ROOT_GROUPS"])
    assert const["REQUEST_BUDGET"] >= _TOKEN_TIMEOUT + calls * const["REQUEST_TIMEOUT"]

    docs = _render(["opendes"], _MEMBERS)
    job = _jobs(docs, "entitlements-members")[0]
    assert job["spec"]["activeDeadlineSeconds"] >= budget


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
    assert "init_members.py" in init_scripts
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
    termination_message: str = ""

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


def test_entitlements_init_retries_until_partition_is_visible(init_scripts, monkeypatch, capsys):
    monkeypatch.setenv("PARTITION", "opendes")
    auth = types.ModuleType("auth")
    setattr(auth, "get_token", lambda: "test-token")
    wait = types.ModuleType("wait")
    setattr(wait, "wait_for_status", lambda *args, **kwargs: True)
    monkeypatch.setitem(sys.modules, "auth", auth)
    monkeypatch.setitem(sys.modules, "wait", wait)

    namespace: dict[str, object] = {"__name__": "init_entitlements_test"}
    exec(init_scripts["init_entitlements.py"], namespace)
    time_module = namespace["time"]
    assert isinstance(time_module, types.ModuleType)
    monkeypatch.setattr(time_module, "sleep", lambda _: None)

    responses = iter([403, 403, 200])
    requests = []

    def urlopen(req, timeout):
        requests.append(req)
        code = next(responses)
        if code == 403:
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "error",
                email.message.Message(),
                io.BytesIO(b'{"message":"Invalid data partition id"}'),
            )
        return _FakeResponse(code, b"")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    main = cast(Callable[[], int], namespace["main"])
    assert main() == 0
    assert len(requests) == 3
    assert all(req.headers["Data-partition-id"] == "opendes" for req in requests)
    assert capsys.readouterr().out.count("Partition is not visible to entitlements yet") == 2


def test_entitlements_init_fails_fast_on_unrelated_forbidden(init_scripts, monkeypatch, capsys):
    monkeypatch.setenv("PARTITION", "opendes")
    auth = types.ModuleType("auth")
    setattr(auth, "get_token", lambda: "test-token")
    wait = types.ModuleType("wait")
    setattr(wait, "wait_for_status", lambda *args, **kwargs: True)
    monkeypatch.setitem(sys.modules, "auth", auth)
    monkeypatch.setitem(sys.modules, "wait", wait)

    namespace: dict[str, object] = {"__name__": "init_entitlements_test"}
    exec(init_scripts["init_entitlements.py"], namespace)
    time_module = namespace["time"]
    assert isinstance(time_module, types.ModuleType)
    sleeps: list[float] = []
    monkeypatch.setattr(time_module, "sleep", lambda seconds: sleeps.append(seconds))

    requests = []

    def urlopen(req, timeout):
        requests.append(req)
        raise urllib.error.HTTPError(
            req.full_url,
            403,
            "error",
            email.message.Message(),
            io.BytesIO(b'{"message":"Access denied"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    main = cast(Callable[[], int], namespace["main"])
    assert main() == 1
    assert len(requests) == 1
    assert "Partition is not visible to entitlements yet" not in capsys.readouterr().out
    assert sleeps == []


# --- init_members.py execution harness ---------------------------------------

_GROUPS = {
    "users": "users@opendes.dataservices.energy",
    "users.datalake.ops": "users.datalake.ops@opendes.dataservices.energy",
    "users.datalake.admins": "users.datalake.admins@opendes.dataservices.energy",
    "users.data.root": "users.data.root@opendes.dataservices.energy",
    "users.datalake.delegation": "users.datalake.delegation@opendes.dataservices.energy",
}
# Created by the Job when the listing lacks them; only delegation takes members.
_CREATED_GROUPS = {
    "users.datalake.delegation": _GROUPS["users.datalake.delegation"],
    "users.datalake.impersonation": "users.datalake.impersonation@opendes.dataservices.energy",
}
_BOOTSTRAP_GROUPS = tuple(n for n in _GROUPS if n not in _CREATED_GROUPS)


_ALL_GROUPS = {**_GROUPS, **_CREATED_GROUPS}


def _groups_listing(names=tuple(_ALL_GROUPS)) -> bytes:
    return json.dumps(
        {"groups": [{"name": n, "email": _ALL_GROUPS[n], "description": ""} for n in names]}
    ).encode()


def _run_members(init_scripts, monkeypatch, capsys, *, members="deployer-client-id", **routes):
    """Execute init_members.py against a routed fake of urlopen.

    Routes: ``groups`` (GET /groups), ``create`` (POST /groups), ``post``
    (POST members), ``get`` (GET members); each a handler(url) returning a
    _FakeResponse or raising.
    """
    monkeypatch.setenv("PARTITION", "opendes")
    monkeypatch.setenv("MEMBERS", members)
    termination_log = Path(tempfile.mkdtemp()) / "termination-log"
    monkeypatch.setenv("TERMINATION_MESSAGE_PATH", str(termination_log))
    auth = types.ModuleType("auth")
    setattr(auth, "get_token", lambda: "test-token")
    wait = types.ModuleType("wait")
    setattr(wait, "wait_for_status", lambda *args, **kwargs: routes.get("info", True))
    monkeypatch.setitem(sys.modules, "auth", auth)
    monkeypatch.setitem(sys.modules, "wait", wait)

    namespace: dict[str, object] = {"__name__": "init_members_test"}
    exec(init_scripts["init_members.py"], namespace)
    calls: list[_Call] = []
    member_lists = {email: set() for email in _GROUPS.values()}

    def urlopen(req, timeout):
        url, method = req.full_url, req.get_method()
        if url.endswith("/groups") and method == "GET":
            route = "groups"
            default = _responds(200, _groups_listing())
        elif url.endswith("/groups"):
            route = "create"
            name = json.loads(req.data)["name"]
            created = {"name": name, "email": _CREATED_GROUPS[name], "description": ""}

            def default(url, created=created):
                return _FakeResponse(201, json.dumps(created).encode())
        elif method == "POST":
            route = "post"
            email = url.rsplit("/groups/", 1)[1].removesuffix("/members")
            member_lists[email].add(json.loads(req.data)["email"].upper())

            def default(url):
                return _FakeResponse(200, b"{}")
        else:
            route = "get"
            email = url.rsplit("/groups/", 1)[1].removesuffix("/members")
            listing = {"members": [{"email": m, "role": "MEMBER"} for m in member_lists[email]]}

            def default(url, listing=listing):
                return _FakeResponse(200, json.dumps(listing).encode())

        calls.append(_Call(route, url, method, dict(req.headers), req.data))
        return routes.get(route, default)(url)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    main = cast(Callable[[], int], namespace["main"])
    rc = main()
    message = termination_log.read_text() if termination_log.exists() else ""
    return _Result(rc, capsys.readouterr().out, calls, message)


def test_members_seeds_every_root_group_and_verifies(init_scripts, monkeypatch, capsys):
    result = _run_members(init_scripts, monkeypatch, capsys, members="b-id,a-id")

    assert result.exit_code == 0
    assert "entitlements-members outcome: seeded" in result.stdout
    assert result.routed("create") == []
    posts = result.routed("post")
    assert len(posts) == 10
    assert {json.loads(p.body)["email"] for p in posts} == {"a-id", "b-id"}
    assert {json.loads(p.body)["role"] for p in posts} == {"MEMBER"}
    assert {p.url.rsplit("/groups/", 1)[1].removesuffix("/members") for p in posts} == set(
        _GROUPS.values()
    )
    assert all(p.headers["Data-partition-id"] == "opendes" for p in posts)
    assert len(result.routed("get")) == 5


def test_members_creates_the_impersonation_groups_the_bootstrap_lacks(
    init_scripts, monkeypatch, capsys
):
    result = _run_members(
        init_scripts,
        monkeypatch,
        capsys,
        groups=_responds(200, _groups_listing(_BOOTSTRAP_GROUPS)),
    )

    assert result.exit_code == 0
    assert "entitlements-members outcome: seeded" in result.stdout
    creates = result.routed("create")
    assert [json.loads(c.body)["name"] for c in creates] == [
        "users.datalake.delegation",
        "users.datalake.impersonation",
    ]
    assert all(json.loads(c.body)["description"] for c in creates)
    posted_to = {
        p.url.rsplit("/groups/", 1)[1].removesuffix("/members") for p in result.routed("post")
    }
    assert _CREATED_GROUPS["users.datalake.delegation"] in posted_to
    assert _CREATED_GROUPS["users.datalake.impersonation"] not in posted_to


def test_members_derives_the_created_group_email_when_the_service_omits_it(
    init_scripts, monkeypatch, capsys
):
    result = _run_members(
        init_scripts,
        monkeypatch,
        capsys,
        groups=_responds(200, _groups_listing(_BOOTSTRAP_GROUPS)),
        create=_http_error(409, b"exists"),
    )

    assert result.exit_code == 0
    posted_to = {
        p.url.rsplit("/groups/", 1)[1].removesuffix("/members") for p in result.routed("post")
    }
    assert _CREATED_GROUPS["users.datalake.delegation"] in posted_to


def test_members_fails_when_the_service_rejects_a_group(init_scripts, monkeypatch, capsys):
    result = _run_members(
        init_scripts,
        monkeypatch,
        capsys,
        groups=_responds(200, _groups_listing(_BOOTSTRAP_GROUPS)),
        create=_http_error(403, b"forbidden"),
    )

    assert result.exit_code == 1
    assert "entitlements-members outcome: group_rejected" in result.stdout
    assert "users.datalake.delegation was not created (403)" in result.stdout
    assert result.routed("post") == []


def test_members_treats_conflict_as_already_a_member(init_scripts, monkeypatch, capsys):
    listing = json.dumps({"members": [{"email": "deployer-client-id", "role": "MEMBER"}]}).encode()
    result = _run_members(
        init_scripts,
        monkeypatch,
        capsys,
        post=_http_error(409, b"exists"),
        get=_responds(200, listing),
    )

    assert result.exit_code == 0
    assert "entitlements-members outcome: already_member" in result.stdout


def test_members_fails_when_a_root_group_is_missing(init_scripts, monkeypatch, capsys):
    result = _run_members(
        init_scripts,
        monkeypatch,
        capsys,
        groups=_responds(200, _groups_listing(("users", "users.datalake.ops"))),
    )

    assert result.exit_code == 1
    assert "entitlements-members outcome: group_missing" in result.stdout
    assert "users.datalake.admins, users.data.root" in result.stdout
    assert result.routed("post") == []
    assert result.termination_message == (
        "entitlements-members outcome: group_missing: "
        "root groups not visible to the owner: users.datalake.admins, users.data.root; "
        "deployer-client-id not seeded"
    )


def test_members_fails_when_the_service_rejects_a_member(init_scripts, monkeypatch, capsys):
    result = _run_members(init_scripts, monkeypatch, capsys, post=_http_error(403, b"forbidden"))

    assert result.exit_code == 1
    assert "entitlements-members outcome: member_rejected" in result.stdout
    assert "deployer-client-id was not added to users@opendes.dataservices.energy" in result.stdout
    assert len(result.routed("post")) == 1


def test_members_fails_when_verification_does_not_list_the_member(
    init_scripts, monkeypatch, capsys
):
    result = _run_members(init_scripts, monkeypatch, capsys, get=_responds(200, b'{"members": []}'))

    assert result.exit_code == 1
    assert "entitlements-members outcome: verify_failed" in result.stdout
    assert "is not a member of users@opendes.dataservices.energy" in result.stdout


def test_members_times_out_waiting_for_entitlements(init_scripts, monkeypatch, capsys):
    result = _run_members(init_scripts, monkeypatch, capsys, info=False)

    assert result.exit_code == 1
    assert "entitlements-members outcome: service_timeout" in result.stdout
    assert result.calls == []


def test_members_fails_on_transport_error(init_scripts, monkeypatch, capsys):
    with pytest.raises(urllib.error.URLError):
        _run_members(init_scripts, monkeypatch, capsys, groups=_transport_error())


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
