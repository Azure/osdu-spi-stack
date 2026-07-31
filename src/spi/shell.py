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
import platform
import re
import shlex
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Union

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

_SAFE_BATCH_ARGUMENT = re.compile(r"[A-Za-z0-9_@+=:,./\\-]+")
_BATCH_SUFFIXES = {".bat", ".cmd"}
PreparedCommand = Union[List[str], str]


class BatchArgumentError(ValueError):
    """Raised when an argument cannot be represented safely through cmd.exe."""


def _validate_batch_value(value: str, label: str) -> None:
    if any(character in value for character in ("\0", "\r", "\n")):
        raise BatchArgumentError(f"{label} contains a NUL or newline character")


def _quote_windows_argument(argument: str) -> str:
    """Quote one argument using the MSVCRT backslash and double-quote rules."""
    quoted = ['"']
    backslashes = 0
    for character in argument:
        if character == "\\":
            backslashes += 1
            continue
        if character == '"':
            quoted.append("\\" * (backslashes * 2 + 1))
        else:
            quoted.append("\\" * backslashes)
        quoted.append(character)
        backslashes = 0
    quoted.append("\\" * (backslashes * 2))
    quoted.append('"')
    return "".join(quoted)


def escape_batch_argument(argument: str) -> str:
    """Escape one value for the batch script argument list."""
    _validate_batch_value(argument, "Batch argument")
    needs_quotes = (
        not argument or argument.endswith("\\") or _SAFE_BATCH_ARGUMENT.fullmatch(argument) is None
    )
    escaped = argument.replace("%", "%%cd:~,%")
    return _quote_windows_argument(escaped) if needs_quotes else escaped


def _cmd_executable() -> str:
    windows_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    if not ntpath.isabs(windows_root):
        windows_root = r"C:\Windows"
    return ntpath.join(windows_root, "System32", "cmd.exe")


def build_batch_command_line(script: str, args: List[str]) -> str:
    """Build a cmd.exe command line that preserves every batch-script argument."""
    _validate_batch_value(script, "Batch script path")
    if not script or '"' in script or script.endswith("\\"):
        raise BatchArgumentError(
            "Batch script path must be non-empty and cannot contain a quote or end in a backslash"
        )

    escaped_script = script.replace("%", "%%cd:~,%")
    command = [
        _quote_windows_argument(_cmd_executable()),
        "/e:ON",
        "/v:OFF",
        "/d",
        f'/c ""{escaped_script}"',
        *(escape_batch_argument(argument) for argument in args),
    ]
    return " ".join(command) + '"'


def prepare_command(cmd_list: List[str]) -> PreparedCommand:
    """Prepare argv for direct execution, including Windows batch shims."""
    if not cmd_list:
        return cmd_list
    if platform.system() != "Windows":
        return cmd_list

    executable = shutil.which(cmd_list[0])
    if not executable:
        return cmd_list
    if ntpath.splitext(executable)[1].lower() in _BATCH_SUFFIXES:
        return build_batch_command_line(executable, cmd_list[1:])
    return [executable, *cmd_list[1:]]


def resolve_command(cmd_list: List[str]) -> PreparedCommand:
    """Compatibility wrapper for callers migrating to ``run_process``."""
    return prepare_command(cmd_list)


def run_process(cmd_list: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """Run a command through the platform-safe launch path."""
    return subprocess.run(prepare_command(cmd_list), **kwargs)


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

    try:
        result = run_process(cmd_list, capture_output=capture_output, text=text)
    except BatchArgumentError as exc:
        console.print(f"[error]{exc}[/error]")
        raise typer.Exit(code=1) from exc

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
        proc = run_process(
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
    result = run_process(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
