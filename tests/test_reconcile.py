# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for CI-mode freeze (spi reconcile --suspend/--resume)."""

from unittest import mock

import pytest

from spi import cli, guard
from spi.images import ImageSource


def test_resolve_flux_namespace_reads_gitrepository_namespace():
    data = {"items": [{"metadata": {"name": "osdu-spi-stack-system", "namespace": "osdu-flux"}}]}
    with mock.patch.object(guard, "kubectl_json", return_value=data):
        assert guard.resolve_flux_namespace() == "osdu-flux"


def test_resolve_flux_namespace_ignores_other_gitrepositories():
    data = {
        "items": [
            {"metadata": {"name": "some-other", "namespace": "flux-system"}},
            {"metadata": {"name": "osdu-spi-stack-system", "namespace": "osdu-flux"}},
        ]
    }
    with mock.patch.object(guard, "kubectl_json", return_value=data):
        assert guard.resolve_flux_namespace() == "osdu-flux"


def test_resolve_flux_namespace_falls_back_when_absent():
    with mock.patch.object(guard, "kubectl_json", return_value=None):
        assert guard.resolve_flux_namespace(default="flux-system") == "flux-system"


def test_set_flux_suspend_freezes_gitrepository_kustomizations_and_helmreleases():
    def fake_kubectl_json(args):
        kind = args[1]
        if kind == "kustomization":
            return {"items": [{"metadata": {"name": "spi-osdu-services"}}]}
        if kind == "helmrelease":
            return {"items": [{"metadata": {"name": "storage"}}]}
        return None

    calls = []
    with (
        mock.patch.object(cli, "kubectl_json", side_effect=fake_kubectl_json),
        mock.patch.object(cli, "run_command", side_effect=lambda cmd, **kw: calls.append(cmd)),
    ):
        cli._set_flux_suspend("osdu-flux", True)

    patched = {(c[2], c[3]) for c in calls}
    assert ("gitrepository", "osdu-spi-stack-system") in patched
    assert ("kustomization", "spi-osdu-services") in patched
    assert ("helmrelease", "storage") in patched
    # every patch targets the resolved namespace and sets suspend:true
    for cmd in calls:
        assert "osdu-flux" in cmd
        assert '{"spec":{"suspend":true}}' in cmd


def test_set_flux_suspend_resume_sets_false():
    with mock.patch.object(cli, "kubectl_json", return_value={"items": []}):
        calls = []
        with mock.patch.object(cli, "run_command", side_effect=lambda cmd, **kw: calls.append(cmd)):
            cli._set_flux_suspend("osdu-flux", False)
    # only the GitRepository (no kustomizations/helmreleases returned), suspend:false
    assert any(c[2] == "gitrepository" for c in calls)
    for cmd in calls:
        assert '{"spec":{"suspend":false}}' in cmd


def test_flux_resource_names_returns_empty_on_no_items():
    # A successful query with no resources yields [], not an error.
    with mock.patch.object(cli, "kubectl_json", return_value={"items": []}):
        assert cli._flux_resource_names("helmrelease", "osdu-flux") == []


def test_flux_resource_names_raises_on_kubectl_error():
    # kubectl_json returns None only on command failure; a failed listing must
    # NOT be treated as "no resources", or CI-mode suspend would report a
    # successful freeze while HelmReleases keep reconciling (ADR-032).
    import pytest

    with mock.patch.object(cli, "kubectl_json", return_value=None):
        with pytest.raises(RuntimeError, match="Failed to list Flux helmrelease"):
            cli._flux_resource_names("helmrelease", "osdu-flux")


def test_set_flux_suspend_raises_when_listing_fails():
    # If the helmrelease listing fails mid-freeze, the whole operation must fail
    # loud rather than silently leaving HelmReleases reconciling.
    import pytest

    def fake_kubectl_json(args):
        if args[1] == "kustomization":
            return {"items": [{"metadata": {"name": "spi-osdu-services"}}]}
        return None  # helmrelease listing fails

    with (
        mock.patch.object(cli, "kubectl_json", side_effect=fake_kubectl_json),
        mock.patch.object(cli, "run_command", side_effect=lambda cmd, **kw: None),
    ):
        with pytest.raises(RuntimeError, match="Failed to list Flux helmrelease"):
            cli._set_flux_suspend("osdu-flux", True)


def test_legacy_image_branch_selects_community_images():
    assert cli._resolve_image_selection(
        image_source=None,
        image_org=None,
        image_tag=None,
        image_ref=None,
        image_branch="master",
    ) == (ImageSource.COMMUNITY, "", "", "master")


def test_image_refresh_preserves_current_selection_when_options_omitted():
    current = (
        ImageSource.GHCR,
        "yuchen-osdu",
        "",
        "fix/core-lib-azure-3.0.1",
    )

    assert (
        cli._resolve_image_selection(
            image_source=None,
            image_org=None,
            image_tag=None,
            image_ref=None,
            image_branch=None,
            current=current,
        )
        == current
    )


def test_image_source_change_uses_new_source_defaults():
    assert cli._resolve_image_selection(
        image_source=ImageSource.COMMUNITY,
        image_org=None,
        image_tag=None,
        image_ref=None,
        image_branch=None,
        current=(ImageSource.GHCR, "yuchen-osdu", "", "feature/ref"),
    ) == (ImageSource.COMMUNITY, "", "", "master")


def test_ghcr_defaults_to_main_snapshot():
    assert cli._resolve_image_selection(
        image_source=None,
        image_org=None,
        image_tag=None,
        image_ref=None,
        image_branch=None,
    ) == (ImageSource.GHCR, "yuchen-osdu", "main-snapshot", "")


def test_explicit_tag_replaces_feature_ref():
    assert cli._resolve_image_selection(
        image_source=None,
        image_org=None,
        image_tag="v1.2.3",
        image_ref=None,
        image_branch=None,
        current=(ImageSource.GHCR, "yuchen-osdu", "", "feature/ref"),
    ) == (ImageSource.GHCR, "yuchen-osdu", "v1.2.3", "")


@pytest.mark.parametrize(
    ("option", "kwargs"),
    [
        ("--image-org", {"image_org": " "}),
        ("--image-tag", {"image_tag": ""}),
        ("--image-ref", {"image_ref": "  "}),
        ("--image-branch", {"image_branch": ""}),
    ],
)
def test_empty_image_selector_options_are_rejected(option, kwargs):
    options = {
        "image_source": None,
        "image_org": None,
        "image_tag": None,
        "image_ref": None,
        "image_branch": None,
    }
    options.update(kwargs)

    with pytest.raises(ValueError, match=option):
        cli._resolve_image_selection(**options)


def test_read_image_lock_selection_supports_legacy_community_lock():
    configmap = {"data": {"IMAGE_BRANCH": "master"}}
    with mock.patch.object(cli, "kubectl_json", return_value=configmap):
        assert cli._read_image_lock_selection() == (
            ImageSource.COMMUNITY,
            "",
            "",
            "master",
        )


def test_read_image_lock_selection_reads_ghcr_metadata():
    configmap = {
        "data": {
            "IMAGE_SOURCE": "ghcr",
            "IMAGE_ORG": "example",
            "IMAGE_TAG": "",
            "IMAGE_REF": "feature/ref",
        }
    }
    with mock.patch.object(cli, "kubectl_json", return_value=configmap):
        assert cli._read_image_lock_selection() == (
            ImageSource.GHCR,
            "example",
            "",
            "feature/ref",
        )


def test_read_image_lock_selection_reads_ghcr_tag():
    configmap = {
        "data": {
            "IMAGE_SOURCE": "ghcr",
            "IMAGE_ORG": "example",
            "IMAGE_TAG": "main-snapshot",
            "IMAGE_REF": "",
        }
    }
    with mock.patch.object(cli, "kubectl_json", return_value=configmap):
        assert cli._read_image_lock_selection() == (
            ImageSource.GHCR,
            "example",
            "main-snapshot",
            "",
        )
