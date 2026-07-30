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

Every process launch goes through ``run_process`` so Windows batch shims
(``az.cmd`` and friends) receive arguments exactly as written; see
``prepare_command`` for the details.
"""

import json
import os
import platform
import shlex
import shutil
import subprocess
import time
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Union

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


BATCH_SUFFIXES = (".cmd", ".bat")

# Characters cmd.exe leaves alone outside quotes; anything else forces quoting.
_BATCH_UNQUOTED = "#$*+-./:?@\\_"


class BatchArgumentError(ValueError):
    """An argument cannot be passed safely through a Windows batch shim."""


def _is_windows() -> bool:
    return platform.system().lower() == "windows"


def _is_batch_file(program: str) -> bool:
    return program.lower().endswith(BATCH_SUFFIXES)


def _batch_arg_needs_quotes(arg: str) -> bool:
    """Quote unless every character is known to survive cmd.exe unquoted."""
    if not arg or arg.endswith("\\"):
        return True
    for ch in arg:
        if ch.isascii():
            if not (ch.isalnum() or ch in _BATCH_UNQUOTED):
                return True
        elif unicodedata.category(ch) == "Cc":
            return True
    return False


def escape_batch_argument(arg: str) -> str:
    """Escape one argument so a batch file receives it verbatim.

    cmd.exe re-parses the command line it hands to a ``.cmd``/``.bat`` shim,
    so plain quoting is not enough: ``%NAME%`` would still expand. Quoting
    covers metacharacters (``&``, ``|``, ``^``, ...) and each ``%`` is
    neutralised with the ``%%cd:~,%`` no-op substring expansion.
    """
    if any(ch in arg for ch in ("\r", "\n", "\0")):
        raise BatchArgumentError(
            "argument contains a newline or NUL character, which cannot be "
            "passed through a Windows batch shim"
        )

    quote = _batch_arg_needs_quotes(arg)
    parts: List[str] = ['"'] if quote else []
    backslashes = 0
    for ch in arg:
        if ch == "\\":
            backslashes += 1
        else:
            if ch == '"':
                # Double the run of backslashes, then escape the quote.
                parts.append("\\" * backslashes)
                parts.append('"')
            elif ch == "%":
                parts.append("%%cd:~,")
            backslashes = 0
        parts.append(ch)
    if quote:
        parts.append("\\" * backslashes)
        parts.append('"')
    return "".join(parts)


def build_batch_command_line(script: str, args: Sequence[str]) -> str:
    """Build a ``cmd.exe`` command line that invokes a batch shim safely.

    ``/e:ON`` keeps command extensions (the ``%%cd:~,%`` trick needs them),
    ``/v:OFF`` disables delayed expansion so ``!VAR!`` stays literal, and
    ``/d`` skips AutoRun commands.
    """
    if '"' in script or script.endswith("\\"):
        raise BatchArgumentError(f"batch shim path is not usable by cmd.exe: {script}")

    parts = [f'cmd.exe /e:ON /v:OFF /d /c ""{script}"']
    parts.extend(escape_batch_argument(arg) for arg in args)
    return " ".join(parts) + '"'


def prepare_command(cmd_list: Sequence[str]) -> Union[str, List[str]]:
    """Return the argv (or Windows command line) to hand to ``subprocess``.

    On POSIX the argv list is returned untouched. On Windows the program is
    resolved through ``PATH`` so ``.cmd``/``.bat`` shims (Azure CLI, helm,
    flux, ...) are found; when the resolved program is such a shim, a fully
    escaped ``cmd.exe`` command line is returned instead of an argv list so
    Python's default quoting cannot be undone by batch re-parsing.
    """
    args = list(cmd_list)
    if not args or not _is_windows():
        return args

    program = shutil.which(args[0]) or args[0]
    if not _is_batch_file(program):
        return [program, *args[1:]]
    return build_batch_command_line(os.path.abspath(program), args[1:])


def run_process(cmd_list: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` with Windows batch-shim handling applied."""
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
        raise typer.Exit(code=1)

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
    result = run_process(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
