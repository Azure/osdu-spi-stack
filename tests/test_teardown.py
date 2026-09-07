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

"""`spi down` keeps managed identities; `--purge` deletes the group."""

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from spi.config import Config
from spi.teardown import (
    HANDLED_TYPES,
    RETAINED_TYPES,
    ExternalGrant,
    TeardownError,
    purge_environment,
    teardown_environment,
)

INFRA = Path(__file__).parent.parent / "infra"
SUB = "/subscriptions/00000000-0000-0000-0000-000000000000"
RG = "spi-stack-dev1"
FQDN = "spi-stack-dev1-a1b2c3d4.hcp.eastus.azmk8s.io"


def _res(rtype: str, name: str) -> dict:
    return {
        "id": f"{SUB}/resourceGroups/{RG}/providers/{rtype}/{name}",
        "type": rtype,
        "name": name,
    }


def full_inventory() -> list:
    """What `az resource list` returns for a built environment."""
    return [
        _res("Microsoft.ContainerService/managedClusters", "spi-stack-dev1"),
        _res("Microsoft.ContainerRegistry/registries", "osdudev1abcde"),
        _res("Microsoft.DocumentDB/databaseAccounts", "osdu-dev1-opendes-cosmos-abcde"),
        _res("Microsoft.DocumentDB/databaseAccounts", "osdu-dev1-graph-abcde"),
        _res("Microsoft.EventGrid/systemTopics", "osdudev1commonabcde-guid"),
        _res("Microsoft.KeyVault/vaults", "osdudev1abcde"),
        _res("Microsoft.ManagedIdentity/userAssignedIdentities", "spi-stack-dev1-ctl-id"),
        _res("Microsoft.ManagedIdentity/userAssignedIdentities", "spi-stack-dev1-osdu-identity"),
        _res("Microsoft.ManagedIdentity/userAssignedIdentities", "spi-stack-dev1-deployer"),
        _res("Microsoft.Network/natGateways", "spi-stack-dev1-natgw"),
        _res("Microsoft.Network/publicIPAddresses", "spi-stack-dev1-natgw-pip"),
        _res("Microsoft.Network/virtualNetworks", "spi-stack-dev1-vnet"),
        _res("Microsoft.ServiceBus/namespaces", "osdu-dev1-opendes-bus-abcde"),
        _res("Microsoft.Storage/storageAccounts", "osdudev1commonabcde"),
    ]


class FakeAzure:
    """Answers the `az` calls teardown makes, mutating state as deletes land."""

    def __init__(self, inventory=None, groups=None):
        self.inventory = inventory if inventory is not None else full_inventory()
        self.groups = groups if groups is not None else {RG: True, f"{RG}-nodes": True}
        self.subnets = []
        self.identities = []
        self.grants = {}
        self.delete_failures = {}
        self.calls = []
        self.group_delete_polls_until_gone = 0
        self.prune = None
        self.deleting = set()
        self.clock = 0.0
        self.tick_seconds = 5.0

    def monotonic(self):
        self.clock += self.tick_seconds
        return self.clock

    def _ok(self, stdout=""):
        return subprocess.CompletedProcess(["az"], 0, stdout, "")

    def _fail(self, stderr):
        return subprocess.CompletedProcess(["az"], 1, "", stderr)

    def run_command(self, cmd, **_kwargs):
        self.calls.append(cmd)
        verb = tuple(cmd[1:4])
        if verb[:2] == ("group", "exists"):
            name = cmd[cmd.index("--name") + 1]
            if self.group_delete_polls_until_gone and name == RG:
                self.group_delete_polls_until_gone -= 1
                if self.group_delete_polls_until_gone == 0:
                    self.groups[RG] = False
            return self._ok("true" if self.groups.get(name) else "false")
        if verb[:2] == ("group", "delete"):
            name = cmd[cmd.index("--name") + 1]
            self.groups[name] = self.group_delete_polls_until_gone > 0 and name == RG
            return self._ok()
        if verb[:2] == ("resource", "delete"):
            assert "--no-wait" in cmd
            rid = cmd[cmd.index("--ids") + 1]
            pending = self.delete_failures.get(rid)
            if pending:
                stderr = pending.pop(0)
                return self._fail(stderr)
            self.deleting.add(rid)
            return self._ok()
        if verb[:2] == ("resource", "show"):
            rid = cmd[cmd.index("--ids") + 1]
            return self._ok("Deleting" if rid in self.deleting else "Succeeded")
        if verb[:2] == ("resource", "list"):
            # Accepted deletes land by the next inventory read.
            for rid in list(self.deleting):
                self.inventory = [r for r in self.inventory if r["id"] != rid]
                if rid.lower().endswith("/managedclusters/spi-stack-dev1"):
                    self.groups[f"{RG}-nodes"] = False
            self.deleting.clear()
            return self._ok(json.dumps(self.inventory))
        if verb[:2] == ("aks", "show"):
            return self._ok(f"{FQDN}\n")
        if verb == ("network", "vnet", "list"):
            return self._ok(json.dumps([{"name": "vnet", "subnets": self.subnets}]))
        if verb == ("network", "vnet", "subnet"):
            sid = cmd[cmd.index("--ids") + 1]
            for subnet in self.subnets:
                if subnet["id"] == sid:
                    subnet["natGateway"] = None
            return self._ok()
        if verb[:2] == ("identity", "list"):
            return self._ok(json.dumps(self.identities))
        if verb == ("role", "assignment", "list"):
            pid = cmd[cmd.index("--assignee") + 1]
            return self._ok(json.dumps(self.grants.get(pid, [])))
        if verb == ("role", "assignment", "delete"):
            gid = cmd[cmd.index("--ids") + 1]
            for pid, items in self.grants.items():
                self.grants[pid] = [g for g in items if g["id"] != gid]
            return self._ok()
        raise AssertionError(f"unexpected az call: {cmd}")

    def deletes(self):
        return [c[c.index("--ids") + 1] for c in self.calls if c[1:3] == ["resource", "delete"]]


@pytest.fixture
def az():
    fake = FakeAzure()
    with (
        patch("spi.teardown.run_command", side_effect=fake.run_command),
        patch("spi.teardown.time.sleep"),
        patch("spi.teardown.time.monotonic", side_effect=fake.monotonic),
        patch("spi.teardown.prune_kube_context") as prune,
        patch("spi.teardown.display_result"),
    ):
        fake.prune = prune
        yield fake


def config() -> Config:
    return Config.from_env("dev1", name_suffix="abcde")


class TestPlanCoversTheBicep:
    def test_every_top_level_type_the_bicep_provisions_is_planned(self):
        """Provisioning a new resource type adds a teardown obligation."""
        pattern = re.compile(r"^resource \w+ '(Microsoft\.[A-Za-z]+/[A-Za-z]+)@[^']+'(.*)$", re.M)
        provisioned = set()
        for path in INFRA.rglob("*.bicep"):
            for rtype, rest in pattern.findall(path.read_text()):
                if "existing" in rest:
                    continue
                provisioned.add(rtype.lower())
        # Extension resources live on their parent and go with it.
        extension_types = {
            "microsoft.authorization/roleassignments",
            "microsoft.kubernetesconfiguration/extensions",
            "microsoft.kubernetesconfiguration/fluxconfigurations",
        }
        missing = provisioned - extension_types - HANDLED_TYPES
        assert not missing, f"teardown plan does not cover {sorted(missing)}"

    def test_identities_are_the_only_retained_type(self):
        assert RETAINED_TYPES == {"microsoft.managedidentity/userassignedidentities"}


class TestOrdinaryTeardown:
    def test_everything_but_identities_is_deleted(self, az):
        retained = teardown_environment(config())

        assert {r.name for r in retained} == {
            "spi-stack-dev1-ctl-id",
            "spi-stack-dev1-osdu-identity",
            "spi-stack-dev1-deployer",
        }
        assert all("userAssignedIdentities" not in rid for rid in az.deletes())
        assert {r["type"].lower() for r in az.inventory} == RETAINED_TYPES
        assert az.groups[RG] is True

    def test_independent_resources_are_requested_together_then_the_network_chain(self, az):
        teardown_environment(config())

        order = [rid.split("/providers/")[1].split("/")[1].lower() for rid in az.deletes()]
        first = {t: order.index(t) for t in dict.fromkeys(order)}
        last = {t: len(order) - 1 - order[::-1].index(t) for t in dict.fromkeys(order)}
        independent = {
            "managedclusters",
            "systemtopics",
            "databaseaccounts",
            "namespaces",
            "storageaccounts",
            "registries",
            "vaults",
        }
        assert max(last[t] for t in independent) < first["natgateways"]
        assert last["natgateways"] < first["publicipaddresses"]
        assert last["publicipaddresses"] < first["virtualnetworks"]
        # No inventory read sits between the independent deletes: they were fired as one batch.
        delete_positions = [i for i, c in enumerate(az.calls) if c[1:3] == ["resource", "delete"]]
        batch = delete_positions[: len(independent) + 1]
        assert batch == list(range(batch[0], batch[0] + len(batch)))

    def test_the_context_is_pruned_once_the_nodes_group_is_gone(self, az):
        teardown_environment(config())

        az.prune.assert_called_once_with("spi-stack-dev1", server_fqdn=FQDN)
        nodes_check = next(
            i
            for i, c in enumerate(az.calls)
            if c[1:3] == ["group", "exists"] and f"{RG}-nodes" in c
        )
        cluster_delete = next(
            i
            for i, c in enumerate(az.calls)
            if c[1:3] == ["resource", "delete"] and "managedClusters" in c[4]
        )
        first_network_delete = next(
            i
            for i, c in enumerate(az.calls)
            if c[1:3] == ["resource", "delete"] and "natGateways" in c[4]
        )
        assert cluster_delete < nodes_check < first_network_delete

    def test_nat_gateway_is_detached_from_subnets_before_deletion(self, az):
        subnet_id = (
            f"{SUB}/resourceGroups/{RG}/providers/Microsoft.Network/virtualNetworks/v/subnets/aks"
        )
        az.subnets = [{"id": subnet_id, "name": "aks", "natGateway": {"id": "natgw"}}]

        teardown_environment(config())

        detach = next(i for i, c in enumerate(az.calls) if c[1:4] == ["network", "vnet", "subnet"])
        natgw_delete = next(
            i
            for i, c in enumerate(az.calls)
            if c[1:3] == ["resource", "delete"] and "natGateways" in c[4]
        )
        assert detach < natgw_delete
        assert az.subnets[0]["natGateway"] is None

    def test_a_missing_group_is_not_an_error(self, az):
        az.groups[RG] = False

        assert teardown_environment(config()) == []
        assert az.deletes() == []

    def test_a_lingering_nodes_group_blocks_a_resumed_network_teardown(self, az):
        """The cluster is gone but its nodes group is not: wait, do not delete the VNet."""
        az.inventory = [
            r
            for r in full_inventory()
            if "Microsoft.Network" in r["type"] or "userAssignedIdentities" in r["type"]
        ]
        az.groups[f"{RG}-nodes"] = True
        az.tick_seconds = 10 * 60.0

        with pytest.raises(TeardownError, match="nodes group"):
            teardown_environment(config())

        assert az.deletes() == []

    def test_identities_only_is_not_success_while_the_nodes_group_exists(self, az):
        az.inventory = [r for r in full_inventory() if "userAssignedIdentities" in r["type"]]
        az.groups[f"{RG}-nodes"] = True
        az.tick_seconds = 10 * 60.0

        with pytest.raises(TeardownError, match="nodes group"):
            teardown_environment(config())

    def test_an_already_torn_down_group_is_idempotent(self, az):
        az.inventory = [r for r in full_inventory() if "userAssignedIdentities" in r["type"]]
        az.groups[f"{RG}-nodes"] = False

        retained = teardown_environment(config())

        assert len(retained) == 3
        assert az.deletes() == []
        az.prune.assert_not_called()

    def test_a_resource_that_appears_mid_run_is_caught_by_the_next_pass(self, az):
        """Azure adds an Event Grid system topic beside a storage account on its own."""
        topic = _res("Microsoft.EventGrid/systemTopics", "late-topic")
        original = az.run_command
        state = {"added": False}

        def add_topic_after_natgw(cmd, **kw):
            if (
                cmd[1:3] == ["resource", "delete"]
                and "natGateways" in cmd[4]
                and not state["added"]
            ):
                az.inventory.append(topic)
                state["added"] = True
            return original(cmd, **kw)

        with patch("spi.teardown.run_command", side_effect=add_topic_after_natgw):
            retained = teardown_environment(config())

        assert len(retained) == 3
        assert topic["id"] in az.deletes()
        assert {r["type"].lower() for r in az.inventory} == RETAINED_TYPES


class TestReadsAndPrune:
    def test_a_transient_inventory_failure_is_retried(self, az):
        original = az.run_command
        flaky = {"left": 2}

        def throttled(cmd, **kw):
            if cmd[1:3] == ["resource", "list"] and flaky["left"]:
                flaky["left"] -= 1
                return subprocess.CompletedProcess(cmd, 1, "", "TooManyRequests")
            return original(cmd, **kw)

        with patch("spi.teardown.run_command", side_effect=throttled):
            retained = teardown_environment(config())

        assert len(retained) == 3

    def test_an_unreadable_api_server_reaches_the_prune_as_empty(self, az):
        original = az.run_command

        def no_aks(cmd, **kw):
            if cmd[1:3] == ["aks", "show"]:
                return subprocess.CompletedProcess(cmd, 1, "", "ResourceNotFound")
            return original(cmd, **kw)

        with patch("spi.teardown.run_command", side_effect=no_aks):
            teardown_environment(config())

        az.prune.assert_called_once_with("spi-stack-dev1", server_fqdn="")

    def test_the_prune_runs_once_even_when_a_second_pass_is_needed(self, az):
        topic = _res("Microsoft.EventGrid/systemTopics", "late-topic")
        original = az.run_command
        state = {"added": False}

        def add_topic(cmd, **kw):
            if (
                cmd[1:3] == ["resource", "delete"]
                and "natGateways" in cmd[4]
                and not state["added"]
            ):
                az.inventory.append(topic)
                state["added"] = True
            return original(cmd, **kw)

        with patch("spi.teardown.run_command", side_effect=add_topic):
            teardown_environment(config())

        az.prune.assert_called_once()


class TestTeardownStops:
    def test_a_resource_without_a_provisioning_state_is_still_re_requested(self, az):
        vault = next(r["id"] for r in az.inventory if "vaults" in r["type"])
        az.delete_failures[vault] = ["Conflict"] * 10
        original = az.run_command

        def stateless(cmd, **kw):
            if cmd[1:3] == ["resource", "show"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return original(cmd, **kw)

        with patch("spi.teardown.run_command", side_effect=stateless):
            with pytest.raises(TeardownError, match="declined 3 times"):
                teardown_environment(config())

    def test_delete_requests_carry_the_remaining_deadline_as_a_timeout(self, az):
        timeouts = []
        original = az.run_command

        def capture(cmd, **kw):
            if cmd[1:3] == ["resource", "delete"]:
                timeouts.append(kw.get("timeout"))
            return original(cmd, **kw)

        with patch("spi.teardown.run_command", side_effect=capture):
            teardown_environment(config())

        assert timeouts and all(0 < t <= 45 * 60 for t in timeouts)

    def test_a_request_declined_repeatedly_with_the_same_reason_stops_the_run(self, az):
        vault = next(r["id"] for r in az.inventory if "vaults" in r["type"])
        az.delete_failures[vault] = ["Conflict: vault is being purged"] * 10

        with pytest.raises(TeardownError, match="declined 3 times"):
            teardown_environment(config())

        assert az.deletes().count(vault) == 3

    def test_a_fatal_refusal_still_requests_the_rest_of_the_wave(self, az):
        cluster = az.inventory[0]["id"]
        az.delete_failures[cluster] = ["AuthorizationFailed"]

        with pytest.raises(TeardownError, match="Delete refused"):
            teardown_environment(config())

        first_wave = [
            r["id"]
            for r in full_inventory()
            if "userAssignedIdentities" not in r["type"] and "Microsoft.Network" not in r["type"]
        ]
        assert set(first_wave) <= set(az.deletes())

    def test_an_unplanned_resource_type_blocks_before_any_delete(self, az):
        az.inventory.append(_res("Microsoft.Web/sites", "mystery"))

        with pytest.raises(TeardownError, match="does not cover") as exc:
            teardown_environment(config())

        assert "mystery" in str(exc.value)
        assert az.deletes() == []

    def test_an_unreadable_inventory_is_a_failure(self, az):
        def broken(cmd, **kw):
            if cmd[1:3] == ["resource", "list"]:
                return subprocess.CompletedProcess(cmd, 1, "", "AuthorizationFailed")
            return az.run_command(cmd, **kw)

        with patch("spi.teardown.run_command", side_effect=broken):
            with pytest.raises(TeardownError, match="Could not inventory"):
                teardown_environment(config())
        assert az.deletes() == []

    def test_a_refused_delete_is_not_retried(self, az):
        cluster = az.inventory[0]["id"]
        az.delete_failures[cluster] = ["AuthorizationFailed: no permission", "unused"]

        with pytest.raises(TeardownError, match="Delete refused"):
            teardown_environment(config())

        assert az.deletes().count(cluster) == 1
        az.prune.assert_not_called()

    def test_a_declined_request_is_asked_again_once_the_resource_is_not_deleting(self, az):
        cluster = az.inventory[0]["id"]
        az.delete_failures[cluster] = ["Conflict: operation in progress"]

        teardown_environment(config())

        assert az.deletes().count(cluster) == 2
        shows = [c for c in az.calls if c[1:3] == ["resource", "show"]]
        assert shows and shows[0][4].endswith("/managedClusters/spi-stack-dev1")

    def test_the_deadline_reports_the_remaining_inventory_with_its_state(self, az):
        cluster = az.inventory[0]["id"]
        az.delete_failures[cluster] = ["Conflict"] * 50
        az.tick_seconds = 20 * 60.0

        with pytest.raises(TeardownError, match="deadline reached") as exc:
            teardown_environment(config())

        assert cluster in str(exc.value)
        assert "(Succeeded)" in str(exc.value)

    def test_a_lingering_nodes_group_blocks_network_teardown(self, az):
        original = az.run_command

        def sticky_nodes(cmd, **kw):
            result = original(cmd, **kw)
            if cmd[1:3] == ["group", "exists"] and f"{RG}-nodes" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "true", "")
            return result

        az.tick_seconds = 10 * 60.0
        with patch("spi.teardown.run_command", side_effect=sticky_nodes):
            with pytest.raises(TeardownError, match="nodes group"):
                teardown_environment(config())

        assert not any("natGateways" in rid for rid in az.deletes())


def _grant(pid, role, scope, gid="ra-1"):
    return {
        "id": f"{scope}/providers/Microsoft.Authorization/roleAssignments/{gid}",
        "roleDefinitionName": role,
        "scope": scope,
        "principalId": pid,
    }


ZONE = f"{SUB}/resourceGroups/dns-rg/providers/Microsoft.Network/dnsZones/example.com"


class TestPurge:
    def _identities(self, az):
        az.identities = [
            {"name": "spi-stack-dev1-external-dns", "principalId": "dns-pid"},
            {"name": "spi-stack-dev1-ctl-id", "principalId": "ctl-pid"},
        ]
        az.grants = {
            "dns-pid": [_grant("dns-pid", "DNS Zone Contributor", ZONE)],
            "ctl-pid": [
                _grant("ctl-pid", "Contributor", f"{SUB}/resourceGroups/{RG}-nodes", "ra-2"),
                _grant(
                    "ctl-pid",
                    "Network Contributor",
                    f"{SUB}/resourceGroups/{RG}/providers/Microsoft.Network/virtualNetworks/v",
                    "ra-3",
                ),
            ],
        }

    def test_removes_the_stack_owned_dns_grant_then_deletes_the_group(self, az):
        self._identities(az)

        purge_environment(config())

        removed = [c for c in az.calls if c[1:4] == ["role", "assignment", "delete"]]
        assert len(removed) == 1 and removed[0][-1].endswith("/ra-1")
        assert az.grants["dns-pid"] == []
        assert az.grants["ctl-pid"][0]["id"].endswith("/ra-2")
        group_delete = next(i for i, c in enumerate(az.calls) if c[1:3] == ["group", "delete"])
        assert az.calls.index(removed[0]) < group_delete
        assert az.groups[RG] is False
        az.prune.assert_called_once_with("spi-stack-dev1", server_fqdn=FQDN)

    def test_a_lingering_nodes_group_is_purged_with_the_environment(self, az):
        self._identities(az)
        az.inventory = [r for r in full_inventory() if "userAssignedIdentities" in r["type"]]
        az.groups[f"{RG}-nodes"] = True

        purge_environment(config())

        deleted = [c[c.index("--name") + 1] for c in az.calls if c[1:3] == ["group", "delete"]]
        assert deleted == [RG, f"{RG}-nodes"]
        assert az.groups[f"{RG}-nodes"] is False

    def test_an_orphaned_nodes_group_is_purged_when_the_environment_group_is_gone(self, az):
        az.groups[RG] = False
        az.groups[f"{RG}-nodes"] = True

        purge_environment(config())

        deleted = [c[c.index("--name") + 1] for c in az.calls if c[1:3] == ["group", "delete"]]
        assert deleted == [f"{RG}-nodes"]

    def test_a_grant_that_appears_during_purge_stops_it(self, az):
        self._identities(az)
        original = az.run_command
        state = {"deleted": False}

        def sneak(cmd, **kw):
            result = original(cmd, **kw)
            if cmd[1:4] == ["role", "assignment", "delete"] and not state["deleted"]:
                az.grants["ctl-pid"].append(
                    _grant("ctl-pid", "Reader", f"{SUB}/resourceGroups/elsewhere", "ra-7")
                )
                state["deleted"] = True
            return result

        with patch("spi.teardown.run_command", side_effect=sneak):
            with pytest.raises(TeardownError, match="appeared outside"):
                purge_environment(config())

        assert not any(c[1:3] == ["group", "delete"] for c in az.calls)

    def test_the_group_delete_request_is_bounded_by_the_deadline(self, az):
        self._identities(az)
        seen = []
        original = az.run_command

        def capture(cmd, **kw):
            if cmd[1:3] == ["group", "delete"]:
                seen.append(kw.get("timeout"))
            return original(cmd, **kw)

        with patch("spi.teardown.run_command", side_effect=capture):
            purge_environment(config())

        assert seen and 0 < seen[0] <= 45 * 60

    def test_an_identity_only_group_is_purged_without_touching_kubeconfig(self, az):
        self._identities(az)
        az.inventory = [r for r in full_inventory() if "userAssignedIdentities" in r["type"]]
        az.groups[f"{RG}-nodes"] = False

        purge_environment(config())

        assert az.groups[RG] is False
        az.prune.assert_not_called()

    def test_a_foreign_external_grant_stops_the_purge(self, az):
        self._identities(az)
        az.grants["ctl-pid"].append(
            _grant("ctl-pid", "Reader", f"{SUB}/resourceGroups/someone-elses", "ra-9")
        )

        with pytest.raises(TeardownError, match="did not create") as exc:
            purge_environment(config())

        assert "someone-elses" in str(exc.value)
        assert not any(c[1:3] == ["group", "delete"] for c in az.calls)
        assert not any(c[1:4] == ["role", "assignment", "delete"] for c in az.calls)
        assert az.groups[RG] is True

    def test_grant_discovery_failure_stops_the_purge(self, az):
        self._identities(az)
        original = az.run_command

        def broken(cmd, **kw):
            if cmd[1:4] == ["role", "assignment", "list"]:
                return subprocess.CompletedProcess(cmd, 1, "", "AuthorizationFailed")
            return original(cmd, **kw)

        with patch("spi.teardown.run_command", side_effect=broken):
            with pytest.raises(TeardownError, match="discover"):
                purge_environment(config())
        assert not any(c[1:3] == ["group", "delete"] for c in az.calls)

    def test_an_accepted_but_incomplete_delete_is_a_failure(self, az):
        self._identities(az)
        az.group_delete_polls_until_gone = 10_000
        az.tick_seconds = 20 * 60.0

        with pytest.raises(TeardownError, match="still exists"):
            purge_environment(config())

        az.prune.assert_not_called()

    def test_grants_are_matched_to_the_environment_case_insensitively(self):
        from spi.teardown import _in_environment

        cfg = config()
        assert _in_environment(f"{SUB}/resourcegroups/SPI-STACK-DEV1/providers/x/y/z", cfg)
        assert _in_environment(f"{SUB}/resourceGroups/spi-stack-dev1-nodes", cfg)
        assert not _in_environment(f"{SUB}/resourceGroups/spi-stack-dev1-other", cfg)
        assert not _in_environment(ZONE, cfg)

    def test_only_the_external_dns_identity_zone_grant_is_stack_owned(self):
        from spi.teardown import is_stack_owned

        cfg = config()
        dns = cfg.external_dns_identity_name
        assert is_stack_owned(ExternalGrant("i", ZONE, "DNS Zone Contributor", "p", dns), cfg)
        assert not is_stack_owned(ExternalGrant("i", ZONE, "Contributor", "p", dns), cfg)
        assert not is_stack_owned(
            ExternalGrant("i", ZONE, "DNS Zone Contributor", "p", "spi-stack-dev1-ctl-id"), cfg
        )
        assert not is_stack_owned(
            ExternalGrant("i", f"{SUB}/resourceGroups/dns-rg", "DNS Zone Contributor", "p", dns),
            cfg,
        )
        assert not is_stack_owned(
            ExternalGrant("i", f"{ZONE}/A/www", "DNS Zone Contributor", "p", dns), cfg
        )

    def test_a_zone_grant_held_by_another_identity_stops_the_purge(self, az):
        self._identities(az)
        az.grants["ctl-pid"].append(_grant("ctl-pid", "DNS Zone Contributor", ZONE, "ra-8"))

        with pytest.raises(TeardownError, match="did not create"):
            purge_environment(config())

        assert not any(c[1:4] == ["role", "assignment", "delete"] for c in az.calls)

    def test_removal_confirmation_tolerates_rbac_replication_lag(self, az):
        self._identities(az)
        original = az.run_command
        stale_reads = {"left": 2}

        def lagging(cmd, **kw):
            if cmd[1:4] == ["role", "assignment", "delete"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[1:4] == ["role", "assignment", "list"] and stale_reads["left"]:
                stale_reads["left"] -= 1
                return original(cmd, **kw)
            if cmd[1:4] == ["role", "assignment", "list"]:
                az.grants["dns-pid"] = []
            return original(cmd, **kw)

        with patch("spi.teardown.run_command", side_effect=lagging):
            purge_environment(config())

        assert az.groups[RG] is False


class TestCli:
    def _run(self, args):
        from typer.testing import CliRunner

        from spi.cli import app

        with (
            patch("spi.cli.check_prerequisites"),
            patch("spi.cli._resolve_name_suffix", return_value="abcde"),
            patch("spi.cli._show_config"),
            patch("spi.teardown.teardown_environment") as down,
            patch("spi.teardown.purge_environment") as purge,
        ):
            result = CliRunner().invoke(app, args)
            return result, down, purge

    def test_down_keeps_identities_by_default(self):
        result, down, purge = self._run(["down", "--env", "dev1"])
        assert result.exit_code == 0, result.output
        down.assert_called_once()
        purge.assert_not_called()

    def test_purge_routes_to_the_group_delete(self):
        result, down, purge = self._run(["down", "--env", "dev1", "--purge"])
        assert result.exit_code == 0, result.output
        purge.assert_called_once()
        down.assert_not_called()

    def test_a_teardown_error_exits_nonzero_with_the_reason(self):
        from typer.testing import CliRunner

        from spi.cli import app

        with (
            patch("spi.cli.check_prerequisites"),
            patch("spi.cli._resolve_name_suffix", return_value="abcde"),
            patch("spi.cli._show_config"),
            patch("spi.teardown.teardown_environment", side_effect=TeardownError("plan gap: x")),
        ):
            result = CliRunner().invoke(app, ["down", "--env", "dev1"])
        assert result.exit_code == 1
        assert "plan gap: x" in result.output


class TestRunCommandTimeout:
    def test_an_expired_timeout_is_reported_as_a_failed_result(self):
        from spi.shell import run_command

        with patch(
            "spi.shell.run_process",
            side_effect=subprocess.TimeoutExpired(cmd=["az"], timeout=3),
        ):
            result = run_command(
                ["az", "resource", "delete"], display=False, check=False, timeout=3
            )

        assert result.returncode == 124
        assert "timed out" in result.stderr


class TestEveryAzureCallIsBounded:
    def test_no_az_call_runs_without_a_timeout(self, az):
        missing = []
        original = az.run_command

        def check(cmd, **kw):
            if not kw.get("timeout"):
                missing.append(cmd[:3])
            return original(cmd, **kw)

        with patch("spi.teardown.run_command", side_effect=check):
            teardown_environment(config())
            az.identities = [{"name": "spi-stack-dev1-ctl-id", "principalId": "ctl-pid"}]
            az.groups[RG] = True
            purge_environment(config())

        assert missing == []
