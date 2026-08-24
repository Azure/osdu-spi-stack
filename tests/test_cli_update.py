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

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from spi import cli
from spi import update as upd


def test_update_reports_run_upgrade_error():
    runner = CliRunner()
    release = {"tag_name": "v0.2.1", "assets": []}
    wheel_url = "https://github.com/Azure/osdu-spi-stack/releases/download/v0.2.1/spi-0.2.1-py3-none-any.whl"

    with (
        patch("spi.cli.__version__", "0.2.0"),
        patch("spi.update.detect_installer", return_value="uv"),
        patch("spi.update.resolve_github_token", return_value=None),
        patch("spi.update.fetch_latest_release", return_value=release),
        patch("spi.update.fetch_release_notes", return_value=None),
        patch("spi.update.find_wheel_asset_url", return_value=wheel_url),
        patch(
            "spi.update.run_upgrade", side_effect=upd.UpdateError("upgrade refused")
        ) as run_upgrade,
    ):
        result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "upgrade refused" in result.output
    run_upgrade.assert_called_once_with("uv", wheel_url, display=True)


def test_update_silent_reports_run_upgrade_error_on_stderr():
    runner = CliRunner()
    release = {"tag_name": "v0.2.1", "assets": []}
    wheel_url = "https://github.com/Azure/osdu-spi-stack/releases/download/v0.2.1/spi-0.2.1-py3-none-any.whl"

    with (
        patch("spi.cli.__version__", "0.2.0"),
        patch("spi.update.detect_installer", return_value="uv"),
        patch("spi.update.resolve_github_token", return_value=None),
        patch("spi.update.fetch_latest_release", return_value=release),
        patch("spi.update.fetch_release_notes", return_value=None),
        patch("spi.update.find_wheel_asset_url", return_value=wheel_url),
        patch(
            "spi.update.run_upgrade", side_effect=upd.UpdateError("upgrade refused")
        ) as run_upgrade,
    ):
        result = runner.invoke(cli.app, ["update", "--silent"])

    assert result.exit_code == 1
    assert result.output == "upgrade refused\n"
    run_upgrade.assert_called_once_with("uv", wheel_url, display=False)
