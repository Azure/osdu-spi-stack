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
import ntpath
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


def _is_windows() -> bool:
    return os.name == "nt"


def _quote_windows_batch_fragment(value: str) -> str:
    """Quote one fragment for cmd.exe followed by the Windows runtime parser."""
    quoted = ['"']
    backslashes = 0

    for char in value:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            quoted.append("\\" * (backslashes * 2))
            quoted.append('""')
            backslashes = 0
            continue
        if backslashes:
            quoted.append("\\" * backslashes)
            backslashes = 0
        quoted.append(char)

    if backslashes:
        quoted.append("\\" * (backslashes * 2))
    quoted.append('"')
    return "".join(quoted)


def _quote_windows_batch_argument(value: str) -> str:
    """Preserve an argument through cmd.exe and a batch shim's ``%*`` expansion.

    Quoting protects CMD metacharacters. Every percent is caret-escaped outside
    quoted fragments so environment, replacement, substring, positional, and
    for-variable expansions remain literal. The target runtime recombines the
    adjacent fragments into the original argument.
    """
    return "^%".join(_quote_windows_batch_fragment(fragment) for fragment in value.split("%"))


def _windows_command_processor() -> str:
    system_root = os.environ.get("SystemRoot")
    if system_root:
        return ntpath.join(system_root, "System32", "cmd.exe")
    return shutil.which("cmd.exe") or "cmd.exe"


def _serialize_windows_batch_command(cmd_list: List[str]) -> str:
    for index, value in enumerate(cmd_list):
        if "\r" in value or "\n" in value:
            raise ValueError(
                f"Windows batch command argument {index} contains a carriage return or newline"
            )

    command_processor = _windows_command_processor()
    batch_command = " ".join(_quote_windows_batch_argument(value) for value in cmd_list)
    return f'{_quote_windows_batch_fragment(command_processor)} /d /v:off /s /c "{batch_command}"'


def resolve_command(cmd_list: List[str]) -> List[str] | str:
    """Resolve and safely prepare a command for direct subprocess calls.

    Windows often exposes CLIs such as Azure CLI as ``az.cmd``. PowerShell can
    resolve ``az`` through PATHEXT, but ``subprocess.run(["az", ...])`` with
    ``shell=False`` cannot. Batch shims also reparse their arguments through
    cmd.exe, so launch a known command processor and serialize the shim path and
    arguments explicitly to preserve literal metacharacters and percent
    expressions. Native executables and non-Windows platforms continue to
    receive ordinary argv lists.
    """
    if not cmd_list:
        return cmd_list
    executable = shutil.which(cmd_list[0])
    if executable:
        resolved = [executable, *cmd_list[1:]]
    else:
        resolved = cmd_list
    if _is_windows() and resolved[0].lower().endswith((".cmd", ".bat")):
        return _serialize_windows_batch_command(resolved)
    return resolved


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

    result = subprocess.run(resolve_command(cmd_list), capture_output=capture_output, text=text)

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
        proc = subprocess.run(
            resolve_command(["kubectl", "apply", "-f", "-"]),
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
    cmd = resolve_command(["kubectl"] + args + ["-o", "json"])
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
