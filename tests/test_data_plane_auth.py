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

"""Guards the Entra-only data-plane posture (ADR-027).

Local (key/SAS) authentication must stay disabled on every Cosmos DB and
Service Bus account, and no Cosmos key material may be resolved into Key Vault.
These are text assertions over the Bicep sources, so they run without the
Azure CLI and fail fast if the compliant posture regresses.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INFRA_DIR = REPO_ROOT / "infra"

GREMLIN = INFRA_DIR / "modules" / "cosmos-gremlin.bicep"
PARTITION = INFRA_DIR / "modules" / "partition.bicep"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_gremlin_disables_local_auth():
    assert "disableLocalAuth: true" in _read(GREMLIN)


def test_partition_disables_local_auth_on_cosmos_and_service_bus():
    # One occurrence for the Cosmos SQL account, one for the Service Bus namespace.
    assert _read(PARTITION).count("disableLocalAuth: true") >= 2


def test_no_cosmos_key_material_written():
    # listKeys() on a Cosmos account is rejected once local auth is disabled;
    # guard against a primary-key write creeping back onto any account.
    offenders = [
        p.relative_to(REPO_ROOT)
        for p in INFRA_DIR.rglob("*.bicep")
        if "primaryMasterKey" in _read(p)
    ]
    assert not offenders, f"Cosmos key material resolved in: {offenders}"


def test_service_bus_local_auth_not_parameterized():
    # The per-tenant knob is gone; local auth is hardcoded off everywhere.
    offenders = [
        p.relative_to(REPO_ROOT)
        for p in INFRA_DIR.rglob("*.bicep")
        if "serviceBusDisableLocalAuth" in _read(p)
    ]
    assert not offenders, f"serviceBusDisableLocalAuth still present in: {offenders}"


def test_graph_db_primary_key_secret_not_written():
    offenders = [
        p.relative_to(REPO_ROOT)
        for p in INFRA_DIR.rglob("*.bicep")
        if "graph-db-primary-key" in _read(p)
    ]
    assert not offenders, f"graph-db-primary-key still written in: {offenders}"
