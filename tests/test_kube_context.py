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

"""Kubeconfig pruning on teardown.

``spi down`` deletes the resource group the cluster lives in, so the
kubeconfig entries ``spi up`` merged in are left pointing at nothing. These
tests cover what gets removed, what is deliberately left alone, and the
degraded paths that must not fail a teardown.
"""

import subprocess
from unittest.mock import patch

from spi.shell import prune_kube_context

DEV1_FQDN = "spi-stack-dev1-a1b2c3d4.hcp.eastus.azmk8s.io"

KUBECONFIG = {
    "contexts": [
        {
            "name": "spi-stack-dev1",
            "context": {"cluster": "spi-stack-dev1", "user": "clusterUser_spi-stack-dev1"},
        },
        {"name": "shared-a", "context": {"cluster": "shared", "user": "shared-user"}},
        {"name": "shared-b", "context": {"cluster": "shared", "user": "shared-user"}},
    ],
    "clusters": [
        {"name": "spi-stack-dev1", "cluster": {"server": f"https://{DEV1_FQDN}:443"}},
        {"name": "shared", "cluster": {"server": "https://shared.example:6443"}},
    ],
}


def _ok() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["kubectl"], 0, "", "")


def _without(view: dict, context: str) -> dict:
    """The merged view kubectl reports once `context` has been deleted."""
    return {**view, "contexts": [c for c in view["contexts"] if c["name"] != context]}


def _views(pre: dict, context: str = "spi-stack-dev1", post: dict | None = None) -> list:
    """The pair of `kubectl config view` reads one prune performs."""
    return [pre, post if post is not None else _without(pre, context)]


class TestPruneKubeContext:
    def test_removes_the_context_cluster_and_user(self):
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", side_effect=_views(KUBECONFIG)),
            patch("spi.shell.run_command", return_value=_ok()) as run_command,
            patch("spi.shell.display_result"),
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        assert [call.args[0] for call in run_command.call_args_list] == [
            ["kubectl", "config", "delete-context", "spi-stack-dev1"],
            ["kubectl", "config", "delete-cluster", "spi-stack-dev1"],
            ["kubectl", "config", "delete-user", "clusterUser_spi-stack-dev1"],
        ]

    def test_keeps_a_cluster_and_user_another_context_still_uses(self):
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", side_effect=_views(KUBECONFIG, "shared-a")),
            patch("spi.shell.run_command", return_value=_ok()) as run_command,
            patch("spi.shell.display_result"),
        ):
            prune_kube_context("shared-a", "shared.example")

        assert [call.args[0] for call in run_command.call_args_list] == [
            ["kubectl", "config", "delete-context", "shared-a"],
        ]

    def test_a_context_that_is_not_there_is_a_no_op(self):
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", return_value=KUBECONFIG),
            patch("spi.shell.run_command") as run_command,
            patch("spi.shell.display_result") as display_result,
        ):
            prune_kube_context("spi-stack-never-deployed", DEV1_FQDN)

        run_command.assert_not_called()
        display_result.assert_not_called()

    def test_an_unreadable_kubeconfig_is_a_no_op(self):
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", return_value=None),
            patch("spi.shell.run_command") as run_command,
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        run_command.assert_not_called()

    def test_every_kubeconfig_change_is_shown_to_the_operator(self):
        """The command panels exist so an operator sees what the CLI changes.
        Reads are silent; nothing that edits the kubeconfig may be."""
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", side_effect=_views(KUBECONFIG)),
            patch("spi.shell.run_command", return_value=_ok()) as run_command,
            patch("spi.shell.display_result"),
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        assert run_command.call_count == 3
        assert all(call.kwargs.get("display", True) is True for call in run_command.call_args_list)

    def test_teardown_without_kubectl_installed_is_a_no_op(self):
        with (
            patch("spi.shell.shutil.which", return_value=None),
            patch("spi.shell.kubectl_json") as kubectl_json,
            patch("spi.shell.run_command") as run_command,
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        kubectl_json.assert_not_called()
        run_command.assert_not_called()

    def test_a_failed_delete_does_not_abort_the_teardown(self):
        failure = subprocess.CompletedProcess(["kubectl"], 1, "", "boom")
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", return_value=KUBECONFIG),
            patch("spi.shell.run_command", return_value=failure) as run_command,
            patch("spi.shell.display_result"),
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        assert all(call.kwargs["check"] is False for call in run_command.call_args_list)

    def test_a_failed_context_delete_keeps_its_cluster_and_user(self):
        """Deleting the backing entries under a context that survived would
        leave the kubeconfig with a dangling reference."""
        failure = subprocess.CompletedProcess(["kubectl"], 1, "", "permission denied")
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", return_value=KUBECONFIG),
            patch("spi.shell.run_command", return_value=failure) as run_command,
            patch("spi.shell.display_result") as display_result,
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        assert [call.args[0] for call in run_command.call_args_list] == [
            ["kubectl", "config", "delete-context", "spi-stack-dev1"],
        ]
        display_result.assert_not_called()


class TestPruneReadsTheKubeconfigAgain:
    """A multi-file KUBECONFIG merges first-wins, and `delete-context` edits
    only the file holding the winner. A shadowed context of the same name can
    surface after the delete, carrying live references with it, so the
    pre-delete view is not a sound basis for what to remove next."""

    SHADOWED = {
        **KUBECONFIG,
        "contexts": [
            {
                "name": "spi-stack-dev1",
                "context": {"cluster": "spi-stack-dev1", "user": "clusterUser_spi-stack-dev1"},
            },
        ],
    }

    def test_a_surfaced_context_keeps_the_cluster_and_user_it_references(self):
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch(
                "spi.shell.kubectl_json",
                side_effect=_views(self.SHADOWED, post=self.SHADOWED),
            ),
            patch("spi.shell.run_command", return_value=_ok()) as run_command,
            patch("spi.shell.display_result"),
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        assert [call.args[0] for call in run_command.call_args_list] == [
            ["kubectl", "config", "delete-context", "spi-stack-dev1"],
        ]

    def test_a_surfaced_context_keeps_the_selection(self):
        active = {**self.SHADOWED, "current-context": "spi-stack-dev1"}
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", side_effect=_views(active, post=active)),
            patch("spi.shell.run_command", return_value=_ok()) as run_command,
            patch("spi.shell.display_result"),
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        verbs = [call.args[0][2] for call in run_command.call_args_list]
        assert "unset" not in verbs

    def test_an_unreadable_second_view_keeps_the_dependent_entries(self):
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", side_effect=[KUBECONFIG, None]),
            patch("spi.shell.run_command", return_value=_ok()) as run_command,
            patch("spi.shell.display_result") as display_result,
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        assert [call.args[0] for call in run_command.call_args_list] == [
            ["kubectl", "config", "delete-context", "spi-stack-dev1"],
        ]
        display_result.assert_not_called()


class TestPruneClearsTheActiveContext:
    """`kubectl config delete-context` removes the entry but leaves
    `current-context` naming it, so kubectl then fails with
    `current-context was not found` instead of the dead-cluster error."""

    ACTIVE = {**KUBECONFIG, "current-context": "spi-stack-dev1"}

    def test_the_deleted_context_is_cleared_when_it_was_active(self):
        deleted = _without(self.ACTIVE, "spi-stack-dev1")
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch(
                "spi.shell.kubectl_json",
                side_effect=[self.ACTIVE, deleted, {**deleted, "current-context": ""}],
            ),
            patch("spi.shell.run_command", return_value=_ok()) as run_command,
            patch("spi.shell.display_result"),
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        assert ["kubectl", "config", "unset", "current-context"] in [
            call.args[0] for call in run_command.call_args_list
        ]

    def test_another_context_stays_selected(self):
        active_elsewhere = {**KUBECONFIG, "current-context": "shared-a"}
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", side_effect=_views(active_elsewhere)),
            patch("spi.shell.run_command", return_value=_ok()) as run_command,
            patch("spi.shell.display_result"),
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        verbs = [call.args[0][2] for call in run_command.call_args_list]
        assert "unset" not in verbs

    def test_a_selection_named_in_two_files_is_cleared_from_both(self):
        """`unset` writes to the file holding the winning value. A second file
        naming the deleted context surfaces its own copy, which still selects
        an entry that is gone."""
        deleted = _without(self.ACTIVE, "spi-stack-dev1")
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch(
                "spi.shell.kubectl_json",
                # The second file's identical value surfaces after the first unset.
                side_effect=[self.ACTIVE, deleted, deleted, {**deleted, "current-context": ""}],
            ),
            patch("spi.shell.run_command", return_value=_ok()) as run_command,
            patch("spi.shell.display_result"),
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        unsets = [call.args[0] for call in run_command.call_args_list if call.args[0][2] == "unset"]
        assert len(unsets) == 2

    def test_a_selection_that_will_not_clear_stops_and_warns(self):
        deleted = _without(self.ACTIVE, "spi-stack-dev1")
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            # Every re-read still names the deleted context, as if another
            # file keeps supplying it.
            patch("spi.shell.kubectl_json", side_effect=[self.ACTIVE] + [deleted] * 9),
            patch("spi.shell.run_command", return_value=_ok()) as run_command,
            patch("spi.shell.display_result") as display_result,
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        unsets = [c for c in run_command.call_args_list if c.args[0][2] == "unset"]
        assert len(unsets) == 8
        display_result.assert_not_called()

    def test_a_failed_unset_stops_before_the_dependent_entries(self):
        deleted = _without(self.ACTIVE, "spi-stack-dev1")
        failure = subprocess.CompletedProcess(["kubectl"], 1, "", "read-only")

        def outcome(cmd, **kwargs):
            return failure if cmd[2] == "unset" else _ok()

        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", side_effect=[self.ACTIVE, deleted]),
            patch("spi.shell.run_command", side_effect=outcome) as run_command,
            patch("spi.shell.display_result") as display_result,
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        verbs = [call.args[0][2] for call in run_command.call_args_list]
        assert verbs == ["delete-context", "unset"]
        display_result.assert_not_called()

    def test_a_failed_context_delete_leaves_the_selection_alone(self):
        failure = subprocess.CompletedProcess(["kubectl"], 1, "", "read-only")
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", return_value=self.ACTIVE),
            patch("spi.shell.run_command", return_value=failure) as run_command,
            patch("spi.shell.display_result"),
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        assert run_command.call_count == 1


class TestPruneChecksClusterIdentity:
    """`spi up --env dev1` in two subscriptions builds two `spi-stack-dev1`
    clusters, so both write the same context name. Tearing one down must not
    take the other one's credentials with it."""

    def test_prunes_when_the_context_serves_the_deleted_cluster(self):
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", side_effect=_views(KUBECONFIG)),
            patch("spi.shell.run_command", return_value=_ok()) as run_command,
            patch("spi.shell.display_result"),
        ):
            prune_kube_context("spi-stack-dev1", server_fqdn=DEV1_FQDN)

        assert [call.args[0][2] for call in run_command.call_args_list] == [
            "delete-context",
            "delete-cluster",
            "delete-user",
        ]

    def test_leaves_a_same_named_context_for_another_subscription(self):
        other = "spi-stack-dev1-99887766.hcp.westus.azmk8s.io"
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", return_value=KUBECONFIG),
            patch("spi.shell.run_command") as run_command,
            patch("spi.shell.display_result") as display_result,
        ):
            prune_kube_context("spi-stack-dev1", server_fqdn=other)

        run_command.assert_not_called()
        display_result.assert_not_called()

    def test_a_server_that_merely_contains_the_fqdn_is_not_a_match(self):
        """`api.azmk8s.io` occurs inside `api.azmk8s.io.example.invalid`, so a
        substring test would clear a context pointing at another host."""
        lookalike = {
            "contexts": KUBECONFIG["contexts"],
            "clusters": [
                {
                    "name": "spi-stack-dev1",
                    "cluster": {"server": f"https://{DEV1_FQDN}.example.invalid"},
                }
            ],
        }
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", return_value=lookalike),
            patch("spi.shell.run_command") as run_command,
            patch("spi.shell.display_result"),
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        run_command.assert_not_called()

    def test_a_server_the_url_parser_rejects_is_not_a_match(self):
        """`urlsplit` raises on an unmatched IPv6 bracket. The value comes from
        the operator's kubeconfig, and the resource group is already deleted by
        the time this runs, so raising would abort a successful teardown."""
        malformed = {
            "contexts": KUBECONFIG["contexts"],
            "clusters": [{"name": "spi-stack-dev1", "cluster": {"server": "https://[::1:443"}}],
        }
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", return_value=malformed),
            patch("spi.shell.run_command") as run_command,
            patch("spi.shell.display_result"),
        ):
            prune_kube_context("spi-stack-dev1", DEV1_FQDN)

        run_command.assert_not_called()

    def test_an_unknown_api_server_leaves_the_kubeconfig_alone(self):
        """A resource group whose AKS creation failed, or a cluster already
        deleted out of band, cannot prove the context is its own. Guessing
        would cost a live same-named cluster its credentials."""
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", return_value=KUBECONFIG),
            patch("spi.shell.run_command") as run_command,
            patch("spi.shell.display_result") as display_result,
        ):
            prune_kube_context("spi-stack-dev1", server_fqdn="")

        run_command.assert_not_called()
        display_result.assert_not_called()


class TestCleanupPrunesTheContext:
    """The pruning has to be wired into teardown, not just available."""

    def test_down_prunes_the_context_named_after_the_cluster(self):
        from spi.config import Config
        from spi.deploy import cleanup_azure

        # `az aks show --query 'privateFqdn || fqdn'`, then `az group delete`,
        # then the `az group exists` poll that ends the wait.
        responses = [
            subprocess.CompletedProcess(["az"], 0, f"{DEV1_FQDN}\n", ""),
            subprocess.CompletedProcess(["az"], 0, "", ""),
            subprocess.CompletedProcess(["az"], 0, "false", ""),
        ]
        with (
            patch("spi.deploy.run_command", side_effect=responses),
            patch("spi.deploy.prune_kube_context") as prune,
            patch("spi.deploy.display_result"),
        ):
            cleanup_azure(Config.from_env("dev1"))

        prune.assert_called_once_with("spi-stack-dev1", server_fqdn=DEV1_FQDN)

    def test_the_api_server_is_read_before_the_group_is_deleted(self):
        """`az aks show` against a deleted resource group returns nothing."""
        from spi.config import Config
        from spi.deploy import cleanup_azure

        responses = [
            subprocess.CompletedProcess(["az"], 0, f"{DEV1_FQDN}\n", ""),
            subprocess.CompletedProcess(["az"], 0, "", ""),
            subprocess.CompletedProcess(["az"], 0, "false", ""),
        ]
        with (
            patch("spi.deploy.run_command", side_effect=responses) as run_command,
            patch("spi.deploy.prune_kube_context"),
            patch("spi.deploy.display_result"),
        ):
            cleanup_azure(Config.from_env("dev1"))

        verbs = [call.args[0][:3] for call in run_command.call_args_list]
        assert verbs[0] == ["az", "aks", "show"]
        assert "privateFqdn || fqdn" in run_command.call_args_list[0].args[0]
        assert verbs[1] == ["az", "group", "delete"]

    def test_an_unconfirmed_deletion_leaves_the_context_in_place(self):
        """`az group delete --no-wait` returns on acceptance. An accepted
        delete can still fail, and the cluster would survive with it."""
        from spi.config import Config
        from spi.deploy import cleanup_azure

        responses = [
            subprocess.CompletedProcess(["az"], 0, f"{DEV1_FQDN}\n", ""),
            subprocess.CompletedProcess(["az"], 0, "", ""),
            subprocess.CompletedProcess(["az"], 0, "true", ""),
        ]
        with (
            patch("spi.deploy.run_command", side_effect=responses),
            patch("spi.deploy.prune_kube_context") as prune,
            patch("spi.deploy.display_result"),
            patch("spi.deploy.time.sleep"),
            patch("spi.deploy.time.time", side_effect=[0, 0, 100]),
        ):
            cleanup_azure(Config.from_env("dev1"))

        prune.assert_not_called()

    def test_an_unreadable_api_server_reaches_the_prune_as_empty(self):
        from spi.config import Config
        from spi.deploy import cleanup_azure

        responses = [
            subprocess.CompletedProcess(["az"], 1, "", "ResourceNotFound"),
            subprocess.CompletedProcess(["az"], 0, "", ""),
            subprocess.CompletedProcess(["az"], 0, "false", ""),
        ]
        with (
            patch("spi.deploy.run_command", side_effect=responses),
            patch("spi.deploy.prune_kube_context") as prune,
            patch("spi.deploy.display_result"),
        ):
            cleanup_azure(Config.from_env("dev1"))

        prune.assert_called_once_with("spi-stack-dev1", server_fqdn="")

    def test_a_rejected_delete_leaves_the_context_alone(self):
        import typer

        from spi.config import Config
        from spi.deploy import cleanup_azure

        responses = [
            subprocess.CompletedProcess(["az"], 0, f"{DEV1_FQDN}\n", ""),
            subprocess.CompletedProcess(["az"], 1, "", "AuthorizationFailed"),
        ]
        with (
            patch("spi.deploy.run_command", side_effect=responses),
            patch("spi.deploy.prune_kube_context") as prune,
        ):
            try:
                cleanup_azure(Config.from_env("dev1"))
            except typer.Exit:
                pass

        prune.assert_not_called()
