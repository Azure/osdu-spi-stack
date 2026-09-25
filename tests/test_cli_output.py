# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Shell completion options and the closing output of `spi up`."""

import re

import pytest
from typer.testing import CliRunner

from spi import cli

runner = CliRunner()
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI styling; Rich splits option names into styled runs under CI."""
    return _ANSI.sub("", text)


def test_help_lists_the_completion_options():
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "--install-completion" in _plain(result.stdout)
    assert "--show-completion" in _plain(result.stdout)


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
def test_install_completion_refuses_powershell(monkeypatch, shell):
    monkeypatch.setattr(cli.shellingham, "detect_shell", lambda: (shell, shell))
    monkeypatch.setattr(
        cli, "install_callback", lambda *a: pytest.fail("PowerShell must not reach Typer")
    )

    result = runner.invoke(cli.app, ["--install-completion"])

    assert result.exit_code == 1
    assert "--show-completion" in _plain(result.stdout)
    assert "$PROFILE" in _plain(result.stdout)


def test_install_completion_refuses_an_undetected_shell(monkeypatch):
    def detect_shell():
        raise cli.shellingham.ShellDetectionFailure()

    monkeypatch.setattr(cli.shellingham, "detect_shell", detect_shell)
    monkeypatch.setattr(cli, "install_callback", lambda *a: pytest.fail("must not install"))

    result = runner.invoke(cli.app, ["--install-completion"])

    assert result.exit_code == 1
    assert "Could not detect your shell" in _plain(result.stdout)


def test_install_completion_forwards_the_vetted_shell(monkeypatch):
    forwarded = []

    def install_callback(ctx, param, value):
        forwarded.append(value)
        raise SystemExit(0)

    monkeypatch.setattr(cli.shellingham, "detect_shell", lambda: ("zsh", "/bin/zsh"))
    monkeypatch.setattr(cli, "install_callback", install_callback)

    result = runner.invoke(cli.app, ["--install-completion"])

    assert result.exit_code == 0
    assert forwarded == ["zsh"]


def _run_up(monkeypatch, *args):
    monkeypatch.delenv("SPI_INGRESS_MODE", raising=False)
    monkeypatch.setattr(cli, "check_prerequisites", lambda tools: None)
    monkeypatch.setattr(
        cli, "_resolve_up_context", lambda env, requested_suffix=None: ("abc12", {}, ("", ""))
    )
    monkeypatch.setattr("spi.deploy.deploy_azure", lambda config, **kwargs: None)
    return runner.invoke(cli.app, ["up", "--env", "dev1", *args], terminal_width=200)


def test_up_next_steps_name_the_teardown_command(monkeypatch):
    result = _run_up(monkeypatch)

    assert result.exit_code == 0, result.output
    assert "spi down --env dev1" in _plain(result.stdout)


def test_up_branch_hint_says_reconcile_pulls_the_branch_head(monkeypatch):
    result = _run_up(monkeypatch, "--branch", "feat/x")

    assert result.exit_code == 0, result.output
    assert "pinned to the resolved commit on feat/x" in _plain(result.stdout)
    assert "latest commit on the branch" in _plain(result.stdout)
    assert "re-apply" not in _plain(result.stdout)
