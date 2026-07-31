# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Tests for cross-platform executable resolution."""

import json
import os
import subprocess
import sys
from unittest import mock

import pytest

from spi.shell import resolve_command


def test_resolve_command_replaces_windows_shim_with_absolute_path():
    with (
        mock.patch("spi.shell.shutil.which", return_value=r"C:\tools\az.CMD"),
        mock.patch("spi.shell._is_windows", return_value=False),
    ):
        assert resolve_command(["az", "account", "show"]) == [
            r"C:\tools\az.CMD",
            "account",
            "show",
        ]


def test_resolve_command_preserves_arguments_when_executable_is_not_found():
    command = ["missing-tool", "--version"]
    with (
        mock.patch("spi.shell.shutil.which", return_value=None),
        mock.patch("spi.shell._is_windows", return_value=False),
    ):
        assert resolve_command(command) == command


def test_resolve_command_preserves_empty_argv():
    with mock.patch("spi.shell.shutil.which") as which:
        assert resolve_command([]) == []
    which.assert_not_called()


def test_resolve_command_serializes_windows_batch_arguments():
    with (
        mock.patch("spi.shell.shutil.which", return_value=r"C:\tools\az.CMD"),
        mock.patch("spi.shell._is_windows", return_value=True),
        mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}),
    ):
        command = resolve_command(["az", "version", "a&b", "100%PATH%"])

    assert command == (
        '"C:\\Windows\\System32\\cmd.exe" /d /v:off /s /c '
        '""C:\\tools\\az.CMD" "version" "a&b" "100"^%"PATH"^%"""'
    )


def test_resolve_command_keeps_backslash_after_unmatched_percent_quoted():
    with (
        mock.patch("spi.shell.shutil.which", return_value=r"C:\tools\az.CMD"),
        mock.patch("spi.shell._is_windows", return_value=True),
        mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}),
    ):
        command = resolve_command(["az", "version", r"C:\src\100%\template.bicep", "sentinel"])

    assert command == (
        '"C:\\Windows\\System32\\cmd.exe" /d /v:off /s /c '
        '""C:\\tools\\az.CMD" "version" "C:\\src\\100"^%"\\template.bicep" "sentinel""'
    )


@pytest.mark.parametrize("value", ["a\nb", "a\rb", "a\r\nb"])
def test_resolve_command_rejects_line_breaks_for_windows_batch_shims(value):
    with (
        mock.patch("spi.shell.shutil.which", return_value=r"C:\tools\az.CMD"),
        mock.patch("spi.shell._is_windows", return_value=True),
        mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}),
        pytest.raises(ValueError, match="carriage return or newline"),
    ):
        resolve_command(["az", "version", value])


def test_resolve_command_keeps_windows_native_executables_as_argv():
    with (
        mock.patch("spi.shell.shutil.which", return_value=r"C:\tools\kubectl.exe"),
        mock.patch("spi.shell._is_windows", return_value=True),
    ):
        assert resolve_command(["kubectl", "version", "--client"]) == [
            r"C:\tools\kubectl.exe",
            "version",
            "--client",
        ]


@pytest.mark.skipif(os.name != "nt", reason="Windows batch parsing test")
def test_resolve_command_preserves_arguments_through_percent_star_batch_shim(tmp_path, monkeypatch):
    monkeypatch.setenv("SPI_CMD_PATH_PROBE", "EXPANDED")
    shim_dir = tmp_path / "shim%SPI_CMD_PATH_PROBE% & dir"
    shim_dir.mkdir()
    probe = shim_dir / "argv_probe.py"
    shim = shim_dir / "argv_probe.cmd"
    probe.write_text(
        "import json, sys; print(json.dumps(sys.argv[1:]))",
        encoding="utf-8",
    )
    shim.write_text(
        '@echo off\r\n"%SPI_TEST_PYTHON%" "%~dp0argv_probe.py" %*\r\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SPI_TEST_PYTHON", sys.executable)
    monkeypatch.setenv("SPI_CMD_EXPANSION_PROBE", "EXPANDED")

    arguments = [
        r"C:\src\a&b\template.bicep",
        "100%SPI_CMD_EXPANSION_PROBE%",
        "caret^pipe|redirect<out>paren()",
        "has space",
        "",
        'json={"spec":{"suspend":true}}',
        "a&100%SPI_CMD_EXPANSION_PROBE% b!",
        "%COMSPEC:C=&echo SPI_INJECTED&%",
        "sentinel-after-replacement-expression",
        "%PATH:C=X%",
        "sentinel-after-path-replacement",
        "%COMSPEC:~0,3%",
        "sentinel-after-substring-expression",
        r"C:\src\100%\template.bicep",
        "sentinel-after-percent-backslash",
        r"a%\ b",
        "sentinel-after-percent-backslash-space",
        "%1",
        "%*",
        "%~dp0",
        "%%",
        "%&%",
        "trailing\\",
    ]
    command = resolve_command([str(shim), *arguments])

    assert isinstance(command, str)
    result = subprocess.run(command, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == arguments
