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

"""Self-update support for the spi CLI.

End users install via:
    uv tool install <wheel-url-from-github-release>

This module checks GitHub Releases for a newer version and re-runs the
installer with a wheel URL when that can safely upgrade the active install.
Native Windows `uv` installs are refused with a manual recovery command instead.
The canonical install path is a wheel-asset URL so the recorded version metadata
is correct; `git+...@vX.Y.Z` is documented as a developer fallback only.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal, Optional

from packaging.version import InvalidVersion, Version

from .shell import run_command

GITHUB_OWNER = "Azure"
GITHUB_REPO = "osdu-spi-stack"
GITHUB_API_BASE = "https://api.github.com"
RELEASES_LATEST = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_LIST = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases?per_page=30"

Installer = Literal["uv", "pipx"]


class UpdateError(Exception):
    """Raised when an update operation fails before the upgrade subprocess."""


def _require_https(url: str) -> None:
    """Reject URLs whose scheme is not HTTP(S).

    `urllib.request.urlopen` accepts `file://`, `ftp://`, and other schemes.
    Lock to HTTP(S) since the URLs here are all built from module constants
    or extracted from the trusted GitHub API response.
    """
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise UpdateError(f"refused to open URL with non-HTTP(S) scheme: {scheme!r}")


def _github_headers(token: Optional[str] = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "spi-update-cli",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_get_json(url: str, token: Optional[str] = None, timeout: int = 10):
    _require_https(url)
    req = urllib.request.Request(url, headers=_github_headers(token))
    try:
        # URL scheme validated above; URL is a constant or extracted from a
        # GitHub API JSON response that we control.
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise UpdateError(f"GitHub API request failed ({url}): {exc}") from exc


def fetch_latest_release(token: Optional[str] = None, timeout: int = 10) -> dict:
    """Fetch the latest GitHub Release JSON for the project."""
    return _github_get_json(RELEASES_LATEST, token=token, timeout=timeout)


def parse_version_from_release(release: dict) -> Version:
    """Extract the semver from a release's `tag_name` (strips leading 'v')."""
    tag = (release.get("tag_name") or "").lstrip("v")
    if not tag:
        raise UpdateError("release JSON missing tag_name")
    try:
        return Version(tag)
    except InvalidVersion as exc:
        raise UpdateError(f"release tag is not a valid semver: {tag!r}") from exc


def find_wheel_asset_url(release: dict) -> str:
    """Return the `browser_download_url` for the py3-none-any wheel asset."""
    assets = release.get("assets") or []
    for asset in assets:
        name = (asset.get("name") or "").lower()
        if name.endswith("-py3-none-any.whl") and name.startswith("spi-"):
            url = asset.get("browser_download_url")
            if isinstance(url, str) and url:
                return url
    raise UpdateError(f"release {release.get('tag_name')!r} has no spi-*-py3-none-any.whl asset")


def _running_spi_dir() -> Optional[Path]:
    """Return the resolved directory containing the running spi package."""
    from . import __file__ as spi_init

    if not spi_init:
        return None
    try:
        return Path(spi_init).resolve().parent
    except OSError:
        return None


def _is_descendant(child: Path, ancestor: Path) -> bool:
    try:
        child.resolve().relative_to(ancestor.resolve())
    except (OSError, ValueError):
        return False
    return True


def _uv_tool_spi_dir() -> Optional[Path]:
    try:
        result = run_command(["uv", "tool", "dir"], display=False, check=False)
    except FileNotFoundError:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    base = result.stdout.strip()
    return Path(base) / "spi" if base else None


def _pipx_spi_dir() -> Optional[Path]:
    try:
        result = run_command(
            ["pipx", "environment", "--value", "PIPX_LOCAL_VENVS"],
            display=False,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    base = result.stdout.strip()
    return Path(base) / "spi" if base else None


def detect_installer() -> Optional[Installer]:
    """Return the installer that manages the *currently running* spi, if any.

    Path-based check: we ask each installer for its managed directory and
    only return a hit when the running `spi/__init__.py` lives under that
    directory. Guards against the case where a developer runs `uv run spi
    update` from a source clone on a machine that also has a separate
    `uv tool install spi` — stdout-based detection would happily upgrade
    the unrelated global install.
    """
    running = _running_spi_dir()
    if running is None:
        return None

    uv_dir = _uv_tool_spi_dir()
    if uv_dir and _is_descendant(running, uv_dir):
        return "uv"

    pipx_dir = _pipx_spi_dir()
    if pipx_dir and _is_descendant(running, pipx_dir):
        return "pipx"

    return None


def resolve_github_token(override: Optional[str]) -> Optional[str]:
    """Resolve a GitHub token from an explicit override, env vars, or gh CLI.

    Public-repo Releases API works unauthenticated (60 req/hr/IP); a token
    raises that to 5000 req/hr and is required only on rate-limited shared
    runners. Returns None when no token is available; callers should treat
    that as "fall back to anonymous behavior."
    """
    if override:
        return override
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        env = os.environ.get(var)
        if env:
            return env
    try:
        result = run_command(["gh", "auth", "token"], display=False, check=False)
    except FileNotFoundError:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    token = result.stdout.strip()
    return token or None


def fetch_release_notes(
    current: Version, latest: Version, token: Optional[str] = None
) -> Optional[str]:
    """Concatenate GitHub Release bodies for tags in (current, latest].

    Returns None on any HTTP/JSON error so the caller can render a friendly
    fallback rather than abort the upgrade.
    """
    try:
        payload = _github_get_json(RELEASES_LIST, token=token)
    except UpdateError:
        return None

    if not isinstance(payload, list):
        return None

    sections: list[tuple[Version, str]] = []
    for release in payload:
        if not isinstance(release, dict):
            continue
        tag = (release.get("tag_name") or "").lstrip("v")
        body = release.get("body") or ""
        try:
            v = Version(tag)
        except InvalidVersion:
            continue
        if v <= current or v > latest:
            continue
        sections.append((v, body.strip()))

    if not sections:
        return None

    sections.sort(key=lambda pair: pair[0])
    parts = [f"## v{v}\n\n{body}" if body else f"## v{v}" for v, body in sections]
    return "\n\n".join(parts)


def installed_version() -> Optional[Version]:
    """Return the spi version read from on-disk distribution metadata.

    Reads dist-info fresh on each call so it reflects what the upgrade
    subprocess just wrote. The module-level `__version__` is captured at
    import time and cannot be used to verify a post-upgrade state from the
    same process.
    """
    try:
        return Version(importlib.metadata.version("spi"))
    except (importlib.metadata.PackageNotFoundError, InvalidVersion):
        return None


def run_upgrade(
    installer: Installer,
    wheel_url: str,
    *,
    display: bool = True,
) -> int:
    """Re-install spi from a GitHub Release wheel asset URL.

    Both uv and pipx accept a direct URL to a wheel as the install spec. Native
    Windows uv installs raise UpdateError with a manual recovery command because
    replacing the active tool environment can orphan the launcher. Returns the
    subprocess exit code; run_command prints stderr on failure.
    """
    _require_https(wheel_url)
    if installer == "uv":
        if platform.system() == "Windows":
            raise UpdateError(
                "uv self-update is disabled on Windows because replacing the "
                "active tool environment can orphan the spi launcher. Run this "
                "from a new terminal instead:\n"
                f"uv tool install --force {wheel_url}"
            )
        cmd = ["uv", "tool", "install", "--force", wheel_url]
    else:
        cmd = ["pipx", "install", "--force", wheel_url]
    result = run_command(cmd, description="Upgrade spi", display=display, check=False)
    return result.returncode
