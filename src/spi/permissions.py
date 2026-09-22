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

"""What the deployer needs from a subscription, and a check that reports the gap.

The check reads the ARM permissions API for the signed-in principal, which
Reader can call and which avoids Microsoft Graph. It evaluates role-based
``actions`` and ``notActions`` only: assignment conditions and deny
assignments are not evaluated.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import typer
from rich.panel import Panel
from rich.table import Table

from .checks import detect_platform
from .console import console
from .shell import run_command

# Every role the stack assigns, by name and built-in definition id. The
# constrained grant's condition is built from this map, and a test keeps it
# equal to the role ids under infra/.
STACK_ROLES: Dict[str, str] = {
    "Network Contributor": "4d97b98b-1d4f-4787-a291-c67834d212e7",
    "Azure Kubernetes Service RBAC Cluster Admin": "b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b",
    "Azure Kubernetes Service Cluster User Role": "4abbcc35-e782-43d8-92c5-2d3f1bd2253f",
    "Key Vault Secrets Officer": "b86a8fe4-44ce-4948-aee5-eccb2c155cd7",
    "Key Vault Secrets User": "4633458b-17de-408a-b874-0445c86b69e6",
    "Storage Blob Data Contributor": "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
    "Storage Table Data Contributor": "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3",
    "Azure Service Bus Data Sender": "69a216fc-b8fb-44d8-bc22-1f3c2cd27a39",
    "Azure Service Bus Data Receiver": "4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0",
    "AcrPull": "7f951dda-4ed3-4680-a7ca-43fe172d538d",
    "DNS Zone Contributor": "befefa01-2a29-4197-83a8-272ff33ce314",
}
AKS_RBAC_CLUSTER_ADMIN_ROLE_ID = STACK_ROLES["Azure Kubernetes Service RBAC Cluster Admin"]

CREATE_GROUP = "Microsoft.Resources/subscriptions/resourceGroups/write"
CREATE_CLUSTER = "Microsoft.ContainerService/managedClusters/write"
WRITE_ASSIGNMENT = "Microsoft.Authorization/roleAssignments/write"
DELETE_ASSIGNMENT = "Microsoft.Authorization/roleAssignments/delete"

ACTION_LABELS: Dict[str, str] = {
    CREATE_GROUP: "Create resource groups",
    CREATE_CLUSTER: "Create AKS clusters",
    WRITE_ASSIGNMENT: "Create role assignments",
    DELETE_ASSIGNMENT: "Remove role assignments",
}
DEPLOY_ACTIONS: Tuple[str, ...] = tuple(ACTION_LABELS)
CONTRIBUTOR_ACTIONS = (CREATE_GROUP, CREATE_CLUSTER)
GRANT_ACTIONS = (WRITE_ASSIGNMENT, DELETE_ASSIGNMENT)

DOCS_POINTER = "docs/install.md#permissions"
PERMISSIONS_API_VERSION = "2022-04-01"


@dataclass
class ScopeResult:
    scope: str
    reason: str
    actions: Dict[str, bool] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def denied(self) -> List[str]:
        return [a for a, allowed in self.actions.items() if not allowed]

    def to_json(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "reason": self.reason,
            "actions": self.actions,
            "error": self.error,
        }


def subscription_scope(subscription_id: str) -> str:
    return f"/subscriptions/{subscription_id}"


def group_scope(subscription_id: str, resource_group: str) -> str:
    return f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"


def read_permissions(scope: str) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """The caller's role-based permission entries at a scope, or None and the reason."""
    result = run_command(
        [
            "az",
            "rest",
            "--method",
            "get",
            "--url",
            (
                f"https://management.azure.com{scope}"
                "/providers/Microsoft.Authorization/permissions"
                f"?api-version={PERMISSIONS_API_VERSION}"
            ),
            "--output",
            "json",
        ],
        description=f"Read permissions at {scope}",
        display=False,
        check=False,
    )
    if result.returncode != 0:
        lines = (result.stderr or "").strip().splitlines()
        return None, lines[0] if lines else f"az exited {result.returncode}"
    try:
        value = json.loads(result.stdout or "").get("value")
    except (ValueError, AttributeError):
        value = None
    if not isinstance(value, list):
        return None, "unexpected response from the permissions API"
    return value, ""


def _matches(pattern: str, action: str) -> bool:
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, action, flags=re.IGNORECASE) is not None


def _entry_allows(entry: Dict[str, Any], action: str) -> bool:
    granted = any(_matches(p, action) for p in entry.get("actions") or [])
    excluded = any(_matches(p, action) for p in entry.get("notActions") or [])
    return granted and not excluded


def action_allowed(action: str, entries: Sequence[Dict[str, Any]]) -> bool:
    """Each entry is one role assignment's actions minus its notActions; any entry suffices."""
    return any(_entry_allows(e, action) for e in entries)


def evaluate(scope: str, reason: str, actions: Sequence[str]) -> ScopeResult:
    entries, error = read_permissions(scope)
    if entries is None:
        return ScopeResult(scope, reason, error=error)
    return ScopeResult(
        scope,
        reason,
        actions={a: action_allowed(a, entries) for a in actions},
    )


def stack_condition() -> str:
    """The ABAC condition limiting role-assignment writes and deletes to STACK_ROLES."""
    ids = ", ".join(STACK_ROLES.values())
    return (
        "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR "
        "(@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
        f"ForAnyOfAnyValues:GuidEquals {{{ids}}})) AND "
        "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR "
        "(@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
        f"ForAnyOfAnyValues:GuidEquals {{{ids}}}))"
    )


def grant_commands(
    object_id: str,
    principal_type: str,
    subscription_id: str,
    denied: Sequence[str],
    powershell: Optional[bool] = None,
) -> List[str]:
    """The admin commands that close the gap, only for what is missing.

    Bash form by default, PowerShell on native Windows; both single-quote the
    condition so neither shell expands its `!`, `$`, or braces.
    """
    if powershell is None:
        powershell = detect_platform() == "windows"
    if powershell:
        cont = " `\n  "
        condition = "'" + stack_condition().replace("'", "''") + "'"
    else:
        cont = " \\\n  "
        condition = "'" + stack_condition().replace("'", "'\\''") + "'"
    assignee = [
        f"--assignee-object-id {object_id}",
        f"--assignee-principal-type {principal_type}",
        f"--scope {subscription_scope(subscription_id)}",
    ]
    commands = []
    if any(a in denied for a in CONTRIBUTOR_ACTIONS):
        parts = ["az role assignment create", '--role "Contributor"', *assignee]
        commands.append(cont.join(parts))
    if any(a in denied for a in GRANT_ACTIONS):
        parts = [
            "az role assignment create",
            '--role "Role Based Access Control Administrator"',
            *assignee,
            "--condition-version 2.0",
            f"--condition {condition}",
        ]
        commands.append(cont.join(parts))
    return commands


def render_table(results: Sequence[ScopeResult]) -> Table:
    table = Table(title="Azure Permissions", border_style="cyan")
    table.add_column("Permission", style="cyan", min_width=10)
    table.add_column("Status", justify="center", min_width=8)
    table.add_column("Detail")
    for result in results:
        for action, allowed in result.actions.items():
            status = "[success]OK[/success]" if allowed else "[error]MISSING[/error]"
            table.add_row(ACTION_LABELS.get(action, action), status, action)
    return table


def report_denial(results: Sequence[ScopeResult], commands: Sequence[str], command: str) -> None:
    """The missing-permission panel, then the commands as plain text so they paste intact."""
    denied = {a for r in results for a in r.denied}
    lines = []
    if denied & set(CONTRIBUTOR_ACTIONS):
        lines.append(f"{command} needs Contributor on the subscription to create the stack.")
    if denied & set(GRANT_ACTIONS):
        lines.append(
            f"{command} assigns {len(STACK_ROLES)} Azure roles to the identities it creates "
            "and to you.\nContributor cannot create role assignments."
        )
    noun = "command" if len(commands) == 1 else "commands"
    lines.append(f"Ask a subscription admin to run the {noun} below.")
    lines.append(
        "The grant only allows assigning the roles the stack uses. Owner or\n"
        "User Access Administrator is not needed."
    )
    lines.append(f"Full requirement and admin steps: {DOCS_POINTER}.")
    console.print()
    console.print(
        Panel("\n\n".join(lines), title="Missing permission", border_style="error", expand=False)
    )
    for text in commands:
        console.print()
        console.print(text, markup=False, highlight=False, soft_wrap=True)


def resolve_scope(subscription_id: str, resource_group: str) -> ScopeResult:
    """Evaluate at the group when it exists, otherwise at the subscription."""
    exists = run_command(
        ["az", "group", "exists", "--name", resource_group],
        description=f"Check resource group exists: {resource_group}",
        display=False,
        check=False,
    )
    if exists.returncode == 0 and exists.stdout.strip().lower() == "true":
        scope = group_scope(subscription_id, resource_group)
        actions = (CREATE_CLUSTER, WRITE_ASSIGNMENT, DELETE_ASSIGNMENT)
        return evaluate(scope, "resource group", actions)
    return evaluate(subscription_scope(subscription_id), "subscription scope", DEPLOY_ACTIONS)


def require_deploy_permissions(
    account: Dict[str, Any], principal: Tuple[str, str], resource_group: str
) -> None:
    """Stop `spi up` before its first mutation when the deployer lacks a required permission."""
    subscription_id = account.get("id", "")
    console.print("\n[bold]Checking Azure permissions...[/bold]")
    result = resolve_scope(subscription_id, resource_group)
    if result.error:
        console.print(
            f"  [warning]Permissions not checked at {result.scope}: {result.error}. "
            "Continuing.[/warning]"
        )
        return
    if not result.denied:
        console.print(f"  [success]Required permissions present ({result.reason})[/success]")
        return
    console.print()
    console.print(render_table([result]))
    object_id, principal_type = principal
    commands = grant_commands(object_id, principal_type, subscription_id, result.denied)
    report_denial([result], commands, "spi up")
    raise typer.Exit(code=1)


@dataclass
class AzureCheck:
    """The Azure half of `spi check`: who is signed in and what they may do."""

    checked: bool
    detail: str = ""
    user: str = ""
    subscription_id: str = ""
    principal: Tuple[str, str] = ("", "")
    result: Optional[ScopeResult] = None
    commands: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.result and self.result.denied)

    def to_json(self) -> Dict[str, Any]:
        if not self.checked:
            return {"checked": False, "detail": self.detail, "ok": True}
        object_id, principal_type = self.principal
        return {
            "checked": True,
            "principal": {"object_id": object_id, "type": principal_type},
            "scopes": [self.result.to_json()] if self.result else [],
            "grant_commands": self.commands,
            "ok": self.ok,
        }


def _quiet_principal(account: Dict[str, Any]) -> Tuple[str, str]:
    """Object id and type without Graph or console output; a placeholder when unknown."""
    from .azure_infra import _deployer_oid_from_arm_token, _deployer_principal_type

    object_id = os.environ.get("SPI_DEPLOYER_OID", "").strip() or _deployer_oid_from_arm_token()
    return object_id or "<object-id>", _deployer_principal_type(account)


def check_azure(az_installed: bool) -> AzureCheck:
    """Evaluate the deploy actions at subscription scope for whoever `az` is signed in as."""
    if not az_installed:
        return AzureCheck(checked=False, detail="az is not installed")
    shown = run_command(
        ["az", "account", "show", "--output", "json"],
        description="Check Azure sign-in",
        display=False,
        check=False,
    )
    try:
        account = json.loads(shown.stdout) if shown.returncode == 0 else None
    except ValueError:
        account = None
    if not isinstance(account, dict) or not account.get("id"):
        return AzureCheck(checked=False, detail="not signed in to Azure (az login)")
    subscription_id = account["id"]
    principal = _quiet_principal(account)
    result = evaluate(subscription_scope(subscription_id), "subscription scope", DEPLOY_ACTIONS)
    check = AzureCheck(
        checked=True,
        user=account.get("user", {}).get("name", ""),
        subscription_id=subscription_id,
        principal=principal,
        result=result,
    )
    if result.denied:
        check.commands = grant_commands(*principal, subscription_id, result.denied)
    return check


def print_azure_check(check: AzureCheck) -> None:
    console.print()
    if not check.checked:
        table = Table(title="Azure Permissions", border_style="cyan")
        table.add_column("Permission", style="cyan", min_width=10)
        table.add_column("Status", justify="center", min_width=8)
        table.add_column("Detail")
        table.add_row("Azure", "[warning]SKIPPED[/warning]", f"[dim]{check.detail}[/dim]")
        console.print(table)
        return
    _, principal_type = check.principal
    console.print(
        f"Signed in as {check.user} ({principal_type}) on subscription {check.subscription_id}"
    )
    console.print("Checking at subscription scope.")
    result = check.result
    if result is None or result.error:
        detail = result.error if result else "no result"
        console.print(f"[warning]Permissions not checked: {detail}.[/warning]")
        return
    console.print()
    console.print(render_table([result]))
    if not result.denied:
        console.print(f"\n[success]All {len(result.actions)} permissions present.[/success]")
        return
    report_denial([result], check.commands, "spi up")
    console.print(
        f"\n[warning]{len(result.denied)} of {len(result.actions)} permissions missing.[/warning]"
    )
