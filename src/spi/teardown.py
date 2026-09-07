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

"""Identity-preserving teardown of an environment resource group.

``spi down`` deletes the group's resources individually, in dependency
order, and leaves the managed identities standing with the group and its
tags, so a rebuild never rotates the deploy identity's client id.
``spi down --purge`` removes the identities' out-of-group grants and then
deletes the whole group.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Sequence

from .config import Config
from .console import console, display_result
from .shell import prune_kube_context, run_command

TEARDOWN_DEADLINE_SECONDS = 45 * 60
POLL_INTERVAL_SECONDS = 15
RETRY_BACKOFF_SECONDS = (10, 20, 40, 60)

RETAINED_TYPES = frozenset({"microsoft.managedidentity/userassignedidentities"})
CLUSTER_TYPE = "microsoft.containerservice/managedclusters"

# Azure reports these as terminal failures; retrying cannot change the outcome.
FATAL_ERROR_MARKERS = ("AuthorizationFailed", "LinkedAuthorizationFailed", "ScopeLocked")

DNS_ZONE_CONTRIBUTOR = "DNS Zone Contributor"
DNS_ZONE_SCOPE_MARKER = "/providers/microsoft.network/dnszones/"


class TeardownError(RuntimeError):
    """Teardown stopped before the group reached its target state."""


@dataclass(frozen=True)
class AzureResource:
    id: str
    type: str
    name: str

    @staticmethod
    def from_json(item: dict) -> "AzureResource":
        return AzureResource(
            id=item.get("id", ""),
            type=item.get("type", "").lower(),
            name=item.get("name", ""),
        )


@dataclass(frozen=True)
class Wave:
    """Resource types deleted together, after every earlier wave is gone."""

    types: frozenset
    before: Optional[Callable[["TeardownRun"], None]] = None
    after: Optional[Callable[["TeardownRun"], None]] = None


@dataclass
class TeardownRun:
    config: Config
    deadline: float
    api_server: str = ""
    had_cluster: bool = False
    inventory: List[AzureResource] = field(default_factory=list)


def _wave(*types: str, **hooks) -> Wave:
    return Wave(types=frozenset(t.lower() for t in types), **hooks)


def _detach_nat_gateways(run: TeardownRun) -> None:
    """Azure refuses to delete a NAT gateway that a subnet still references."""
    result = run_command(
        ["az", "network", "vnet", "list", "-g", run.config.resource_group, "-o", "json"],
        description=f"List virtual networks in {run.config.resource_group}",
        display=False,
        check=False,
    )
    if result.returncode != 0:
        raise TeardownError(
            f"Could not list virtual networks in {run.config.resource_group}: "
            f"{result.stderr.strip()}"
        )
    for vnet in json.loads(result.stdout or "[]"):
        for subnet in vnet.get("subnets") or []:
            if not subnet.get("natGateway"):
                continue
            detach = run_command(
                [
                    "az",
                    "network",
                    "vnet",
                    "subnet",
                    "update",
                    "--ids",
                    subnet["id"],
                    "--remove",
                    "natGateway",
                ],
                description=f"Detach NAT gateway from subnet {subnet.get('name', '')}",
                check=False,
            )
            if detach.returncode != 0:
                raise TeardownError(
                    f"Could not detach the NAT gateway from {subnet['id']}: {detach.stderr.strip()}"
                )


def _confirm_nodes_group_gone(run: TeardownRun) -> None:
    """The managed nodes group lags the cluster delete; network teardown waits for it."""
    nodes_group = run.config.node_resource_group
    console.print(f"  [info]Waiting for managed nodes group {nodes_group} to be gone...[/info]")
    if not wait_for_group_gone(nodes_group, run.deadline):
        raise TeardownError(
            f"Managed nodes group {nodes_group} still exists after the cluster delete; "
            "network teardown cannot start while it holds node resources."
        )
    if run.had_cluster:
        prune_kube_context(run.config.cluster_name, server_fqdn=run.api_server)


# The first wave holds everything without a dependency on anything else in
# the group, so its deletes run concurrently. Only the network chain needs
# ordering, and only after the cluster's nodes have left the VNet. The types
# are what infra/*.bicep provisions plus what Azure adds beside them (Event
# Grid system topics on storage accounts, smart detection rules and their
# action group on Application Insights). A type missing here blocks
# teardown rather than being deleted blindly.
DELETION_PLAN: Sequence[Wave] = (
    _wave(
        "Microsoft.ContainerService/managedClusters",
        "Microsoft.EventGrid/systemTopics",
        "Microsoft.DocumentDB/databaseAccounts",
        "Microsoft.ServiceBus/namespaces",
        "Microsoft.Storage/storageAccounts",
        "Microsoft.ContainerRegistry/registries",
        "Microsoft.KeyVault/vaults",
        "Microsoft.AlertsManagement/smartDetectorAlertRules",
        "Microsoft.Insights/actionGroups",
        "Microsoft.Insights/components",
        "Microsoft.OperationalInsights/workspaces",
        after=_confirm_nodes_group_gone,
    ),
    _wave("Microsoft.Network/natGateways", before=_detach_nat_gateways),
    _wave("Microsoft.Network/publicIPAddresses"),
    _wave("Microsoft.Network/virtualNetworks"),
)

HANDLED_TYPES = RETAINED_TYPES.union(*(wave.types for wave in DELETION_PLAN))


def group_exists(name: str) -> bool:
    result = run_command(
        ["az", "group", "exists", "--name", name],
        description=f"Check resource group status: {name}",
        display=False,
        check=False,
    )
    if result.returncode != 0:
        raise TeardownError(f"Could not check resource group {name}: {result.stderr.strip()}")
    return result.stdout.strip().lower() == "true"


def wait_for_group_gone(name: str, deadline: float) -> bool:
    while True:
        if not group_exists(name):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL_SECONDS)


def list_group_resources(resource_group: str) -> List[AzureResource]:
    """An unreadable inventory is a failure, never an empty group."""
    result = run_command(
        ["az", "resource", "list", "--resource-group", resource_group, "-o", "json"],
        description=f"Inventory resource group: {resource_group}",
        display=False,
        check=False,
    )
    if result.returncode != 0:
        raise TeardownError(
            f"Could not inventory resource group {resource_group}: {result.stderr.strip()}"
        )
    try:
        items = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise TeardownError(f"Unreadable inventory for {resource_group}: {exc}") from exc
    return [AzureResource.from_json(item) for item in items]


def unhandled_resources(inventory: Iterable[AzureResource]) -> List[AzureResource]:
    return [r for r in inventory if r.type not in HANDLED_TYPES]


def retained_resources(inventory: Iterable[AzureResource]) -> List[AzureResource]:
    return [r for r in inventory if r.type in RETAINED_TYPES]


def _is_fatal(stderr: str) -> bool:
    return any(marker in stderr for marker in FATAL_ERROR_MARKERS)


def request_delete(resource: AzureResource) -> bool:
    """Ask Azure to delete one resource without waiting; the inventory poll confirms.

    Returns False when Azure declined the request for a reason worth retrying
    (a dependency or an operation in progress). Terminal refusals raise.
    """
    result = run_command(
        ["az", "resource", "delete", "--ids", resource.id, "--no-wait"],
        description=f"Delete {resource.type.split('/')[-1]}: {resource.name}",
        check=False,
    )
    if result.returncode == 0:
        return True
    reason = result.stderr.strip()
    if _is_fatal(reason):
        raise TeardownError(f"Delete refused for {resource.id}: {reason}")
    console.print(f"  [warning]Delete not accepted for {resource.name}: {reason}[/warning]")
    return False


def _request_deletes(resources: Sequence[AzureResource]) -> None:
    """One thread per resource: `az resource delete --no-wait` still blocks on
    long-running deletes such as AKS and Cosmos, and the point is to have Azure
    work on all of them at once."""
    if not resources:
        return
    with ThreadPoolExecutor(max_workers=len(resources)) as pool:
        list(pool.map(request_delete, resources))


def provisioning_state(resource: AzureResource) -> str:
    result = run_command(
        [
            "az",
            "resource",
            "show",
            "--ids",
            resource.id,
            "--query",
            "properties.provisioningState",
            "-o",
            "tsv",
        ],
        description=f"Read state of {resource.name}",
        display=False,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def delete_wave(run: TeardownRun, wave: Wave) -> None:
    """Fire the wave's deletes together, then poll until the inventory shows them gone.

    A resource that is still present and no longer reports ``Deleting`` had
    its delete fail behind the accepted request, so it is asked for again
    with backoff until the deadline.
    """
    targets = [r for r in run.inventory if r.type in wave.types]
    _request_deletes(targets)
    attempt = 0
    next_check = time.monotonic() + RETRY_BACKOFF_SECONDS[0]
    while True:
        run.inventory = list_group_resources(run.config.resource_group)
        lingering = [r for r in run.inventory if r.type in wave.types]
        if not lingering:
            return
        now = time.monotonic()
        if now >= run.deadline:
            raise TeardownError(
                "Teardown deadline reached with resources remaining:\n  "
                + "\n  ".join(f"{r.id} ({provisioning_state(r) or 'unknown'})" for r in lingering)
            )
        if now >= next_check:
            stalled = [r for r in lingering if provisioning_state(r).lower() != "deleting"]
            _request_deletes(stalled)
            attempt += 1
            backoff = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            next_check = now + backoff
        time.sleep(POLL_INTERVAL_SECONDS)


def _describe(resources: Iterable[AzureResource]) -> str:
    return "\n  ".join(f"{r.type}  {r.name}" for r in resources)


def _cluster_api_server(config: Config) -> str:
    result = run_command(
        [
            "az",
            "aks",
            "show",
            "--resource-group",
            config.resource_group,
            "--name",
            config.cluster_name,
            "--query",
            "privateFqdn || fqdn",
            "--output",
            "tsv",
        ],
        description=f"Look up API server for {config.cluster_name}",
        display=False,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _check_plan_covers(inventory: Iterable[AzureResource]) -> None:
    unhandled = unhandled_resources(inventory)
    if unhandled:
        raise TeardownError(
            "Resource group holds resources the teardown plan does not cover; "
            "they were left in place:\n  " + _describe(unhandled)
        )


def teardown_environment(config: Config) -> List[AzureResource]:
    """Delete everything in the group except managed identities.

    The plan runs in passes: a resource Azure adds while an earlier wave is
    in flight (an Event Grid system topic on a storage account, for example)
    is picked up by the next pass. Returns the retained resources. Raises
    ``TeardownError`` when the group holds a resource type the plan does not
    cover, when a delete is refused, when a pass makes no progress, or when
    the deadline passes with resources remaining.
    """
    rg = config.resource_group
    console.print(f"\n[bold]Tearing down {rg} (managed identities are kept)...[/bold]")
    if not group_exists(rg):
        display_result(f"Resource group {rg} does not exist; nothing to tear down")
        return []

    run = TeardownRun(config=config, deadline=time.monotonic() + TEARDOWN_DEADLINE_SECONDS)
    run.inventory = list_group_resources(rg)
    _check_plan_covers(run.inventory)
    run.had_cluster = any(r.type == CLUSTER_TYPE for r in run.inventory)
    run.api_server = _cluster_api_server(config) if run.had_cluster else ""

    while True:
        pending = {r.id for r in run.inventory if r.type not in RETAINED_TYPES}
        if not pending:
            break
        for wave in DELETION_PLAN:
            if not any(r.type in wave.types for r in run.inventory):
                continue
            if wave.before:
                wave.before(run)
            delete_wave(run, wave)
            if wave.after:
                wave.after(run)
        run.inventory = list_group_resources(rg)
        _check_plan_covers(run.inventory)
        remaining = {r.id for r in run.inventory if r.type not in RETAINED_TYPES}
        if remaining and remaining >= pending:
            raise TeardownError(
                "Teardown made no progress; resources remain:\n  "
                + _describe(r for r in run.inventory if r.id in remaining)
            )
        if remaining:
            console.print(
                "  [info]New resources appeared during teardown; running another pass[/info]"
            )

    retained = retained_resources(run.inventory)
    display_result(f"Resource group {rg} holds only managed identities")
    for identity in retained:
        console.print(f"  [dim]retained {identity.name}[/dim]")
    return retained


@dataclass(frozen=True)
class ExternalGrant:
    id: str
    scope: str
    role: str
    principal_id: str


def _in_environment(scope: str, config: Config) -> bool:
    """Grants inside the group or its managed nodes group die with those groups."""
    lowered = scope.lower()
    for group in (config.resource_group, config.node_resource_group):
        marker = f"/resourcegroups/{group.lower()}"
        if lowered.endswith(marker) or f"{marker}/" in lowered:
            return True
    return False


def discover_external_grants(config: Config) -> List[ExternalGrant]:
    """Role assignments the group's identities hold outside the environment."""
    listed = run_command(
        [
            "az",
            "identity",
            "list",
            "--resource-group",
            config.resource_group,
            "--query",
            "[].{name:name, principalId:principalId}",
            "-o",
            "json",
        ],
        description=f"List managed identities in {config.resource_group}",
        display=False,
        check=False,
    )
    if listed.returncode != 0:
        raise TeardownError(
            f"Could not list identities in {config.resource_group}: {listed.stderr.strip()}"
        )
    grants: List[ExternalGrant] = []
    for identity in json.loads(listed.stdout or "[]"):
        principal_id = identity.get("principalId") or ""
        if not principal_id:
            continue
        result = run_command(
            ["az", "role", "assignment", "list", "--all", "--assignee", principal_id, "-o", "json"],
            description=f"Discover role assignments for {identity.get('name', principal_id)}",
            display=False,
            check=False,
        )
        if result.returncode != 0:
            raise TeardownError(
                f"Could not discover role assignments for {principal_id}: {result.stderr.strip()}"
            )
        for item in json.loads(result.stdout or "[]"):
            scope = item.get("scope", "")
            if _in_environment(scope, config):
                continue
            grants.append(
                ExternalGrant(
                    id=item.get("id", ""),
                    scope=scope,
                    role=item.get("roleDefinitionName", ""),
                    principal_id=principal_id,
                )
            )
    return grants


def is_stack_owned(grant: ExternalGrant) -> bool:
    return grant.role == DNS_ZONE_CONTRIBUTOR and DNS_ZONE_SCOPE_MARKER in grant.scope.lower()


def remove_external_grants(config: Config) -> List[ExternalGrant]:
    """Remove the stack-owned external grants; anything else stops the purge."""
    grants = discover_external_grants(config)
    foreign = [g for g in grants if not is_stack_owned(g)]
    if foreign:
        raise TeardownError(
            "Identities hold role assignments outside the environment that the stack "
            "did not create; remove them before purging:\n  "
            + "\n  ".join(f"{g.role} at {g.scope} ({g.id})" for g in foreign)
        )
    for grant in grants:
        removed = run_command(
            ["az", "role", "assignment", "delete", "--ids", grant.id],
            description=f"Remove {grant.role} from {grant.scope.rsplit('/', 1)[-1]}",
            check=False,
        )
        if removed.returncode != 0:
            raise TeardownError(f"Could not remove {grant.id}: {removed.stderr.strip()}")
    if grants:
        still = [g for g in discover_external_grants(config) if g.id in {x.id for x in grants}]
        if still:
            raise TeardownError(
                "Role assignments still present after removal:\n  "
                + "\n  ".join(g.id for g in still)
            )
    return grants


def purge_environment(config: Config) -> None:
    """Delete the whole group, identities included, after external grants are gone."""
    rg = config.resource_group
    console.print(f"\n[bold]Purging {rg} (identities and group will be deleted)...[/bold]")
    if not group_exists(rg):
        display_result(f"Resource group {rg} does not exist; nothing to purge")
        return

    deadline = time.monotonic() + TEARDOWN_DEADLINE_SECONDS
    api_server = _cluster_api_server(config)
    removed = remove_external_grants(config)
    if removed:
        console.print(f"  [info]Removed {len(removed)} external role assignment(s)[/info]")

    result = run_command(
        ["az", "group", "delete", "--name", rg, "--yes", "--no-wait"],
        description=f"Delete resource group: {rg}",
        check=False,
    )
    if result.returncode != 0:
        raise TeardownError(f"Purge request failed for {rg}: {result.stderr.strip()}")

    console.print(f"  [info]Waiting for Azure to report {rg} gone...[/info]")
    if not wait_for_group_gone(rg, deadline):
        raise TeardownError(
            f"Resource group {rg} still exists after the purge deadline; the delete was "
            f"accepted but has not completed. Verify with: az group exists --name {rg}"
        )
    prune_kube_context(config.cluster_name, server_fqdn=api_server)
    display_result(f"Resource group {rg} deleted")
