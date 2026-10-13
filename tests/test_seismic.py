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

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from spi import azure_infra
from spi.bootstrap import deploy_identity_facts
from spi.config import Config

REPO_ROOT = Path(__file__).resolve().parent.parent
DDMS = REPO_ROOT / "software" / "stacks" / "osdu" / "services-ddms"
CORE_STACK = REPO_ROOT / "software" / "stacks" / "osdu" / "profiles" / "core" / "stack.yaml"
INFRA = REPO_ROOT / "infra"
PARTITION = INFRA / "modules" / "partition.bicep"
IDENTITY = INFRA / "modules" / "identity.bicep"
RBAC = INFRA / "modules" / "rbac.bicep"
COMMON_STORAGE = INFRA / "modules" / "storage-common.bicep"


def _release(name: str) -> dict:
    return yaml.safe_load((DDMS / f"{name}.yaml").read_text(encoding="utf-8"))


def _env(release: dict) -> dict[str, str]:
    return {
        item["name"]: item["value"] for item in release["spec"]["values"]["env"] if "value" in item
    }


def _result(returncode: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_api_uses_workload_identity_and_gates_restore():
    release = _release("seismic")
    values = release["spec"]["values"]
    env = _env(release)

    assert values["workloadIdentity"] is True
    assert env["AZURE_MSI_ISENABLED"] == "false"
    assert env["FEATURE_FLAG_ENABLE_BULK_DELETE"] == "false"
    assert env["FEATURE_FLAG_ENABLE_CHANGE_TIER"] == "false"
    assert env["FEATURE_FLAG_ENABLE_ANALYTICS"] == "false"
    assert env["FEATURE_FLAG_ENABLE_RESTORE"] == "${ENABLE_SEISMIC_RESTORE:=false}"


def test_restore_release_uses_its_dedicated_identity_and_is_inert_by_default():
    release = _release("seismic-restore")
    values = release["spec"]["values"]
    env = _env(release)

    assert values["replicaCount"] == "${SEISMIC_RESTORE_REPLICAS:=0}"
    assert values["workloadIdentity"] is True
    assert values["serviceAccountName"] == "seismic-restore-sa"
    assert env["SDMS_RESTORE_QUEUE"] == "sdms-queue-restore"
    assert env["SDMS_RESTORE_LOCK_TTL_SECONDS"] == "7200"
    assert env["SDMS_RESTORE_MAX_DEQUEUE_COUNT"] == "3"

    assert values["serviceAccount"]["create"] is True
    assert values["serviceAccount"]["annotations"] == {
        "azure.workload.identity/client-id": "${SEISMIC_RESTORE_IDENTITY_CLIENT_ID}",
        "azure.workload.identity/tenant-id": "${AZURE_TENANT_ID}",
    }


def test_ddms_flux_inventory_substitutes_cluster_and_image_configuration():
    documents = list(yaml.safe_load_all(CORE_STACK.read_text(encoding="utf-8")))
    ddms = next(
        document for document in documents if document["metadata"]["name"] == "spi-osdu-ddms"
    )
    sources = ddms["spec"]["postBuild"]["substituteFrom"]

    assert sources == [
        {"kind": "ConfigMap", "name": "osdu-image-lock"},
        {"kind": "ConfigMap", "name": "spi-cluster-config"},
    ]


def test_restore_infrastructure_is_conditioned_on_one_gate():
    identity = IDENTITY.read_text(encoding="utf-8")
    partition = PARTITION.read_text(encoding="utf-8")
    rbac = RBAC.read_text(encoding="utf-8")
    common_storage = COMMON_STORAGE.read_text(encoding="utf-8")

    assert "resource restoreIdentity " in identity
    assert "= if (enableSeismicRestore)" in identity
    assert "system:serviceaccount:osdu:seismic-restore-sa" in identity
    assert "RestoreOperationStatus" in partition
    assert "ArchiveDatasetMetadata" in partition
    assert "restoreIdentitySqlDataContributor" in partition
    assert "isVersioningEnabled: false" in partition
    assert "restorePolicy:" in partition
    assert "resource restoreQueue " in common_storage
    assert "storageBlobRestoreOperatorRole" in rbac
    assert "Microsoft.Storage/storageAccounts/blobServices/restoreBlobRanges/action" in rbac
    assert "restoreCommonStorageQueueAssignment" in rbac
    assert "restorePartitionStorageBlobAssignments" in rbac


@pytest.mark.parametrize("enabled", [False, True])
def test_restore_gate_reaches_bicep_parameters(enabled: bool):
    config = Config.from_env("dev1", enable_seismic_restore=enabled)

    parameters = azure_infra._build_bicep_params(
        config,
        "https://issuer.example/",
        "deployer",
        "ServicePrincipal",
    )

    assert parameters["restoreIdentityName"] == "spi-stack-dev1-seismic-restore"
    assert parameters["enableSeismicRestore"] is enabled


def test_restore_identity_output_reaches_cluster_configuration():
    outputs = azure_infra._reshape_bicep_outputs({"restoreIdentityClientId": "restore-client-id"})
    facts = deploy_identity_facts(outputs, "spi-stack-dev1", "spi-stack-dev1")

    assert outputs["restore_identity_client_id"] == "restore-client-id"
    assert facts["SEISMIC_RESTORE_IDENTITY_CLIENT_ID"] == "restore-client-id"
    assert facts["ENABLE_SEISMIC_RESTORE"] == "true"
    assert facts["SEISMIC_RESTORE_REPLICAS"] == "1"


def test_absent_restore_identity_keeps_cluster_configuration_disabled():
    facts = deploy_identity_facts({}, "spi-stack-dev1", "spi-stack-dev1")

    assert facts["SEISMIC_RESTORE_IDENTITY_CLIENT_ID"] == ""
    assert facts["ENABLE_SEISMIC_RESTORE"] == "false"
    assert facts["SEISMIC_RESTORE_REPLICAS"] == "0"


def test_existing_restore_setting_is_preserved_from_the_last_deployment():
    responses = [
        _result(0, "true"),
        _result(0, "true"),
    ]
    with patch.object(azure_infra, "run_command", side_effect=responses) as run:
        enabled = azure_infra.read_deployed_seismic_restore("spi-stack-dev1", "dev1")

    assert enabled is True
    assert run.call_args_list[1].args[0][1:4] == ["deployment", "group", "list"]


def test_new_environment_defaults_restore_to_disabled():
    with patch.object(azure_infra, "run_command", return_value=_result(0, "false")):
        assert azure_infra.read_deployed_seismic_restore("spi-stack-dev1", "dev1") is False


def test_invalid_deployed_restore_setting_fails_explicitly():
    responses = [_result(0, "true"), _result(0, "unexpected")]
    with (
        patch.object(azure_infra, "run_command", side_effect=responses),
        pytest.raises(RuntimeError, match="invalid enableSeismicRestore"),
    ):
        azure_infra.read_deployed_seismic_restore("spi-stack-dev1", "dev1")
