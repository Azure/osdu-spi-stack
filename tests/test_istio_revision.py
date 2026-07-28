# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Tests for publishing the managed Istio revision to Flux substitution."""

import subprocess
from unittest import mock

import pytest

from spi import ingress


def _result(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["kubectl"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


def test_publish_keeps_an_existing_revision():
    payload = {"data": {"ISTIO_REVISION": "asm-1-30", "INGRESS_MODE": "azure"}}
    with (
        mock.patch.object(ingress, "kubectl_json", return_value=payload),
        mock.patch.object(ingress, "run_command") as run,
    ):
        assert ingress.ensure_istio_revision_published() == "asm-1-30"

    run.assert_not_called()


def test_publish_backfills_a_missing_revision():
    payload = {"data": {"INGRESS_MODE": "azure"}}
    with (
        mock.patch.object(ingress, "kubectl_json", return_value=payload),
        mock.patch("spi.bootstrap.detect_istio_revision", return_value="asm-1-30"),
        mock.patch.object(ingress, "run_command", return_value=_result()) as run,
    ):
        assert ingress.ensure_istio_revision_published() == "asm-1-30"

    patch_command = run.call_args.args[0]
    assert "patch" in patch_command
    assert '"ISTIO_REVISION": "asm-1-30"' in patch_command[-1]


def test_publish_backfills_an_empty_revision():
    payload = {"data": {"ISTIO_REVISION": ""}}
    with (
        mock.patch.object(ingress, "kubectl_json", return_value=payload),
        mock.patch("spi.bootstrap.detect_istio_revision", return_value="asm-1-28"),
        mock.patch.object(ingress, "run_command", return_value=_result()) as run,
    ):
        assert ingress.ensure_istio_revision_published() == "asm-1-28"

    run.assert_called_once()


def test_publish_requires_the_bootstrap_configmap():
    with (
        mock.patch.object(ingress, "kubectl_json", return_value=None),
        mock.patch.object(ingress, "run_command") as run,
    ):
        with pytest.raises(RuntimeError, match="spi-ingress-config"):
            ingress.ensure_istio_revision_published()

    run.assert_not_called()
