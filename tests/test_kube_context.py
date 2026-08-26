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

KUBECONFIG = {
    "contexts": [
        {
            "name": "spi-stack-dev1",
            "context": {"cluster": "spi-stack-dev1", "user": "clusterUser_spi-stack-dev1"},
        },
        {"name": "shared-a", "context": {"cluster": "shared", "user": "shared-user"}},
        {"name": "shared-b", "context": {"cluster": "shared", "user": "shared-user"}},
    ]
}


def _ok() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["kubectl"], 0, "", "")


class TestPruneKubeContext:
    def test_removes_the_context_cluster_and_user(self):
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", return_value=KUBECONFIG),
            patch("spi.shell.run_command", return_value=_ok()) as run_command,
            patch("spi.shell.display_result"),
        ):
            prune_kube_context("spi-stack-dev1")

        assert [call.args[0] for call in run_command.call_args_list] == [
            ["kubectl", "config", "delete-context", "spi-stack-dev1"],
            ["kubectl", "config", "delete-cluster", "spi-stack-dev1"],
            ["kubectl", "config", "delete-user", "clusterUser_spi-stack-dev1"],
        ]

    def test_keeps_a_cluster_and_user_another_context_still_uses(self):
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", return_value=KUBECONFIG),
            patch("spi.shell.run_command", return_value=_ok()) as run_command,
            patch("spi.shell.display_result"),
        ):
            prune_kube_context("shared-a")

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
            prune_kube_context("spi-stack-never-deployed")

        run_command.assert_not_called()
        display_result.assert_not_called()

    def test_an_unreadable_kubeconfig_is_a_no_op(self):
        with (
            patch("spi.shell.shutil.which", return_value="/usr/bin/kubectl"),
            patch("spi.shell.kubectl_json", return_value=None),
            patch("spi.shell.run_command") as run_command,
        ):
            prune_kube_context("spi-stack-dev1")

        run_command.assert_not_called()

    def test_teardown_without_kubectl_installed_is_a_no_op(self):
        with (
            patch("spi.shell.shutil.which", return_value=None),
            patch("spi.shell.kubectl_json") as kubectl_json,
            patch("spi.shell.run_command") as run_command,
        ):
            prune_kube_context("spi-stack-dev1")

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
            prune_kube_context("spi-stack-dev1")

        assert all(call.kwargs["check"] is False for call in run_command.call_args_list)


class TestCleanupPrunesTheContext:
    """The pruning has to be wired into teardown, not just available."""

    def test_down_prunes_the_context_named_after_the_cluster(self):
        from spi.config import Config
        from spi.deploy import cleanup_azure

        gone = subprocess.CompletedProcess(["az"], 0, "false", "")
        with (
            patch("spi.deploy.run_command", return_value=gone),
            patch("spi.deploy.prune_kube_context") as prune,
            patch("spi.deploy.display_result"),
        ):
            cleanup_azure(Config.from_env("dev1"))

        prune.assert_called_once_with("spi-stack-dev1")

    def test_a_rejected_delete_leaves_the_context_alone(self):
        import typer

        from spi.config import Config
        from spi.deploy import cleanup_azure

        rejected = subprocess.CompletedProcess(["az"], 1, "", "AuthorizationFailed")
        with (
            patch("spi.deploy.run_command", return_value=rejected),
            patch("spi.deploy.prune_kube_context") as prune,
        ):
            try:
                cleanup_azure(Config.from_env("dev1"))
            except typer.Exit:
                pass

        prune.assert_not_called()
