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

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DDMS = REPO_ROOT / "software" / "stacks" / "osdu" / "services-ddms"
PARTITION = REPO_ROOT / "infra" / "modules" / "partition.bicep"


def _release(name: str) -> dict:
    return yaml.safe_load((DDMS / f"{name}.yaml").read_text(encoding="utf-8"))


def _env(release: dict) -> dict[str, str]:
    return {item["name"]: item["value"] for item in release["spec"]["values"]["env"]}


def test_api_uses_workload_identity_and_keeps_optional_features_off():
    release = _release("seismic")
    values = release["spec"]["values"]
    env = _env(release)

    assert values["workloadIdentity"] is True
    assert env["AZURE_MSI_ISENABLED"] == "false"
    assert env["FEATURE_FLAG_ENABLE_BULK_DELETE"] == "false"
    assert env["FEATURE_FLAG_ENABLE_CHANGE_TIER"] == "false"
    assert env["FEATURE_FLAG_ENABLE_ANALYTICS"] == "false"
    assert env["FEATURE_FLAG_ENABLE_RESTORE"] == "false"


def test_restore_release_is_inert_by_default():
    release = _release("seismic-restore")
    assert release["spec"]["values"]["replicaCount"] == 0


def test_api_data_plane_excludes_restore_resources():
    source = PARTITION.read_text(encoding="utf-8")

    assert "resource sdmsDb " in source
    assert "{ name: 'data', partitionKey: '/id' }" in source
    assert "RestoreOperationStatus" not in source
    assert "ArchiveDatasetMetadata" not in source
    assert "changeFeed:" not in source
    assert "isVersioningEnabled:" not in source
    assert "restorePolicy:" not in source
