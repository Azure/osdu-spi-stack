# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Tests for Windows batch-safe command preparation."""

import os
import subprocess
import sys
from unittest import mock

import pytest

from spi.shell import (
    BatchArgumentError,
    build_batch_command_line,
    escape_batch_argument,
    prepare_command,
    run_process,
)


def test_prepare_command_preserves_posix_argv():
    command = ["az", "account", "show"]
    with (
        mock.patch("spi.shell.platform.system", return_value="Linux"),
        mock.patch("spi.shell.shutil.which") as which,
    ):
        assert prepare_command(command) is command
    which.assert_not_called()


def test_prepare_command_resolves_windows_executable():
    with (
        mock.patch("spi.shell.platform.system", return_value="Windows"),
        mock.patch("spi.shell.shutil.which", return_value=r"C:\tools\kubectl.exe"),
    ):
        assert prepare_command(["kubectl", "version"]) == [
            r"C:\tools\kubectl.exe",
            "version",
        ]


def test_prepare_command_builds_escaped_batch_command_line():
    with (
        mock.patch("spi.shell.platform.system", return_value="Windows"),
        mock.patch("spi.shell.shutil.which", return_value=r"C:\tools\az.CMD"),
        mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}, clear=True),
    ):
        command = prepare_command(["az", "bicep", "build", "--file", r"C:\src\a&b\main.bicep"])

    assert command == (
        r'"C:\Windows\System32\cmd.exe" /e:ON /v:OFF /d '
        r'/c ""C:\tools\az.CMD" bicep build --file "C:\src\a&b\main.bicep""'
    )


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("build", "build"),
        ("--output", "--output"),
        ("", '""'),
        ("a b", '"a b"'),
        ("a&b", '"a&b"'),
        ("a|b", '"a|b"'),
        ("a^b", '"a^b"'),
        ("100%PATH%", '"100%%cd:~,%PATH%%cd:~,%"'),
    ],
)
def test_escape_batch_argument(argument: str, expected: str):
    assert escape_batch_argument(argument) == expected


def test_escape_batch_argument_uses_msvcrt_quote_rules():
    assert escape_batch_argument('say "hi"') == '"say \\"hi\\""'
    assert escape_batch_argument("C:\\tmp\\") == '"C:\\tmp\\\\"'


@pytest.mark.parametrize("argument", ["bad\0arg", "bad\rarg", "bad\narg"])
def test_escape_batch_argument_rejects_control_characters(argument: str):
    with pytest.raises(BatchArgumentError, match="NUL or newline"):
        escape_batch_argument(argument)


@pytest.mark.parametrize("script", ["", 'C:\\bad"name.cmd', "C:\\bad\\"])
def test_build_batch_command_line_rejects_unusable_script_path(script: str):
    with pytest.raises(BatchArgumentError, match="Batch script path"):
        build_batch_command_line(script, [])


def test_build_batch_command_line_uses_windows_fallback_for_relative_root():
    with mock.patch.dict(os.environ, {"SystemRoot": "Windows"}, clear=True):
        command = build_batch_command_line(r"C:\tools\az.cmd", [])

    assert command.startswith(r'"C:\Windows\System32\cmd.exe" ')


def test_run_process_forwards_prepared_command_and_kwargs():
    completed = subprocess.CompletedProcess("prepared", 0)
    with (
        mock.patch("spi.shell.prepare_command", return_value="prepared") as prepare,
        mock.patch("spi.shell.subprocess.run", return_value=completed) as run,
    ):
        result = run_process(["az", "account", "show"], capture_output=True, text=True)

    assert result is completed
    prepare.assert_called_once_with(["az", "account", "show"])
    run.assert_called_once_with("prepared", capture_output=True, text=True)


@pytest.mark.skipif(sys.platform != "win32", reason="requires cmd.exe")
def test_run_process_round_trips_metacharacters_through_batch(tmp_path):
    shim = tmp_path / "echo-arg.cmd"
    shim.write_bytes(b"@echo [%~1]\r\n")

    result = run_process([str(shim), "a&b"], capture_output=True, text=True, check=True)

    assert result.stdout.strip() == "[a&b]"
