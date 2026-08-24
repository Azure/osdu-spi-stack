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

"""Tests for the self-update logic in src/spi/update.py.

These tests exercise the parsing and URL-construction paths. The
network calls (urllib.request.urlopen) and subprocess calls (uv tool
install) are mocked; this is a unit test, not an integration test.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest
from packaging.version import Version

from spi import update as upd

RELEASE_FIXTURE = {
    "tag_name": "v0.2.1",
    "name": "Release v0.2.1",
    "body": "## Features\n- new thing",
    "assets": [
        {
            "name": "spi-0.2.1-py3-none-any.whl",
            "browser_download_url": "https://github.com/Azure/osdu-spi-stack/releases/download/v0.2.1/spi-0.2.1-py3-none-any.whl",
        },
        {
            "name": "spi-0.2.1.tar.gz",
            "browser_download_url": "https://github.com/Azure/osdu-spi-stack/releases/download/v0.2.1/spi-0.2.1.tar.gz",
        },
    ],
}


def _mock_urlopen(payload):
    """Return a context-manager-compatible mock that yields `payload` as JSON."""
    raw = json.dumps(payload).encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value = io.BytesIO(raw)
    cm.__exit__.return_value = False
    return cm


def test_parse_version_strips_v_prefix():
    assert upd.parse_version_from_release({"tag_name": "v0.2.1"}) == Version("0.2.1")
    assert upd.parse_version_from_release({"tag_name": "0.2.1"}) == Version("0.2.1")


def test_parse_version_rejects_invalid():
    with pytest.raises(upd.UpdateError, match="not a valid semver"):
        upd.parse_version_from_release({"tag_name": "release-1"})
    with pytest.raises(upd.UpdateError, match="missing tag_name"):
        upd.parse_version_from_release({})


def test_find_wheel_asset_url_picks_wheel():
    url = upd.find_wheel_asset_url(RELEASE_FIXTURE)
    assert url.endswith("spi-0.2.1-py3-none-any.whl")


def test_find_wheel_asset_url_errors_when_no_wheel():
    release = {"tag_name": "v0.2.1", "assets": [{"name": "spi-0.2.1.tar.gz"}]}
    with pytest.raises(upd.UpdateError, match="no spi-\\*-py3-none-any.whl asset"):
        upd.find_wheel_asset_url(release)


def test_fetch_latest_release_hits_github_api():
    with patch("spi.update.urllib.request.urlopen") as urlopen:
        urlopen.return_value = _mock_urlopen(RELEASE_FIXTURE)
        result = upd.fetch_latest_release()
    assert result["tag_name"] == "v0.2.1"
    args, _kwargs = urlopen.call_args
    req = args[0]
    assert req.full_url == upd.RELEASES_LATEST
    assert req.headers["Accept"] == "application/vnd.github+json"


def test_fetch_latest_release_sends_auth_when_token():
    with patch("spi.update.urllib.request.urlopen") as urlopen:
        urlopen.return_value = _mock_urlopen(RELEASE_FIXTURE)
        upd.fetch_latest_release(token="ghp_fake")
    req = urlopen.call_args.args[0]
    assert req.headers["Authorization"] == "Bearer ghp_fake"


def test_require_https_blocks_file_scheme():
    with pytest.raises(upd.UpdateError, match="non-HTTP"):
        upd._require_https("file:///etc/passwd")


def test_fetch_release_notes_filters_by_version_range():
    payload = [
        {"tag_name": "v0.3.0", "body": "future"},
        {"tag_name": "v0.2.1", "body": "fixes"},
        {"tag_name": "v0.2.0", "body": "current"},
        {"tag_name": "v0.1.9", "body": "older"},
    ]
    with patch("spi.update.urllib.request.urlopen") as urlopen:
        urlopen.return_value = _mock_urlopen(payload)
        notes = upd.fetch_release_notes(Version("0.2.0"), Version("0.2.1"))
    assert notes is not None
    assert "## v0.2.1" in notes
    assert "fixes" in notes
    assert "current" not in notes
    assert "future" not in notes
    assert "older" not in notes


def test_fetch_release_notes_returns_none_when_empty_range():
    payload = [{"tag_name": "v0.1.0", "body": "old"}]
    with patch("spi.update.urllib.request.urlopen") as urlopen:
        urlopen.return_value = _mock_urlopen(payload)
        notes = upd.fetch_release_notes(Version("0.2.0"), Version("0.2.1"))
    assert notes is None


def test_fetch_release_notes_returns_none_on_http_error():
    with patch("spi.update.urllib.request.urlopen") as urlopen:
        urlopen.side_effect = TimeoutError("timed out")
        notes = upd.fetch_release_notes(Version("0.2.0"), Version("0.2.1"))
    assert notes is None


def test_run_upgrade_uv_uses_force_install():
    completed = MagicMock(returncode=0)
    wheel_url = "https://github.com/x/y/releases/download/v1.0.0/spi-1.0.0-py3-none-any.whl"
    with (
        patch("spi.update.platform.system", return_value="Linux"),
        patch("spi.update.run_command", return_value=completed) as rc,
    ):
        rv = upd.run_upgrade("uv", wheel_url, display=False)
    assert rv == 0
    cmd = rc.call_args.args[0]
    assert cmd == ["uv", "tool", "install", "--force", wheel_url]


def test_run_upgrade_uv_blocks_in_process_windows_replacement():
    wheel_url = "https://github.com/x/y/releases/download/v1.0.0/spi-1.0.0-py3-none-any.whl"
    with (
        patch("spi.update.platform.system", return_value="Windows"),
        patch("spi.update.run_command") as rc,
        pytest.raises(upd.UpdateError, match="disabled on Windows"),
    ):
        upd.run_upgrade("uv", wheel_url, display=False)
    rc.assert_not_called()


def test_run_upgrade_pipx_uses_force_install():
    completed = MagicMock(returncode=0)
    wheel_url = "https://github.com/x/y/releases/download/v1.0.0/spi-1.0.0-py3-none-any.whl"
    with patch("spi.update.run_command", return_value=completed) as rc:
        upd.run_upgrade("pipx", wheel_url, display=False)
    cmd = rc.call_args.args[0]
    assert cmd == ["pipx", "install", "--force", wheel_url]


def test_run_upgrade_rejects_non_https_wheel_url():
    with pytest.raises(upd.UpdateError, match="non-HTTP"):
        upd.run_upgrade("uv", "file:///tmp/spi.whl", display=False)


def test_resolve_github_token_prefers_override():
    assert upd.resolve_github_token("explicit") == "explicit"


def test_resolve_github_token_reads_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    # Block the gh-cli fallback so this test stays hermetic.
    with patch("spi.update.run_command", side_effect=FileNotFoundError()):
        assert upd.resolve_github_token(None) == "from-env"
