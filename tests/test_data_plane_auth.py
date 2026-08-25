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

"""Guards the Entra-only data-plane posture (ADR-023).

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


def _code(path: Path) -> str:
    # Bicep source with // comment lines stripped, so prose mentioning
    # listKeys() does not trip the assertions below.
    lines = _read(path).splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("//"))


def _resource_block(path: Path, symbolic_name: str) -> str:
    # The chunk from `resource <symbolic_name>` to the next resource declaration.
    chunks = _code(path).split("\nresource ")
    matches = [c for c in chunks if c.startswith(f"{symbolic_name} ")]
    assert matches, f"resource {symbolic_name} not found in {path.name}"
    return matches[0]


def test_gremlin_disables_local_auth():
    assert "disableLocalAuth: true" in _resource_block(GREMLIN, "gremlinAccount")


def test_partition_disables_local_auth_on_cosmos_and_service_bus():
    assert "disableLocalAuth: true" in _resource_block(PARTITION, "cosmosAccount")
    assert "disableLocalAuth: true" in _resource_block(PARTITION, "serviceBusNamespace")


def test_no_key_material_resolved_anywhere():
    # listKeys() on a Cosmos account is rejected once local auth is disabled,
    # and no other account (Service Bus, Storage) may resolve keys either.
    offenders = [
        p.relative_to(REPO_ROOT)
        for p in INFRA_DIR.rglob("*.bicep")
        if "listKeys(" in _code(p) or "primaryMasterKey" in _code(p)
    ]
    assert not offenders, f"key material resolved in: {offenders}"


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
        if "graph-db-primary-key" in _code(p)
    ]
    assert not offenders, f"graph-db-primary-key still written in: {offenders}"
