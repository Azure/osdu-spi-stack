# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Tests for persistent Automatic/Base AKS topology selection."""

import json
import subprocess
from unittest import mock

import pytest

from spi import azure_infra, cli
from spi.config import RG_AKS_MODE_TAG, AksMode, Config


def _result(
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["az"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_config_defaults_to_automatic():
    assert Config.from_env("dev1").aks_mode == AksMode.AUTOMATIC


def test_resolve_aks_mode_defaults_new_environment_to_automatic():
    with (
        mock.patch.object(azure_infra, "read_rg_aks_mode_tag", return_value=None),
        mock.patch.object(azure_infra, "detect_existing_aks_mode") as detect,
        mock.patch.object(azure_infra, "write_rg_aks_mode_tag") as write,
        mock.patch.object(cli, "run_command", return_value=_result("false")),
    ):
        mode = cli._resolve_aks_mode("dev1", requested=None, for_up=True)

    assert mode == AksMode.AUTOMATIC
    detect.assert_not_called()
    write.assert_not_called()


def test_resolve_aks_mode_honors_explicit_base_for_new_environment():
    with (
        mock.patch.object(azure_infra, "read_rg_aks_mode_tag", return_value=None),
        mock.patch.object(azure_infra, "detect_existing_aks_mode") as detect,
        mock.patch.object(azure_infra, "write_rg_aks_mode_tag") as write,
        mock.patch.object(cli, "run_command", return_value=_result("false")),
    ):
        mode = cli._resolve_aks_mode("dev1", requested=AksMode.BASE, for_up=True)

    assert mode == AksMode.BASE
    detect.assert_not_called()
    write.assert_not_called()


def test_resolve_aks_mode_preserves_persisted_mode():
    with (
        mock.patch.object(
            azure_infra,
            "read_rg_aks_mode_tag",
            return_value=AksMode.BASE,
        ),
        mock.patch.object(azure_infra, "detect_existing_aks_mode", return_value=AksMode.BASE),
        mock.patch.object(cli, "run_command", return_value=_result("true")),
    ):
        mode = cli._resolve_aks_mode("dev1", requested=None, for_up=True)

    assert mode == AksMode.BASE


def test_resolve_aks_mode_rejects_in_place_change():
    with (
        mock.patch.object(
            azure_infra,
            "read_rg_aks_mode_tag",
            return_value=AksMode.BASE,
        ),
        mock.patch.object(azure_infra, "detect_existing_aks_mode", return_value=AksMode.BASE),
        mock.patch.object(cli, "run_command", return_value=_result("true")),
    ):
        with pytest.raises(RuntimeError, match="cannot be changed in place"):
            cli._resolve_aks_mode(
                "dev1",
                requested=AksMode.AUTOMATIC,
                for_up=True,
            )


def test_resolve_aks_mode_infers_and_tags_legacy_cluster():
    with (
        mock.patch.object(azure_infra, "read_rg_aks_mode_tag", return_value=None),
        mock.patch.object(
            azure_infra,
            "detect_existing_aks_mode",
            return_value=AksMode.BASE,
        ),
        mock.patch.object(azure_infra, "write_rg_aks_mode_tag") as write,
        mock.patch.object(cli, "run_command", return_value=_result("true")),
    ):
        mode = cli._resolve_aks_mode("dev1", requested=None, for_up=True)

    assert mode == AksMode.BASE
    write.assert_called_once_with("spi-stack-dev1", AksMode.BASE)


def test_resolve_aks_mode_rejects_tag_cluster_mismatch():
    with (
        mock.patch.object(
            azure_infra,
            "read_rg_aks_mode_tag",
            return_value=AksMode.AUTOMATIC,
        ),
        mock.patch.object(
            azure_infra,
            "detect_existing_aks_mode",
            return_value=AksMode.BASE,
        ),
        mock.patch.object(cli, "run_command", return_value=_result("true")),
    ):
        with pytest.raises(RuntimeError, match="tagged for AKS automatic"):
            cli._resolve_aks_mode("dev1", requested=None, for_up=True)


def test_aks_mode_classifier_requires_nap_for_base():
    assert azure_infra._aks_mode_from_cluster({"sku": {"name": "Automatic"}}) == AksMode.AUTOMATIC
    assert (
        azure_infra._aks_mode_from_cluster(
            {
                "sku": {"name": "Base"},
                "nodeProvisioningProfile": {"mode": "Auto"},
            }
        )
        == AksMode.BASE
    )
    with pytest.raises(RuntimeError, match="without Node Autoprovisioning"):
        azure_infra._aks_mode_from_cluster({"sku": {"name": "Base"}})


@pytest.mark.parametrize(
    ("mode", "template_name"),
    [
        (AksMode.AUTOMATIC, "aks.bicep"),
        (AksMode.BASE, "aks-base.bicep"),
    ],
)
def test_create_aks_dry_run_selects_mode_template(mode, template_name):
    cfg = Config.from_env("dev1", aks_mode=mode)
    with mock.patch.object(
        azure_infra,
        "run_bicep_deployment",
        return_value={},
    ) as deploy:
        assert azure_infra.create_aks(cfg, dry_run=True) == {}

    assert deploy.call_args.kwargs["template_path"].endswith(template_name)


def test_create_resource_group_persists_aks_mode_tag():
    cfg = Config.from_env("dev1", name_suffix="abc12", aks_mode=AksMode.BASE)
    with mock.patch.object(
        azure_infra,
        "run_command",
        side_effect=[_result("false"), _result("{}")],
    ) as run:
        azure_infra.create_resource_group(cfg)

    create_command = run.call_args_list[1].args[0]
    assert f"{RG_AKS_MODE_TAG}=base" in create_command


def test_dry_run_resource_group_does_not_persist_aks_mode_tag():
    cfg = Config.from_env("dev1", name_suffix="abc12", aks_mode=AksMode.BASE)
    with mock.patch.object(
        azure_infra,
        "run_command",
        side_effect=[_result("false"), _result("{}")],
    ) as run:
        azure_infra.create_resource_group(cfg, persist_aks_mode=False)

    create_command = run.call_args_list[1].args[0]
    assert all(not arg.startswith(f"{RG_AKS_MODE_TAG}=") for arg in create_command)


def test_detect_existing_aks_mode_reads_cluster_shape():
    payload = {
        "sku": {"name": "Base"},
        "nodeProvisioningProfile": {"mode": "Auto"},
    }
    with mock.patch.object(
        azure_infra,
        "run_command",
        return_value=_result(json.dumps(payload)),
    ):
        mode = azure_infra.detect_existing_aks_mode("rg", "cluster")

    assert mode == AksMode.BASE
