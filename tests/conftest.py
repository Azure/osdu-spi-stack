# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Shared fixtures for the CLI test suite."""

import pytest

from spi import cli


@pytest.fixture(autouse=True)
def _offline_environment_facts(monkeypatch):
    """Keep service confirmations from reading a live deploy record.

    The pin, verify and reset commands name the environment they acted on
    by reading the deploy record after the operation. Tests that stub the
    operation would otherwise reach a real kubectl. Returns the real
    function for the few tests that exercise it.
    """
    original = cli._environment_facts
    monkeypatch.setattr(
        cli,
        "_environment_facts",
        lambda: {
            "name": "test",
            "stackVersion": "v0.0.0",
            "resolvedCommit": "",
            "profile": "core",
            "deployedAt": "",
            "cliVersion": "0.0.0",
        },
    )
    return original


@pytest.fixture
def real_environment_facts(_offline_environment_facts, monkeypatch):
    monkeypatch.setattr(cli, "_environment_facts", _offline_environment_facts)
