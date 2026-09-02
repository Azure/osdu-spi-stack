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

"""Platform-safe command preparation in spi.shell.

These tests run on every platform: the Windows branch is exercised by faking
the platform probe and PATH lookup, and the escaping helpers are pure string
functions. The end-to-end proof through a real cmd.exe lives in
test_shell_windows.py.
"""

import os
import subprocess
import threading
from functools import partial
from unittest import mock

import pytest

from spi.shell import (
    _MAX_PARALLEL_READS,
    BatchArgumentError,
    build_batch_command_line,
    escape_batch_argument,
    gather_reads,
    prepare_command,
    run_process,
)


def test_posix_argv_passes_through_untouched():
    command = ["az", "account", "show"]
    with (
        mock.patch("spi.shell.platform.system", return_value="Linux"),
        mock.patch("spi.shell.shutil.which") as which,
    ):
        assert prepare_command(command) is command
    which.assert_not_called()


def test_empty_argv_passes_through():
    assert prepare_command([]) == []


def test_windows_native_executable_resolves_to_argv_list():
    with (
        mock.patch("spi.shell.platform.system", return_value="Windows"),
        mock.patch("spi.shell.shutil.which", return_value=r"C:\tools\kubectl.exe"),
    ):
        assert prepare_command(["kubectl", "get", "pods"]) == [
            r"C:\tools\kubectl.exe",
            "get",
            "pods",
        ]


def test_windows_unresolvable_program_passes_through():
    command = ["missing-tool", "--version"]
    with (
        mock.patch("spi.shell.platform.system", return_value="Windows"),
        mock.patch("spi.shell.shutil.which", return_value=None),
    ):
        assert prepare_command(command) == command


def test_windows_batch_shim_becomes_escaped_command_line():
    with (
        mock.patch("spi.shell.platform.system", return_value="Windows"),
        mock.patch("spi.shell.shutil.which", return_value=r"C:\tools\az.CMD"),
        mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}, clear=True),
    ):
        command = prepare_command(["az", "bicep", "build", "--file", r"C:\src\a&b\main.bicep"])

    assert command == (
        r'"C:\Windows\System32\cmd.exe" /e:ON /v:OFF /d '
        r'/c ""C:\tools\az.CMD" "bicep" "build" "--file" "C:\src\a&b\main.bicep""'
    )


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("build", '"build"'),
        ("", '""'),
        ("a b", '"a b"'),
        ("a&b", '"a&b"'),
        ("caret^pipe|redirect<out>paren()", '"caret^pipe|redirect<out>paren()"'),
        ("100%PATH%", '"100%%cd:~,%PATH%%cd:~,%"'),
        ("%%", '"%%cd:~,%%%cd:~,%"'),
        ('json={"spec":{"suspend":true}}', '"json={""spec"":{""suspend"":true}}"'),
        ('say "hi', '"say ""hi"'),
        ('a\\"b', '"a\\\\""b"'),
        ("C:\\tmp\\", '"C:\\tmp\\\\"'),
        ("--from-literal=k=v", '"--from-literal=k=v"'),
    ],
)
def test_escape_batch_argument(argument: str, expected: str):
    assert escape_batch_argument(argument) == expected


@pytest.mark.parametrize("argument", ["secret\rvalue", "secret\nvalue", "secret\0value"])
def test_escape_batch_argument_rejects_unrepresentable_characters(argument: str):
    with pytest.raises(BatchArgumentError) as raised:
        escape_batch_argument(argument)
    # The message must never echo the value: it may be a secret.
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize("script", ["", 'C:\\bad"name.cmd', "C:\\bad\\"])
def test_build_batch_command_line_rejects_unusable_shim_path(script: str):
    with pytest.raises(BatchArgumentError, match="shim path"):
        build_batch_command_line(script, [])


def test_build_batch_command_line_escapes_percent_in_shim_path():
    with mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}, clear=True):
        command = build_batch_command_line(r"C:\tools\100%PATH%\az.cmd", ["arg"])
    assert '""C:\\tools\\100%%cd:~,%PATH%%cd:~,%\\az.cmd"' in command


def test_build_batch_command_line_falls_back_for_relative_system_root():
    with mock.patch.dict(os.environ, {"SystemRoot": "Windows"}, clear=True):
        command = build_batch_command_line(r"C:\tools\az.cmd", [])
    assert command.startswith(r'"C:\Windows\System32\cmd.exe" ')


def test_run_process_reports_unrepresentable_argument_as_failed_launch():
    with (
        mock.patch("spi.shell.platform.system", return_value="Windows"),
        mock.patch("spi.shell.shutil.which", return_value=r"C:\tools\az.cmd"),
        mock.patch("spi.shell.subprocess.run") as run,
    ):
        result = run_process(["az", "bad\nvalue"], capture_output=True, text=True)

    run.assert_not_called()
    assert result.returncode == 1
    assert "newline or NUL" in result.stderr
    assert "bad" not in result.stderr


def test_run_process_reports_unrepresentable_argument_as_bytes_by_default():
    with (
        mock.patch("spi.shell.platform.system", return_value="Windows"),
        mock.patch("spi.shell.shutil.which", return_value=r"C:\tools\az.cmd"),
        mock.patch("spi.shell.subprocess.run") as run,
    ):
        result = run_process(["az", "bad\nvalue"], capture_output=True)

    run.assert_not_called()
    assert result.stdout == b""
    assert b"newline or NUL" in result.stderr


def test_run_process_forwards_kwargs_to_subprocess():
    completed = subprocess.CompletedProcess(["kubectl"], 0)
    with (
        mock.patch("spi.shell.prepare_command", return_value=["kubectl", "get"]) as prepare,
        mock.patch("spi.shell.subprocess.run", return_value=completed) as run,
    ):
        result = run_process(["kubectl", "get"], capture_output=True, text=True, timeout=10)

    assert result is completed
    prepare.assert_called_once_with(["kubectl", "get"])
    run.assert_called_once_with(["kubectl", "get"], capture_output=True, text=True, timeout=10)


def test_gather_reads_returns_results_in_call_order():
    """Results follow the call list, not completion order. The first call is
    held until the second has finished, so completion order is the reverse."""
    second_ran = threading.Event()

    def first():
        assert second_ran.wait(timeout=10), "the second call never ran; reads were serial"
        return "first"

    def second():
        second_ran.set()
        return "second"

    assert gather_reads([first, second]) == ["first", "second"]


def test_gather_reads_raises_the_earliest_failure_in_call_order():
    """A later read failing first must not mask an earlier one. This is what
    `collect_status` relies on to keep a Kustomization error ahead of the
    reads that follow it."""
    second_failed = threading.Event()

    def first():
        assert second_failed.wait(timeout=10), "the second call never ran; reads were serial"
        raise RuntimeError("first")

    def second():
        second_failed.set()
        raise RuntimeError("second")

    with pytest.raises(RuntimeError, match="^first$"):
        gather_reads([first, second])


def test_gather_reads_runs_exactly_the_worker_cap_at_once():
    """One more call than the cap: while every started call is blocked, the
    extra one has no worker to run on, so exactly the cap is ever in flight.
    """
    started = threading.Semaphore(0)
    release = threading.Event()
    lock = threading.Lock()
    live = 0

    def probe():
        nonlocal live
        with lock:
            live += 1
        started.release()
        assert release.wait(timeout=10), "the test never released the workers"
        with lock:
            live -= 1

    results: list = []
    runner = threading.Thread(
        target=lambda: results.extend(gather_reads([probe] * (_MAX_PARALLEL_READS + 1)))
    )
    runner.start()
    try:
        for _ in range(_MAX_PARALLEL_READS):
            assert started.acquire(timeout=10), "fewer calls ran at once than the cap allows"
        assert not started.acquire(timeout=0.5), "more calls ran at once than the cap allows"
        with lock:
            assert live == _MAX_PARALLEL_READS
    finally:
        release.set()
        runner.join(timeout=10)

    assert len(results) == _MAX_PARALLEL_READS + 1


def test_gather_reads_runs_a_lone_call_without_a_pool():
    """One read is not worth a thread; it stays on the calling thread."""
    assert gather_reads([threading.current_thread]) == [threading.current_thread()]
    assert gather_reads([]) == []


def test_gather_reads_passes_no_arguments_to_its_calls():
    """Callers bind their arguments up front, so a read takes none."""
    assert gather_reads([partial(len, "abcd"), partial(len, "ab")]) == [4, 2]
