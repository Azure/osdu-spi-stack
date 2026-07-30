# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Tests for cross-platform executable resolution."""

from unittest import mock

import pytest

from spi.shell import _escape_batch_arg, resolve_command, run_subprocess


def test_resolve_command_replaces_windows_shim_with_absolute_path():
    with mock.patch("spi.shell.shutil.which", return_value=r"C:\tools\az.CMD"):
        assert resolve_command(["az", "account", "show"]) == [
            r"C:\tools\az.CMD",
            "account",
            "show",
        ]


def test_resolve_command_preserves_arguments_when_executable_is_not_found():
    command = ["missing-tool", "--version"]
    with mock.patch("spi.shell.shutil.which", return_value=None):
        assert resolve_command(command) == command


def test_resolve_command_preserves_empty_argv():
    with mock.patch("spi.shell.shutil.which") as which:
        assert resolve_command([]) == []
    which.assert_not_called()


def test_run_subprocess_escapes_windows_batch_arguments():
    completed = mock.Mock()
    with (
        mock.patch("spi.shell.os.name", "nt"),
        mock.patch("spi.shell.shutil.which", return_value=r"C:\Program Files\Azure CLI\az.CMD"),
        mock.patch("spi.shell.subprocess.run", return_value=completed) as run,
    ):
        result = run_subprocess(
            ["az", "bicep", "build", "--file", r"C:\src\a&b\template.bicep"],
            capture_output=True,
            text=True,
        )

    assert result is completed
    command_line = (
        r'"C:\Program Files\Azure CLI\az.CMD" '
        r'^"bicep^" ^"build^" ^"--file^" ^"C:\src\a^&b\template.bicep^"'
    )
    run.assert_called_once_with(command_line, capture_output=True, text=True)
    assert "&" not in command_line.replace("^&", "")


def test_run_subprocess_rejects_percent_in_windows_batch_executable():
    executable = r"C:\tools%PATH%\az.cmd"
    with (
        mock.patch("spi.shell.os.name", "nt"),
        mock.patch("spi.shell.shutil.which", return_value=executable),
        mock.patch("spi.shell.subprocess.run") as run,
        pytest.raises(ValueError, match="Windows batch executable paths") as error,
    ):
        run_subprocess(["az", "version"])

    assert repr(executable) in str(error.value)
    run.assert_not_called()


def test_escape_batch_arg_escapes_cmd_operators():
    assert _escape_batch_arg("a^b<c>d|e(f)") == r'^"a^^b^<c^>d^|e^(f^)^"'


def test_escape_batch_arg_quotes_spaces_backslashes_and_quotes():
    assert _escape_batch_arg('a b\\"c\\') == '^"a b\\\\\\^"c\\\\^"'


@pytest.mark.parametrize("arg", ["100%PATH%", "line\nbreak", "line\rbreak"])
def test_escape_batch_arg_rejects_values_cmd_cannot_preserve(arg):
    with pytest.raises(ValueError, match="Windows batch arguments") as error:
        _escape_batch_arg(arg)

    assert repr(arg) in str(error.value)


def test_run_subprocess_preserves_list_for_windows_executable():
    completed = mock.Mock()
    command = ["kubectl", "version", "--client"]
    resolved = [r"C:\tools\kubectl.exe", *command[1:]]
    with (
        mock.patch("spi.shell.os.name", "nt"),
        mock.patch("spi.shell.shutil.which", return_value=resolved[0]),
        mock.patch("spi.shell.subprocess.run", return_value=completed) as run,
    ):
        result = run_subprocess(command, capture_output=True)

    assert result is completed
    run.assert_called_once_with(resolved, capture_output=True)


def test_run_subprocess_preserves_list_outside_windows():
    completed = mock.Mock()
    command = ["az", "version"]
    resolved = ["/usr/local/bin/az.cmd", "version"]
    with (
        mock.patch("spi.shell.os.name", "posix"),
        mock.patch("spi.shell.shutil.which", return_value=resolved[0]),
        mock.patch("spi.shell.subprocess.run", return_value=completed) as run,
    ):
        result = run_subprocess(command)

    assert result is completed
    run.assert_called_once_with(resolved)


def test_run_subprocess_preserves_unresolved_command():
    completed = mock.Mock()
    command = ["missing-tool", "--version"]
    with (
        mock.patch("spi.shell.shutil.which", return_value=None),
        mock.patch("spi.shell.subprocess.run", return_value=completed) as run,
    ):
        result = run_subprocess(command)

    assert result is completed
    run.assert_called_once_with(command)
