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

"""Tests for the deployer permission check and the role set it prints."""

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from spi import permissions
from spi.cli import _resolve_up_context, app
from spi.permissions import (
    CREATE_CLUSTER,
    CREATE_GROUP,
    DELETE_ASSIGNMENT,
    DEPLOY_ACTIONS,
    STACK_ROLES,
    WRITE_ASSIGNMENT,
    action_allowed,
    grant_commands,
    stack_condition,
)

INFRA = Path(__file__).resolve().parent.parent / "infra"

# Synthetic fixture values; never real principal or subscription identifiers.
SUB_ID = "7d6e5f4a-9999-4888-b777-666655554444"
OID = "0a1b2c3d-1111-4222-8333-444455556666"
ACCOUNT = {
    "id": SUB_ID,
    "name": "example",
    "tenantId": "tenant",
    "user": {"type": "user", "name": "a.user@contoso.com"},
}

# Shapes of GET {scope}/providers/Microsoft.Authorization/permissions.
CONTRIBUTOR = {
    "actions": ["*"],
    "notActions": [
        "Microsoft.Authorization/*/Delete",
        "Microsoft.Authorization/*/Write",
        "Microsoft.Authorization/elevateAccess/Action",
    ],
}
OWNER = {"actions": ["*"], "notActions": []}
READER = {"actions": ["*/read"], "notActions": []}
CONSTRAINED_GRANT = {
    "actions": [
        "Microsoft.Authorization/roleAssignments/write",
        "Microsoft.Authorization/roleAssignments/delete",
        "*/read",
    ],
    "notActions": [],
    "condition": "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR ...)",
    "conditionVersion": "2.0",
}


def _evaluate(entries):
    return {a: action_allowed(a, entries) for a in DEPLOY_ACTIONS}


class TestEvaluator:
    def test_contributor_cannot_create_or_remove_role_assignments(self):
        assert _evaluate([CONTRIBUTOR]) == {
            CREATE_GROUP: True,
            CREATE_CLUSTER: True,
            WRITE_ASSIGNMENT: False,
            DELETE_ASSIGNMENT: False,
        }

    def test_owner_allows_everything(self):
        assert all(_evaluate([OWNER]).values())

    def test_reader_allows_nothing(self):
        assert not any(_evaluate([READER]).values())

    def test_contributor_with_the_constrained_grant_allows_everything(self):
        assert all(_evaluate([CONTRIBUTOR, CONSTRAINED_GRANT]).values())

    def test_one_assignments_not_actions_do_not_cancel_anothers_actions(self):
        entries = [CONTRIBUTOR, {"actions": [WRITE_ASSIGNMENT], "notActions": []}]
        assert action_allowed(WRITE_ASSIGNMENT, entries)
        assert not action_allowed(DELETE_ASSIGNMENT, entries)

    def test_matching_ignores_case(self):
        entries = [{"actions": ["microsoft.authorization/ROLEASSIGNMENTS/*"], "notActions": []}]
        assert action_allowed(WRITE_ASSIGNMENT, entries)


def _az(permissions_by_scope, group_exists=False):
    """A run_command fake answering sign-in, group existence, and permission reads."""
    calls = []

    def run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["account", "show"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(ACCOUNT), "")
        if cmd[1:3] == ["group", "exists"]:
            return subprocess.CompletedProcess(cmd, 0, "true" if group_exists else "false", "")
        if cmd[1] == "rest":
            url = cmd[cmd.index("--url") + 1]
            scope = url.split("https://management.azure.com", 1)[1].split("/providers/")[0]
            if scope not in permissions_by_scope:
                return subprocess.CompletedProcess(cmd, 1, "", "ARM unavailable")
            payload = {"value": permissions_by_scope[scope]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        raise AssertionError(f"unexpected az call: {cmd}")

    return run, calls


SUB_SCOPE = f"/subscriptions/{SUB_ID}"
RG_SCOPE = f"{SUB_SCOPE}/resourceGroups/spi-stack-dev1"


class TestEvaluate:
    def test_an_unanswered_read_is_an_error_not_a_denial(self):
        run, _ = _az({})
        with patch("spi.permissions.run_command", side_effect=run):
            result = permissions.evaluate(SUB_SCOPE, "subscription scope", DEPLOY_ACTIONS)
        assert result.error and result.denied == []

    def test_the_grant_condition_is_reported_verbatim(self):
        run, _ = _az({SUB_SCOPE: [CONTRIBUTOR, CONSTRAINED_GRANT]})
        with patch("spi.permissions.run_command", side_effect=run):
            result = permissions.evaluate(SUB_SCOPE, "subscription scope", DEPLOY_ACTIONS)
        assert result.condition == CONSTRAINED_GRANT["condition"]

    def test_a_grant_inherited_from_the_subscription_is_read_at_group_scope(self):
        run, calls = _az({RG_SCOPE: [CONTRIBUTOR, CONSTRAINED_GRANT]}, group_exists=True)
        with patch("spi.permissions.run_command", side_effect=run):
            result = permissions.resolve_scope(SUB_ID, "spi-stack-dev1")
        assert result.scope == RG_SCOPE
        assert CREATE_GROUP not in result.actions
        assert result.denied == []


class TestGrantText:
    def test_the_condition_names_every_stack_role_in_both_halves(self):
        condition = stack_condition()
        for role_id in STACK_ROLES.values():
            assert condition.count(role_id) == 2

    def test_only_the_missing_grant_is_printed(self):
        only_grant = grant_commands(OID, "User", SUB_ID, [WRITE_ASSIGNMENT, DELETE_ASSIGNMENT])
        assert len(only_grant) == 1
        assert '"Role Based Access Control Administrator"' in only_grant[0]
        assert f"--assignee-object-id {OID}" in only_grant[0]
        assert f"--scope {SUB_SCOPE}" in only_grant[0]

        both = grant_commands(OID, "User", SUB_ID, [CREATE_CLUSTER, WRITE_ASSIGNMENT])
        assert [c.splitlines()[1].strip() for c in both] == [
            '--role "Contributor" \\',
            '--role "Role Based Access Control Administrator" \\',
        ]
        assert grant_commands(OID, "User", SUB_ID, []) == []

    def test_the_condition_is_single_quoted_for_the_shell(self):
        command = grant_commands(OID, "User", SUB_ID, [WRITE_ASSIGNMENT])[0]
        quoted = command.split("--condition ", 1)[1]
        assert quoted.startswith("'((!(") and quoted.endswith("))'")
        assert '"' not in quoted


class TestRoleSetMatchesTheBicep:
    def test_every_role_the_templates_assign_is_in_the_stack_role_set(self):
        """The printed grant must allow every role a template assigns, and no others."""
        guid = re.compile(r"'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'")
        assigned = set()
        for path in INFRA.rglob("*.bicep"):
            text = re.sub(r"//.*", "", path.read_text())
            if "Microsoft.Authorization/roleDefinitions" not in text:
                continue
            assigned |= {g for g in guid.findall(text) if not g.startswith("00000000-")}
        # The cluster-admin grant is made by the CLI, not a template.
        assigned.add(permissions.AKS_RBAC_CLUSTER_ADMIN_ROLE_ID)
        assert assigned == set(STACK_ROLES.values())


class TestCheckCommand:
    def _invoke(self, run, args=("check",)):
        tools = [{"name": "az", "installed": True, "version": "2.70", "description": ""}]
        with (
            patch("spi.checks.run_checks", return_value=tools),
            patch("spi.permissions.run_command", side_effect=run),
            patch("spi.azure_infra._deployer_oid_from_arm_token", return_value=OID),
        ):
            return CliRunner().invoke(app, list(args))

    def test_a_contributor_only_identity_fails_with_the_grant_command(self):
        run, _ = _az({SUB_SCOPE: [CONTRIBUTOR]})
        result = self._invoke(run)
        assert result.exit_code == 1, result.output
        assert "Azure Permissions" in result.output
        assert "2 of 4 permissions missing" in result.output
        assert f"--assignee-object-id {OID}" in result.output
        assert '--role "Contributor"' not in result.output

    def test_signed_out_reports_one_row_and_passes(self):
        def signed_out(cmd, **_kwargs):
            return subprocess.CompletedProcess(cmd, 1, "", "Please run 'az login'")

        result = self._invoke(signed_out)
        assert result.exit_code == 0, result.output
        assert "SKIPPED" in result.output

    def test_an_unanswered_read_warns_and_passes(self):
        run, _ = _az({})
        result = self._invoke(run)
        assert result.exit_code == 0, result.output
        assert "Permissions not checked" in result.output

    def test_json_reports_the_denied_action_and_the_grant(self):
        run, _ = _az({SUB_SCOPE: [CONTRIBUTOR]})
        result = self._invoke(run, ("check", "--json"))
        assert result.exit_code == 1
        azure = json.loads(result.output)["azure"]
        assert azure["ok"] is False
        assert azure["scopes"][0]["actions"][WRITE_ASSIGNMENT] is False
        assert azure["principal"] == {"object_id": OID, "type": "User"}
        assert len(azure["grant_commands"]) == 1

    def test_json_passes_with_the_constrained_grant(self):
        run, _ = _az({SUB_SCOPE: [CONTRIBUTOR, CONSTRAINED_GRANT]})
        result = self._invoke(run, ("check", "--json"))
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["azure"]["grant_commands"] == []


class TestUpGate:
    def _resolve(self, run):
        with (
            patch("spi.azure_infra._get_azure_account", return_value=ACCOUNT),
            patch("spi.azure_infra._resolve_deployer_principal", return_value=(OID, "User")),
            patch("spi.permissions.run_command", side_effect=run),
            patch("spi.cli._resolve_name_suffix", return_value="abcde") as resolve_suffix,
        ):
            try:
                _resolve_up_context("dev1")
            finally:
                self.resolve_suffix = resolve_suffix

    def test_a_denial_stops_before_the_first_mutation(self):
        run, _ = _az({SUB_SCOPE: [CONTRIBUTOR]})
        with pytest.raises(typer.Exit) as exc:
            self._resolve(run)
        assert exc.value.exit_code == 1
        self.resolve_suffix.assert_not_called()

    def test_a_new_group_is_checked_at_subscription_scope(self):
        run, calls = _az({SUB_SCOPE: [CONTRIBUTOR, CONSTRAINED_GRANT]})
        self._resolve(run)
        self.resolve_suffix.assert_called_once()
        rest = [c for c in calls if c[1] == "rest"]
        assert len(rest) == 1 and f"{SUB_SCOPE}/providers/" in rest[0][5]

    def test_an_unanswered_read_does_not_block_the_deploy(self):
        run, _ = _az({})
        self._resolve(run)
        self.resolve_suffix.assert_called_once()
