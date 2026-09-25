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

"""`spi down` asks before deleting anything; `--force` skips the question."""

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from spi import cli


@dataclass
class DownRun:
    exit_code: int
    output: str
    teardown: MagicMock
    purge: MagicMock


def _down(*args: str, tty: bool = True, answer: str = "") -> DownRun:
    with (
        patch("spi.cli.check_prerequisites"),
        patch("spi.cli._resolve_name_suffix", return_value=""),
        patch("spi.cli._stdin_is_tty", return_value=tty),
        patch("spi.teardown.teardown_environment", return_value=[]) as teardown,
        patch("spi.teardown.purge_environment") as purge,
    ):
        result = CliRunner().invoke(cli.app, ["down", "--env", "dev1", *args], input=answer)
    # Rich wraps at the terminal width; collapse whitespace so phrases match.
    return DownRun(result.exit_code, " ".join(result.output.split()), teardown, purge)


class TestInteractive:
    @pytest.mark.parametrize("answer", ["y\n", "Y\n"])
    def test_y_proceeds(self, answer):
        run = _down(answer=answer)
        assert run.exit_code == 0, run.output
        run.teardown.assert_called_once()
        run.purge.assert_not_called()

    @pytest.mark.parametrize("answer", ["n\n", "\n", "yes\n"])
    def test_anything_else_cancels(self, answer):
        run = _down(answer=answer)
        assert run.exit_code == 1
        assert "Nothing was deleted" in run.output
        run.teardown.assert_not_called()

    def test_prompt_follows_the_config_table(self):
        run = _down(answer="n\n")
        assert run.output.index("Resource Group") < run.output.index("Type 'y' to confirm")

    def test_prompt_says_identities_are_kept(self):
        run = _down(answer="n\n")
        assert "spi-stack-dev1" in run.output
        assert "managed identities and the group are kept" in run.output

    def test_purge_prompt_names_the_identities(self):
        run = _down("--purge", answer="y\n")
        assert "resource group 'spi-stack-dev1', including its managed identities" in run.output
        run.purge.assert_called_once()
        run.teardown.assert_not_called()


class TestNonInteractive:
    def test_without_force_fails_before_deleting(self):
        run = _down(tty=False)
        assert run.exit_code == 1
        assert "pass --force when running non-interactively" in run.output
        run.teardown.assert_not_called()
        run.purge.assert_not_called()

    @pytest.mark.parametrize("flag", ["--force", "-f"])
    def test_force_skips_the_prompt(self, flag):
        run = _down(flag, tty=False)
        assert run.exit_code == 0, run.output
        assert "Type 'y' to confirm" not in run.output
        run.teardown.assert_called_once()

    def test_force_with_purge(self):
        run = _down("--purge", "--force", tty=False)
        assert run.exit_code == 0, run.output
        run.purge.assert_called_once()
