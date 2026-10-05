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

Checks GitHub Releases for a newer version and re-runs the installer with
the release's wheel URL. Native Windows `uv` installs hand the reinstall to a
detached helper that runs once spi has exited.
"""

from __future__ import annotations

import importlib.metadata
import json
import ntpath
import os
import platform
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal, Optional

from packaging.version import InvalidVersion, Version

from .shell import display_command, prepare_command, run_command

GITHUB_OWNER = "Azure"
GITHUB_REPO = "osdu-spi-stack"
GITHUB_API_BASE = "https://api.github.com"
RELEASES_LATEST = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_LIST = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases?per_page=30"
MICROSOFT_PYPI_PROXY = "https://packagefeedproxy.microsoft.io/pypi/simple/"
UPDATE_LOG_NAME = "spi-update.log"
HELPER_WAIT_SECONDS = 120

# Win32 process creation flags, spelled out because `subprocess` only defines
# them on Windows and the tests run everywhere.
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000

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
        # Scheme validated above; the URL is a constant or a GitHub API value.
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


def _uv_tool_dir(*flags: str) -> Optional[Path]:
    try:
        result = run_command(["uv", "tool", "dir", *flags], display=False, check=False)
    except FileNotFoundError:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    base = result.stdout.strip()
    return Path(base) if base else None


def _uv_tool_spi_dir() -> Optional[Path]:
    base = _uv_tool_dir()
    return base / "spi" if base else None


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
    """Return the installer that manages the currently running spi, if any.

    Matches on the directory the running `spi/__init__.py` lives under, so
    `uv run spi update` from a source clone never upgrades an unrelated
    `uv tool install` on the same machine.
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


def defers_upgrade(installer: Installer) -> bool:
    """Return True when the upgrade must wait until this process has exited.

    On native Windows the running `spi.exe` launcher and the files the tool
    environment has loaded are locked, so `uv tool install --force` run from
    inside spi deletes part of the environment and orphans the launcher.
    """
    return installer == "uv" and platform.system() == "Windows"


def _uv_install_command(wheel_url: str) -> list[str]:
    return [
        "uv",
        "tool",
        "install",
        "--force",
        "--default-index",
        MICROSOFT_PYPI_PROXY,
        wheel_url,
    ]


def manual_upgrade_command(wheel_url: str) -> str:
    """The upgrade command an operator can run from a new terminal."""
    return " ".join(_uv_install_command(wheel_url))


def run_upgrade(
    installer: Installer,
    wheel_url: str,
    *,
    display: bool = True,
) -> int:
    """Re-install spi from a GitHub Release wheel asset URL.

    Both uv and pipx accept a direct URL to a wheel as the install spec.
    Installs where `defers_upgrade` is true raise UpdateError without running
    anything; use `schedule_upgrade` for those. Returns the subprocess exit
    code; run_command prints stderr on failure.
    """
    _require_https(wheel_url)
    if defers_upgrade(installer):
        raise UpdateError(
            "uv cannot replace the running spi on Windows. Run this from a new terminal "
            f"instead:\n{manual_upgrade_command(wheel_url)}"
        )
    if installer == "uv":
        cmd = _uv_install_command(wheel_url)
    else:
        cmd = ["pipx", "install", "--force", wheel_url]
    result = run_command(cmd, description="Upgrade spi", display=display, check=False)
    return result.returncode


def _ps_literal(value: str) -> str:
    # PowerShell ends a single-quoted string at any of these quote characters;
    # doubling one makes it literal.
    quotes = "'\u2018\u2019\u201a\u201b"
    return "'" + "".join(ch * 2 if ch in quotes else ch for ch in value) + "'"


def build_helper_script(
    *,
    uv: str,
    wheel_url: str,
    tool_dir: Path,
    launcher: Path,
    pid: int,
    wait_seconds: int = HELPER_WAIT_SECONDS,
) -> str:
    """PowerShell that waits for every spi process to exit, then reinstalls spi.

    The tool's `python.exe` is a venv launcher whose child, the base
    interpreter, holds the tool's packages open, so the wait matches `pid`
    (that interpreter) plus anything running from the tool directory or the
    `spi.exe` launcher. That also covers other spi commands still running.
    The script uses no double quotes, so it survives the Windows command line
    as a single argument.
    """
    install = " ".join(_ps_literal(arg) for arg in _uv_install_command(wheel_url)[1:])
    manual = _ps_literal(manual_upgrade_command(wheel_url))
    return f"""\
$deadline = (Get-Date).AddSeconds({wait_seconds})
$toolDir = {_ps_literal(str(tool_dir))} + '\\'
$launcher = {_ps_literal(str(launcher))}
Write-Output ((Get-Date -Format s) + ' waiting for spi to exit')
while ($true) {{
    $busy = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {{
        $_.Id -eq {pid} -or ($_.Path -and ($_.Path -eq $launcher -or
            $_.Path.StartsWith($toolDir, [StringComparison]::OrdinalIgnoreCase)))
    }})
    if ($busy.Count -eq 0) {{ break }}
    if ((Get-Date) -gt $deadline) {{
        Write-Output ('spi is still running (pid ' + ($busy.Id -join ', ') + '); update not applied.')
        Write-Output 'Close every spi command, then run this from a new terminal:'
        Write-Output {manual}
        exit 1
    }}
    Start-Sleep -Milliseconds 500
}}
Write-Output ((Get-Date -Format s) + ' running: ' + {manual})
& {_ps_literal(uv)} {install}
$code = $LASTEXITCODE
Write-Output ((Get-Date -Format s) + ' uv exited with code ' + $code)
exit $code
"""


def _powershell_exe() -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    if not ntpath.isabs(root):
        root = r"C:\Windows"
    return ntpath.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def schedule_upgrade(wheel_url: str, *, display: bool = True) -> Path:
    """Start a detached helper that reinstalls spi once this process exits.

    The helper runs the same `uv tool install --force` as the manual
    recovery, after the launcher and tool environment are no longer locked.
    It gets its own hidden console, so closing the terminal does not end it.
    uv's launcher job allows silent breakaway; breaking away explicitly also
    covers an outer job that permits it, and an outer job that does not makes
    the first attempt fail, so the second runs without the flag. Returns the
    helper's log path.
    """
    _require_https(wheel_url)
    uv = shutil.which("uv")
    tool_base = _uv_tool_dir()
    bin_dir = _uv_tool_dir("--bin")
    if not uv or tool_base is None or bin_dir is None:
        raise UpdateError(
            "cannot locate uv or its tool directories. Run this from a new terminal "
            f"instead:\n{manual_upgrade_command(wheel_url)}"
        )
    script = build_helper_script(
        uv=uv,
        wheel_url=wheel_url,
        tool_dir=tool_base / "spi",
        launcher=bin_dir / "spi.exe",
        pid=os.getpid(),
    )
    cmd = prepare_command([_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", script])
    log_path = Path(tempfile.gettempdir()) / UPDATE_LOG_NAME
    if display:
        display_command(_uv_install_command(wheel_url), description="Upgrade spi after exit")

    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    with open(log_path, "w", encoding="utf-8") as log:
        for extra in (CREATE_BREAKAWAY_FROM_JOB, 0):
            try:
                subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=flags | extra,
                    close_fds=True,
                )
                break
            except OSError as exc:
                if extra == 0:
                    raise UpdateError(
                        f"could not start the update helper ({exc}). Run this from a new "
                        f"terminal instead:\n{manual_upgrade_command(wheel_url)}"
                    ) from exc
    return log_path
