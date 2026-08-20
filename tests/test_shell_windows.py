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

"""End-to-end proof through a real cmd.exe on native Windows.

The contract of issue #49 is that a target behind a ``%*``-forwarding batch
shim receives its argv exactly as the caller wrote it. These tests assert
the decoded argv, not any intermediate representation: a real ``.cmd`` shim
forwards ``%*`` to a real Python process that JSON-dumps ``sys.argv``. The
shim itself lives in a directory whose name contains ``%VAR%``, ``&``, ``^``,
``!`` and a space, so the shim-path half of the command line is proven too.
"""

import json
import subprocess
import sys

import pytest

from spi.shell import run_process

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires cmd.exe")

# Sentinels follow the riskiest arguments so a desync (an escaping bug that
# splits, merges, or shifts arguments) is attributable to its cause; the
# whole-list equality below is what actually detects one.
HOSTILE_ARGUMENTS = [
    r"C:\src\a&b\template.bicep",
    "s1",
    "100%SPI_E2E_PROBE%",
    "s2",
    "caret^pipe|redirect<out>paren()",
    "s3",
    "has space",
    "",
    'json={"spec":{"suspend":true}}',
    "s4",
    'say "hi',
    "s5",
    "%COMSPEC:C=&echo INJECTED&%",
    "s6",
    "%COMSPEC:~0,3%",
    "s7",
    "%1",
    "%*",
    "%~dp0",
    "%%",
    "s8",
    r"C:\src\100%\template.bicep",
    "s9",
    "trailing\\",
    "s10",
]


def _make_probe(tmp_path):
    shim_dir = tmp_path / "shim%SPI_E2E_PROBE% & ^caret !bang dir"
    shim_dir.mkdir()
    probe = shim_dir / "argv_probe.py"
    probe.write_text("import json, sys; print(json.dumps(sys.argv[1:]))", encoding="utf-8")
    shim = shim_dir / "argv_probe.cmd"
    shim.write_text(
        '@echo off\r\n"%SPI_E2E_PYTHON%" "%~dp0argv_probe.py" %*\r\n',
        encoding="utf-8",
    )
    return shim


def test_batch_shim_round_trips_hostile_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("SPI_E2E_PYTHON", sys.executable)
    monkeypatch.setenv("SPI_E2E_PROBE", "EXPANDED")
    shim = _make_probe(tmp_path)

    result = run_process([str(shim), *HOSTILE_ARGUMENTS], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == HOSTILE_ARGUMENTS


def test_unrepresentable_argument_is_a_normal_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("SPI_E2E_PYTHON", sys.executable)
    shim = _make_probe(tmp_path)

    result = run_process([str(shim), "line1\nline2"], capture_output=True, text=True)

    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 1
    assert "newline or NUL" in result.stderr
    assert "line1" not in result.stderr
