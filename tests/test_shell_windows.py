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

"""Windows batch-shim argument handling in spi.shell.

These tests run on every platform: the Windows branch is exercised by
faking the platform probe and the PATH lookup, and the escaping helpers are
pure string functions.
"""

import sys

import pytest

from spi import shell


@pytest.fixture
def fake_windows(monkeypatch):
    """Pretend we are on Windows with az installed as a .cmd shim."""
    monkeypatch.setattr(shell, "_is_windows", lambda: True)
    monkeypatch.setattr(shell.os.path, "abspath", lambda p: p)
    monkeypatch.setattr(shell, "_WINDOWS_CMD_EXE", r"C:\Windows\System32\cmd.exe")

    def fake_which(program):
        return {
            "az": r"C:\Program Files\Azure CLI\wbin\az.cmd",
            "kubectl": r"C:\tools\kubectl.exe",
        }.get(program)

    monkeypatch.setattr(shell.shutil, "which", fake_which)


def test_posix_argv_is_untouched(monkeypatch):
    monkeypatch.setattr(shell, "_is_windows", lambda: False)
    cmd = ["az", "bicep", "build", "--file", "/src/a&b/main.bicep"]
    assert shell.prepare_command(cmd) == cmd


def test_windows_exe_keeps_argv_list(fake_windows):
    prepared = shell.prepare_command(["kubectl", "get", "pods"])
    assert prepared == [r"C:\tools\kubectl.exe", "get", "pods"]


def test_batch_shim_becomes_escaped_command_line(fake_windows):
    prepared = shell.prepare_command(["az", "bicep", "build", "--file", r"C:\src\a&b\main.bicep"])
    assert isinstance(prepared, str)
    assert prepared.startswith(
        '"C:\\Windows\\System32\\cmd.exe" /e:ON /v:OFF /d /c ""C:\\Program Files\\Azure CLI'
    )
    # The metacharacter stays inside the quoted argument instead of splitting.
    assert r'"C:\src\a&b\main.bicep"' in prepared
    assert prepared.endswith('"')


def test_percent_sequences_are_not_expanded():
    escaped = shell.escape_batch_argument("100%PATH%")
    # Each '%' is prefixed with the zero-length '%%cd:~,' expansion, so cmd.exe
    # never sees a bare %PATH% pair to expand.
    assert escaped == '"100%%cd:~,%PATH%%cd:~,%"'


def test_plain_argument_is_not_quoted():
    assert shell.escape_batch_argument("--output") == "--output"
    assert shell.escape_batch_argument("build") == "build"


def test_metacharacters_and_spaces_force_quotes():
    for arg in ["a&b", "a|b", "a^b", "a b", "(x)", "!VAR!", ""]:
        assert shell.escape_batch_argument(arg).startswith('"')


def test_embedded_quotes_and_trailing_backslashes():
    assert shell.escape_batch_argument('say "hi"') == '"say ""hi"""'
    assert shell.escape_batch_argument("C:\\tmp\\") == '"C:\\tmp\\\\"'


def test_newlines_are_rejected():
    with pytest.raises(shell.BatchArgumentError):
        shell.escape_batch_argument("line1\nline2")
    with pytest.raises(shell.BatchArgumentError):
        shell.escape_batch_argument("line1\rline2")


def test_unusable_shim_path_is_rejected():
    with pytest.raises(shell.BatchArgumentError):
        shell.build_batch_command_line("C:\\tools\\az.cmd\\", ["version"])


def test_run_process_passes_prepared_command(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return "result"

    monkeypatch.setattr(shell, "_is_windows", lambda: False)
    monkeypatch.setattr(shell.subprocess, "run", fake_run)

    assert shell.run_process(["kubectl", "get", "pods"], capture_output=True) == "result"
    assert seen["cmd"] == ["kubectl", "get", "pods"]
    assert seen["kwargs"] == {"capture_output": True}


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows cmd.exe")
def test_windows_batch_shim_e2e(tmp_path):
    """End-to-end: verify that metacharacter arguments reach the batch shim unchanged.

    A temporary .cmd probe echoes its first argument via ``%1`` (which preserves
    the surrounding double-quotes that build_batch_command_line adds). Running it
    through run_process with an argument that contains a cmd.exe metacharacter
    (``&``) confirms that the escaping in build_batch_command_line is actually
    effective, not just textually correct.

    Note: ``%~1`` is intentionally avoided here. It strips the surrounding quotes,
    exposing the bare ``&`` to cmd.exe's metacharacter scanner (which would split
    the echo into two commands). Real batch shims use ``%*``, which preserves the
    surrounding quotes and passes them intact to the target executable.
    """
    probe = tmp_path / "probe.cmd"
    probe.write_text("@echo off\necho %1\n", encoding="utf-8")
    result = shell.run_process(
        [str(probe), "a&b"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == '"a&b"'
