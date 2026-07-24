# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for existing-cluster output reads on idempotent ``spi up`` re-runs."""

import json
import subprocess
from unittest import mock

from spi import azure_infra
from spi.config import Config


def _fake_result(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["az", "aks", "show"],
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )


def test_existing_aks_outputs_includes_kubelet_identity_object_id():
    cfg = Config(env="dev1")
    cluster = {
        "name": cfg.cluster_name,
        "id": "/subscriptions/s/resourceGroups/rg/providers/x/managedClusters/c",
        "location": "eastus2",
        "provisioningState": "Succeeded",
        "sku": {"name": "Automatic"},
        "oidcIssuerProfile": {"issuerUrl": "https://oidc"},
        "identity": {"userAssignedIdentities": {"/id": {"principalId": "cluster-pid"}}},
        "identityProfile": {"kubeletidentity": {"objectId": "kubelet-oid"}},
        "serviceMeshProfile": {"istio": {"revisions": ["asm-1-30"]}},
    }
    with mock.patch.object(azure_infra, "run_command", return_value=_fake_result(cluster)):
        out = azure_infra._existing_aks_outputs(cfg)

    assert out is not None
    # The re-run path must surface the kubelet identity so the AcrPull grant is
    # not silently skipped against an already-existing cluster.
    assert out["kubeletIdentityObjectId"] == "kubelet-oid"
    assert out["clusterPrincipalId"] == "cluster-pid"
    assert out["istioRevision"] == "asm-1-30"


def test_existing_aks_outputs_kubelet_id_empty_when_absent():
    cfg = Config(env="dev1")
    cluster = {
        "name": cfg.cluster_name,
        "location": "eastus2",
        "provisioningState": "Succeeded",
        "sku": {"name": "Automatic"},
    }
    with mock.patch.object(azure_infra, "run_command", return_value=_fake_result(cluster)):
        out = azure_infra._existing_aks_outputs(cfg)

    assert out is not None
    assert out["kubeletIdentityObjectId"] == ""
