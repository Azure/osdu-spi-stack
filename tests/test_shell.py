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
    ):
        command = resolve_command(["az", "version", "a&b", "100%PATH%"])

    assert command == '"C:\\tools\\az.CMD" "version" "a&b" "100%PATH"%""'


def test_resolve_command_keeps_backslash_after_unmatched_percent_quoted():
    with (
        mock.patch("spi.shell.shutil.which", return_value=r"C:\tools\az.CMD"),
        mock.patch("spi.shell._is_windows", return_value=True),
    ):
        command = resolve_command(["az", "version", r"C:\src\100%\template.bicep", "sentinel"])

    assert command == ('"C:\\tools\\az.CMD" "version" "C:\\src\\100%\\template.bicep" "sentinel"')


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
    probe = tmp_path / "argv_probe.py"
    shim = tmp_path / "argv_probe.cmd"
    probe.write_text(
        "import json, sys; print(json.dumps(sys.argv[1:]))",
        encoding="utf-8",
    )
    shim.write_text(
        '@echo off\r\n"%SPI_TEST_PYTHON%" "%SPI_TEST_PROBE%" %*\r\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SPI_TEST_PYTHON", sys.executable)
    monkeypatch.setenv("SPI_TEST_PROBE", str(probe))
    monkeypatch.setenv("SPI_CMD_EXPANSION_PROBE", "EXPANDED")

    arguments = [
        r"C:\src\a&b\template.bicep",
        "100%SPI_CMD_EXPANSION_PROBE%",
        "caret^pipe|redirect<out>paren()",
        "has space",
        "",
        'json={"spec":{"suspend":true}}',
        "a&100%SPI_CMD_EXPANSION_PROBE% b!",
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
