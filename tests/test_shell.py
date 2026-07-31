# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Tests for cross-platform executable preparation."""

from unittest import mock

from spi.shell import resolve_command


def test_resolve_command_replaces_windows_executable_with_absolute_path():
    with (
        mock.patch("spi.shell.platform.system", return_value="Windows"),
        mock.patch("spi.shell.shutil.which", return_value=r"C:\tools\kubectl.exe"),
    ):
        assert resolve_command(["az", "account", "show"]) == [
            r"C:\tools\kubectl.exe",
            "account",
            "show",
        ]


def test_resolve_command_preserves_arguments_when_executable_is_not_found():
    command = ["missing-tool", "--version"]
    with (
        mock.patch("spi.shell.platform.system", return_value="Windows"),
        mock.patch("spi.shell.shutil.which", return_value=None),
    ):
        assert resolve_command(command) == command


def test_resolve_command_preserves_empty_argv():
    with mock.patch("spi.shell.shutil.which") as which:
        assert resolve_command([]) == []
    which.assert_not_called()
