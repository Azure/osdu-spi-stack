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

"""spi users against an in-memory entitlements behind urlopen."""

import io
import json
import urllib.error
import urllib.parse
from email.message import Message
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from spi import cli, identity, token, users

runner = CliRunner()

BASE = "https://spi.example/api/entitlements/v2"
DOMAIN = "dataservices.energy"
DEPLOY, MEMBER, WORKLOAD = "deploy-client-id", "member-client-id", "workload-client-id"
ALICE, BOB = "alice@contoso.com", "bob@contoso.com"
ENV = users.Environment(
    base=BASE,
    partitions=("opendes",),
    domain=DOMAIN,
    seeded={DEPLOY: "deploy identity", MEMBER: "member identity", WORKLOAD: "workload identity"},
)


class _Response(io.BytesIO):
    status = 200


class Entitlements:
    """The member, group, and caller rules of the entitlements API this command relies on."""

    def __init__(self, groups=None, known=()):
        self.groups = {
            group: set()
            for group in (
                "users",
                "users.data.root",
                "users.datalake.viewers",
                "users.datalake.editors",
                "users.datalake.admins",
                "users.datalake.ops",
            )
        }
        for group, members in (groups or {}).items():
            self.groups[group] = set(members)
        self.known = set(known)
        self.calls = []
        self.partitions = []
        self.failing = set()
        self.caller_answers = []

    def _refuse(self, url, status, body):
        raw = body if isinstance(body, str) else json.dumps(body)
        return urllib.error.HTTPError(url, status, "", Message(), io.BytesIO(raw.encode()))

    def urlopen(self, request, timeout):
        url = request.full_url
        parts = urllib.parse.urlsplit(url).path.removeprefix("/api/entitlements/v2/").split("/")
        parts = [urllib.parse.unquote(part) for part in parts]
        self.calls.append((request.method, url))
        self.partitions.append(request.get_header("Data-partition-id"))
        if parts == ["groups"]:
            status, body = self.caller_answers.pop(0) if self.caller_answers else (200, {})
            if status != 200:
                raise self._refuse(url, status, body)
            return _Response(json.dumps(body).encode())
        if parts[0] == "members":
            for members in self.groups.values():
                members.discard(parts[1])
            return _Response(b"")
        group = parts[1].split("@")[0]
        if group not in self.groups:
            raise self._refuse(url, 404, {"message": "Not Found"})
        members = self.groups[group]
        if (request.method, group) in self.failing:
            raise self._refuse(url, 500, {"message": "store unavailable"})
        if request.method == "GET":
            listing = [{"email": member, "memberType": "USER"} for member in sorted(members)]
            listing.append({"email": f"nested@opendes.{DOMAIN}", "memberType": "GROUP"})
            return _Response(json.dumps({"members": listing}).encode())
        if request.method == "POST":
            member = json.loads(request.data)["email"]
            if member in members:
                raise self._refuse(url, 409, {"message": "already a member"})
            members.add(member)
            return _Response(b"{}")
        members.discard(parts[3])
        return _Response(b"")

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


@pytest.fixture
def clock():
    """Time that passes only while verify sleeps."""
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    with (
        patch("spi.users.time.monotonic", side_effect=lambda: now[0]),
        patch("spi.users.time.sleep", side_effect=sleep) as slept,
    ):
        yield slept


@pytest.fixture
def served():
    def serve(**kwargs):
        server = Entitlements(**kwargs)
        patcher = patch("spi.users.urllib.request.urlopen", side_effect=server.urlopen)
        patcher.start()
        patchers.append(patcher)
        return server

    patchers = []
    yield serve
    for patcher in patchers:
        patcher.stop()


@pytest.mark.parametrize(
    "groups, role",
    [
        (["users"], "none"),
        (["users", "users.datalake.viewers"], "viewer"),
        (["users.datalake.admins", "service.legal.user"], "admin"),
        (["users.data.root", "users.datalake.ops"], "ops"),
        (["users.datalake.ops"], "custom"),
        (["users.datalake.viewers", "users.datalake.admins"], "custom"),
    ],
)
def test_role_is_the_preset_whose_role_groups_are_held(groups, role):
    assert users.role_of(groups) == role


def test_add_writes_the_role_preset_for_a_new_member(served):
    server = served()

    result = users.add_user(ENV, "deploy", ALICE, "admin", ENV.partitions)

    assert result["previousRole"] is None
    assert result["partitions"] == {"opendes": {"users": "added", "users.datalake.admins": "added"}}
    assert ALICE in server.groups["users"] and ALICE in server.groups["users.datalake.admins"]


def test_add_with_another_role_replaces_the_role_and_keeps_users(served):
    server = served(groups={"users": {ALICE}, "users.datalake.admins": {ALICE}})

    result = users.add_user(ENV, "deploy", ALICE, "viewer", ENV.partitions)

    assert result["previousRole"] == "admin"
    assert result["partitions"]["opendes"] == {
        "users": "already",
        "users.datalake.viewers": "added",
        "users.datalake.admins": "removed",
    }
    assert ALICE in server.groups["users"]
    assert ALICE not in server.groups["users.datalake.admins"]


def test_add_with_the_role_already_held_changes_nothing(served):
    server = served(groups={"users": {ALICE}, "users.datalake.admins": {ALICE}})

    result = users.add_user(ENV, "deploy", ALICE, "admin", ENV.partitions)

    assert result["previousRole"] == "admin"
    assert result["partitions"]["opendes"] == {
        "users": "already",
        "users.datalake.admins": "already",
    }
    assert ALICE in server.groups["users.datalake.admins"]


def test_a_failed_role_change_does_not_leave_the_old_role_standing(served):
    server = served(groups={"users": {ALICE}, "users.datalake.admins": {ALICE}})
    server.failing = {("POST", "users.datalake.viewers")}

    with pytest.raises(users.UsersError, match="adding to users.datalake.viewers was refused"):
        users.add_user(ENV, "deploy", ALICE, "viewer", ENV.partitions)

    assert ALICE not in server.groups["users.datalake.admins"]


def test_a_failed_add_takes_back_the_groups_it_wrote(served):
    server = served()
    server.failing = {("POST", "users.datalake.ops")}

    with pytest.raises(users.UsersError, match="groups this run added were removed"):
        users.add_user(ENV, "deploy", BOB, "ops", ENV.partitions)

    assert not [group for group, members in server.groups.items() if BOB in members]


def test_a_failed_add_names_a_group_it_could_not_take_back(served):
    server = served()
    server.failing = {("POST", "users.datalake.ops"), ("DELETE", "users.data.root")}

    with pytest.raises(users.UsersError, match="Could not undo users.data.root"):
        users.add_user(ENV, "deploy", BOB, "ops", ENV.partitions)

    assert BOB not in server.groups["users"]


def test_a_failed_add_keeps_groups_the_member_already_held(served):
    server = served(groups={"users": {BOB}})
    server.failing = {("POST", "users.datalake.admins")}

    with pytest.raises(users.UsersError):
        users.add_user(ENV, "deploy", BOB, "admin", ENV.partitions)

    assert BOB in server.groups["users"]


def test_add_refuses_a_seeded_identity_before_any_call(served):
    server = served(groups={"users": {DEPLOY}, "users.datalake.ops": {DEPLOY}})

    with pytest.raises(users.UsersError, match="deploy identity") as raised:
        users.add_user(ENV, "deploy", DEPLOY.upper(), "viewer", ENV.partitions)

    assert raised.value.code == "seeded_identity"
    assert server.calls == []


@pytest.mark.parametrize("call", [users.add_user, users.remove_user])
def test_a_group_address_is_refused_before_any_call(served, call):
    server = served(groups={"users": {ALICE}})
    group = f"Users@opendes.{DOMAIN}"
    args = (ENV, "deploy", group, "ops", ENV.partitions)

    with pytest.raises(users.UsersError, match="not a person") as raised:
        call(*args) if call is users.add_user else call(*args[:3], args[4])

    assert raised.value.code == "group_address"
    assert server.calls == []


def test_add_matches_a_stored_id_whatever_its_case(served):
    server = served(groups={"users": {ALICE}, "users.datalake.admins": {ALICE}})

    result = users.add_user(ENV, "deploy", "Alice@Contoso.com", "viewer", ENV.partitions)

    assert result["previousRole"] == "admin"
    assert any(
        "users.datalake.admins" in url for method, url in server.writes() if method == "DELETE"
    )


def test_member_ids_and_group_addresses_are_percent_encoded(served):
    server = served(groups={"users.datalake.admins": {"a+b@c.com"}})

    users.add_user(ENV, "deploy", "a+b@c.com", "viewer", ENV.partitions)

    removal = next(url for method, url in server.writes() if method == "DELETE")
    assert removal.endswith(
        "/groups/users.datalake.admins%40opendes.dataservices.energy/members/a%2Bb%40c.com"
    )


@pytest.mark.parametrize(
    "role, missing", [("admin", "users.datalake.admins"), ("ops", "users.datalake.ops")]
)
def test_add_writes_nothing_when_the_partition_lacks_a_group_of_the_role(served, role, missing):
    server = served()
    del server.groups[missing]

    with pytest.raises(users.UsersError, match=f"{missing} does not exist") as raised:
        users.add_user(ENV, "deploy", ALICE, role, ENV.partitions)

    assert raised.value.code == "group_missing"
    assert server.writes() == []


def test_verify_waits_for_entitlements_to_admit_the_caller(served, clock):
    server = served()
    server.caller_answers = [(401, {"message": "not authorized"}), (200, {"groups": [{}, {}]})]

    result = users.verify(ENV, "person", "opendes")

    assert result == {"ok": True, "status": 200, "groups": 2}
    clock.assert_called_once_with(users.VERIFY_INTERVAL)


def test_verify_does_not_wait_when_the_mesh_refuses_the_token(served, clock):
    server = served()
    server.caller_answers = [(401, "Jwt verification fails")]

    result = users.verify(ENV, "person", "opendes")

    assert (result["ok"], result["layer"], result["status"]) == (False, "mesh", 401)
    clock.assert_not_called()


def test_verify_gives_up_on_entitlements_after_the_timeout(served, clock):
    server = served()
    server.caller_answers = [(401, {"message": "not authorized"})] * 20

    result = users.verify(ENV, "person", "opendes", timeout=10, interval=5)

    assert (result["ok"], result["layer"]) == (False, "entitlements")
    assert len(server.calls) == 3


def test_list_gives_each_member_a_kind_and_role_and_skips_nested_groups(served):
    served(
        groups={
            "users": {DEPLOY, MEMBER, ALICE, BOB},
            "users.datalake.ops": {DEPLOY},
            "users.datalake.viewers": {ALICE},
        }
    )

    rows = {row["member"]: row for row in users.list_users(ENV, "deploy")["opendes"]}

    assert set(rows) == {DEPLOY, MEMBER, ALICE, BOB}
    assert (rows[DEPLOY]["kind"], rows[DEPLOY]["role"]) == ("deploy identity", "seeded")
    assert (rows[ALICE]["kind"], rows[ALICE]["role"]) == ("user", "viewer")
    assert rows[ALICE]["groups"] == ["users", "users.datalake.viewers"]
    assert rows[BOB]["role"] == "none"


def test_remove_refuses_a_seeded_identity_before_any_call(served):
    server = served(groups={"users": {DEPLOY}})

    with pytest.raises(users.UsersError, match="deploy identity") as raised:
        users.remove_user(ENV, "deploy", DEPLOY.upper(), ENV.partitions)

    assert raised.value.code == "seeded_identity"
    assert server.calls == []


def test_remove_deletes_a_member_and_reports_a_stranger_as_absent(served):
    server = served(groups={"users": {ALICE}, "users.datalake.admins": {ALICE}})

    assert users.remove_user(ENV, "deploy", ALICE, ENV.partitions) == {"opendes": "removed"}
    assert users.remove_user(ENV, "deploy", BOB, ENV.partitions) == {"opendes": "absent"}

    assert [url for method, url in server.writes()] == [f"{BASE}/members/alice%40contoso.com"]
    assert ALICE not in server.groups["users"]


def test_an_unknown_partition_is_refused():
    with pytest.raises(users.UsersError, match="No partition 'other'"):
        users.select_partitions(ENV, "other")


@pytest.fixture
def invoke(monkeypatch, served, clock):
    """Run a users command against a served entitlements, signed in as alice."""

    def run(args, *, person=ALICE, env=ENV, **kwargs):
        server = served(**kwargs)
        monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
        signed_in = identity.PersonToken("person-bearer", "1", "aud", person, "unique_name")
        minted = token.MintedToken("deploy-bearer", "1", DEPLOY, "spi-deployer", "aud")
        with (
            patch("spi.users.load_environment", return_value=env),
            patch("spi.token.mint_token", return_value=minted),
            patch("spi.identity.person_token", return_value=signed_in),
        ):
            return runner.invoke(cli.app, ["users", *args]), server

    return run


@pytest.mark.parametrize("command", ["add", "remove"])
@pytest.mark.parametrize("target", [[], [BOB, "--me"]])
def test_a_command_needs_exactly_one_of_me_and_an_id(invoke, command, target):
    result, server = invoke([command, *target])

    assert result.exit_code == 1
    assert "either --me or one member id" in result.output
    assert server.calls == []


def test_add_me_defaults_to_admin_and_verifies(invoke):
    result, server = invoke(["add", "--me"])

    assert result.exit_code == 0
    assert ALICE in server.groups["users.datalake.admins"]
    assert "verified" in result.output and "not verified" not in result.output


def test_add_me_verifies_in_the_partition_it_wrote(invoke):
    two = users.Environment(BASE, ("opendes", "second"), DOMAIN, ENV.seeded)

    result, server = invoke(["add", "--me", "--partition", "second"], env=two)

    assert result.exit_code == 0
    assert set(server.partitions) == {"second"}


def test_add_for_someone_else_is_reported_as_not_verified(invoke):
    result, server = invoke(["add", BOB, "-r", "ops", "--json"])

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["apiVersion"] == "spi.osdu.dev/v1"
    assert (document["member"], document["claim"], document["verified"]) == (BOB, None, None)
    assert document["partitions"]["opendes"] == {
        "users": "added",
        "users.data.root": "added",
        "users.datalake.ops": "added",
    }
    assert not [url for method, url in server.calls if url.endswith("/groups")]


def test_add_shows_the_role_change(invoke):
    result, _ = invoke(
        ["add", "--me", "--role", "viewer"],
        groups={"users": {ALICE}, "users.datalake.admins": {ALICE}},
    )

    assert "admin -> viewer" in result.output
    assert "removed  users.datalake.admins" in result.output


def test_add_me_fails_when_entitlements_never_admits_the_token(invoke):
    refusals = [(401, {"message": "not authorized"})] * 20
    with patch.object(Entitlements, "__init__", _with_answers(refusals)):
        result, server = invoke(["add", "--me", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["verified"]["ok"] is False
    assert "different id" in result.stderr
    assert ALICE in server.groups["users"]


def _with_answers(answers):
    original = Entitlements.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.caller_answers = list(answers)

    return init


def test_list_marks_a_signed_in_person_who_is_not_a_member(invoke):
    result, _ = invoke(["list", "--json"], groups={"users": {DEPLOY, BOB}})

    document = json.loads(result.stdout)
    assert document["you"] == {"member": ALICE, "claim": "unique_name", "isMember": False}
    assert [row["you"] for row in document["partitions"]["opendes"]] == [False, False]

    result, _ = invoke(["list"], groups={"users": {DEPLOY, BOB}})
    assert "cannot call OSDU APIs" in result.output
    assert "spi users add --me" in result.output


def test_list_marks_the_signed_in_member_row(invoke):
    result, _ = invoke(["list", "--json"], groups={"users": {ALICE}})

    document = json.loads(result.stdout)
    assert document["you"]["isMember"] is True
    assert document["partitions"]["opendes"][0]["you"] is True


def test_remove_of_a_seeded_identity_exits_2_with_stdout_clean(invoke):
    result, server = invoke(["remove", DEPLOY, "--json"], groups={"users": {DEPLOY}})

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Refusing to remove the deploy identity" in result.stderr
    assert server.calls == []


def test_add_of_a_seeded_identity_exits_2(invoke):
    result, server = invoke(["add", DEPLOY, "-r", "viewer"], groups={"users": {DEPLOY}})

    assert result.exit_code == 2
    assert "Refusing to change the deploy identity" in result.output
    assert server.calls == []


def test_remove_me_removes_the_signed_in_person(invoke):
    result, server = invoke(
        ["remove", "--me"], groups={"users": {ALICE}, "users.datalake.admins": {ALICE}}
    )

    assert result.exit_code == 0
    assert "removed" in result.output
    assert ALICE not in server.groups["users"]
