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

"""Command execution and kubectl helpers.

``run_command`` is the transparent front door used whenever an az/kubectl/
flux/helm command should be visible to the operator. ``kubectl_apply_yaml``
retries on transient kube-API errors. ``kubectl_json`` is the silent query
helper used by status/info/guard where panel output would be noise.
"""

import json
import os
import shlex
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

import typer
from rich.panel import Panel
from rich.syntax import Syntax

from .console import console

_BATCH_SUFFIXES = (".cmd", ".bat")
_CMD_METACHARACTERS = frozenset('&|<>()^"')

TRANSIENT_KUBECTL_ERRORS = (
    "connection refused",
    "connection reset by peer",
    "context deadline exceeded",
    "eof",
    "i/o timeout",
    "no route to host",
    "service unavailable",
    "temporarily unavailable",
    "the server is currently unable to handle the request",
    "tls handshake timeout",
)


def resolve_command(cmd_list: List[str]) -> List[str]:
    """Resolve the executable path for direct subprocess calls.

    Windows often exposes CLIs such as Azure CLI as ``az.cmd``. PowerShell can
    resolve ``az`` through PATHEXT, but ``subprocess.run(["az", ...])`` with
    ``shell=False`` cannot. Resolve the first argv element up front so callers
    keep transparent argv lists without relying on shell execution.
    """
    if not cmd_list:
        return cmd_list
    executable = shutil.which(cmd_list[0])
    if executable:
        return [executable, *cmd_list[1:]]
    return cmd_list


def _is_batch(path: str) -> bool:
    """Return whether path names a Windows batch command."""
    return os.name == "nt" and path.lower().endswith(_BATCH_SUFFIXES)


def _escape_batch_arg(arg: str) -> str:
    """Quote an argument for the MSVCRT and cmd.exe parsing layers."""
    if any(char in arg for char in "%\r\n"):
        raise ValueError(
            f"Windows batch arguments cannot contain percent signs or newlines: {arg!r}"
        )

    quoted = ['"']
    backslashes = 0
    for char in arg:
        if char == "\\":
            backslashes += 1
            continue

        if char == '"':
            quoted.append("\\" * (backslashes * 2 + 1))
        else:
            quoted.append("\\" * backslashes)
        backslashes = 0
        quoted.append(char)

    quoted.append("\\" * (backslashes * 2))
    quoted.append('"')
    return "".join(f"^{char}" if char in _CMD_METACHARACTERS else char for char in quoted)


def _build_batch_command_line(exe: str, args: List[str]) -> str:
    """Build an lpCommandLine string that preserves arguments through cmd.exe."""
    parts = [f'"{exe}"']
    parts.extend(_escape_batch_arg(arg) for arg in args)
    return " ".join(parts)


def run_subprocess(cmd_list: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """Resolve and run a command, safely escaping Windows batch shims."""
    resolved = resolve_command(cmd_list)
    if resolved and _is_batch(resolved[0]):
        return subprocess.run(_build_batch_command_line(resolved[0], resolved[1:]), **kwargs)
    return subprocess.run(resolved, **kwargs)


def run_command(
    cmd_list: List[str],
    capture_output: bool = True,
    text: bool = True,
    display: bool = True,
    description: Optional[str] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a command and display it in a formatted panel."""
    formatted_parts = []
    if cmd_list:
        formatted_parts.append(cmd_list[0])

    i = 1
    while i < len(cmd_list):
        if cmd_list[i].startswith("-"):
            formatted_parts.append("\\\n  " + shlex.quote(cmd_list[i]))
        else:
            formatted_parts.append(shlex.quote(cmd_list[i]))
        i += 1

    formatted_cmd = " ".join(formatted_parts)

    if display:
        first = cmd_list[0] if cmd_list else ""
        style_map = {
            "az": ("azure", "[azure]Azure CLI[/azure]"),
            "kubectl": ("kubectl", "[kubectl]Kubernetes[/kubectl]"),
            "flux": ("flux", "[flux]Flux CD[/flux]"),
            "helm": ("helm", "[helm]Helm[/helm]"),
        }
        style, title = style_map.get(first, ("white", "Command"))

        if description:
            title = f"{title}: {description}"

        command_syntax = Syntax(formatted_cmd, "bash", theme="monokai", line_numbers=False)
        console.print(Panel(command_syntax, title=title, border_style=style))

    result = run_subprocess(cmd_list, capture_output=capture_output, text=text)

    if check and result.returncode != 0:
        if result.stderr and result.stderr.strip():
            console.print(Panel(result.stderr.strip(), title="Error Output", border_style="error"))
        console.print(f"[error]Command failed (exit code {result.returncode})[/error]")
        raise typer.Exit(code=1)

    return result


def kubectl_apply_yaml(
    yaml_content: str,
    description: str,
    retries: int = 4,
    base_delay: int = 2,
) -> subprocess.CompletedProcess:
    """Apply YAML via kubectl with retry/backoff for transient API failures."""
    delay = base_delay
    for attempt in range(1, retries + 1):
        proc = run_subprocess(
            ["kubectl", "apply", "-f", "-"],
            input=yaml_content,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return proc

        stderr = (proc.stderr or proc.stdout or "").strip()
        lowered = stderr.lower()
        is_transient = any(marker in lowered for marker in TRANSIENT_KUBECTL_ERRORS)
        if is_transient and attempt < retries:
            console.print(
                f"  [warning]{description} hit a transient Kubernetes API error; "
                f"retrying in {delay}s (attempt {attempt}/{retries})[/warning]"
            )
            time.sleep(delay)
            delay *= 2
            continue

        console.print(f"  [error]Failed to {description}: {stderr or 'unknown error'}[/error]")
        raise typer.Exit(code=1)

    raise typer.Exit(code=1)


def kubectl_json(args: List[str]) -> Optional[Dict[str, Any]]:
    """Run a silent kubectl query and return parsed JSON, or None on failure.

    Used by status/info/guard for background state reads where the
    transparent command panel from ``run_command`` would be noise.
    """
    cmd = ["kubectl"] + args + ["-o", "json"]
    result = run_subprocess(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
