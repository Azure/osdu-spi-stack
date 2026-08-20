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

Every process the CLI launches goes through ``run_process``. On native Windows, CLIs such as
Azure CLI install as ``.cmd`` batch shims; ``CreateProcess`` cannot run a
batch file, so the OS relaunches it through ``cmd.exe``, which re-parses the
flat command line (``shell=False`` constrains Python, not the OS). For those
shims ``prepare_command`` builds an explicit ``cmd.exe`` command line with
every argument escaped, applying the mitigations published for the
CVE-2024-24576 (BatBadBut) class. Scope: the guarantee holds for standard
``%*``-forwarding shims such as ``az.cmd``. A shim that re-parses its
arguments again (``call``, ``%~1`` re-expansion, ``setlocal
enabledelayedexpansion``) defeats any command-line escaping scheme, and
cmd.exe caps its command line at 8,191 characters where ``CreateProcess``
allows 32,767. Panels printed by ``run_command`` show the logical argv, not
the serialized cmd.exe line.
"""

import json
import ntpath
import os
import platform
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

_BATCH_SUFFIXES = (".cmd", ".bat")
PreparedCommand = Union[List[str], str]


class BatchArgumentError(ValueError):
    """An argument that cannot be represented on a cmd.exe command line."""


def escape_batch_argument(value: str) -> str:
    """Escape one argument so a ``%*``-forwarding batch shim receives it verbatim.

    The argument must survive two parsers. For cmd.exe, quoting protects
    metacharacters and each ``%`` is neutralized with the ``%%cd:~,%``
    empty-substring expansion; it relies on command
    extensions, on ``CD`` being a defined dynamic variable, and on batch
    ``%*`` substitution text not being re-scanned for expansion. For the
    target's MSVCRT argv parser, backslash runs before a quote are doubled
    and an embedded quote becomes ``""``, which keeps cmd's quote state
    balanced for any input, so no quote-parity restriction is needed. Every
    argument is quoted: an unquoted fast path would only add tokenization
    edge cases.
    """
    if any(ch in value for ch in ("\r", "\n", "\0")):
        raise BatchArgumentError(
            "argument contains a newline or NUL character, which cmd.exe "
            "cannot deliver to a batch shim"
        )
    value = value.replace("%", "%%cd:~,%")
    quoted = ['"']
    backslashes = 0
    for ch in value:
        if ch == "\\":
            backslashes += 1
            continue
        if ch == '"':
            quoted.append("\\" * (backslashes * 2))
            quoted.append('""')
        else:
            quoted.append("\\" * backslashes)
            quoted.append(ch)
        backslashes = 0
    quoted.append("\\" * (backslashes * 2))
    quoted.append('"')
    return "".join(quoted)


def _cmd_exe() -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    if not ntpath.isabs(root):
        root = r"C:\Windows"
    return ntpath.join(root, "System32", "cmd.exe")


def build_batch_command_line(script: str, args: List[str]) -> str:
    """Build the explicit cmd.exe command line that launches a batch shim.

    ``/e:ON`` guarantees the extensions ``%%cd:~,%`` needs, ``/v:OFF`` keeps
    ``!`` literal in the outer parse, ``/d`` skips AutoRun. The shim path
    gets the same percent treatment as the arguments; a path containing a
    quote or ending in a backslash cannot be represented and is rejected.
    """
    if not script or '"' in script or script.endswith("\\"):
        raise BatchArgumentError("batch shim path cannot be represented on a cmd.exe command line")
    escaped_script = script.replace("%", "%%cd:~,%")
    parts = [
        f'"{_cmd_exe()}" /e:ON /v:OFF /d /c ""{escaped_script}"',
        *(escape_batch_argument(arg) for arg in args),
    ]
    return " ".join(parts) + '"'


def prepare_command(cmd_list: List[str]) -> PreparedCommand:
    """Return what ``subprocess`` should launch for ``cmd_list``.

    On non-Windows platforms the argv list passes through untouched. On
    Windows the program resolves through PATH (PATHEXT finds ``az.cmd``
    where bare ``az`` fails ``shell=False``); a resolved ``.cmd``/``.bat``
    shim becomes an escaped cmd.exe command line, anything else an argv
    list with the resolved program as argv[0].
    """
    if not cmd_list or platform.system() != "Windows":
        return cmd_list
    program = shutil.which(cmd_list[0]) or cmd_list[0]
    if ntpath.splitext(program)[1].lower() in _BATCH_SUFFIXES:
        return build_batch_command_line(ntpath.abspath(program), cmd_list[1:])
    return [program, *cmd_list[1:]]


def run_process(cmd_list: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` through the platform-safe launch path.

    An argument the batch layer cannot represent is reported as an ordinary
    failed launch (returncode 1, reason on stderr) rather than a raised
    exception: every call site already handles a nonzero returncode, and a
    traceback mid-deploy helps nobody. The argument value is never echoed
    into the error; it may be a secret.
    """
    try:
        prepared = prepare_command(cmd_list)
    except BatchArgumentError as exc:
        program = cmd_list[0] if cmd_list else ""
        error = f"{program}: {exc}"
        if kwargs.get("text", False):
            return subprocess.CompletedProcess(cmd_list, 1, stdout="", stderr=error)
        return subprocess.CompletedProcess(cmd_list, 1, stdout=b"", stderr=error.encode())
    return subprocess.run(prepared, **kwargs)


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

    result = run_process(cmd_list, capture_output=capture_output, text=text)

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
