# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Tests for CLI prerequisite checks."""

import json

from spi import checks
from spi.status import STATUS_API_VERSION


def test_helm_is_not_a_required_local_prerequisite():
    assert "helm" not in checks.TOOL_REGISTRY
    assert "helm" not in checks.PREREQ_TOOLS


def test_spi_up_prerequisite_list_excludes_helm():
    assert set(checks.PREREQ_TOOLS) == {"az", "bicep", "kubectl", "kubelogin", "flux"}


def test_check_json_leads_with_the_shared_api_version():
    payload = json.loads(checks.results_to_json([], azure={"ok": True}))

    assert next(iter(payload)) == "apiVersion"
    assert payload["apiVersion"] == STATUS_API_VERSION
