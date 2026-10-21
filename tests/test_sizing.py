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

"""Every sizing key reaches a manifest Flux substitutes, with the default the CLI assumes."""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from spi import sizing
from spi.images import IMAGE_REGISTRY
from spi.sizing import SIZING_CONFIGMAP, sizing_defaults, sizing_services
from spi.templates import PARTITIONS_HASH_KEY

REPO_ROOT = Path(__file__).resolve().parent.parent
STACKS = REPO_ROOT / "software" / "stacks" / "osdu"
COMPONENTS = REPO_ROOT / "software" / "components"
PROFILES = STACKS / "profiles"

PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*):=([^}]*)\}")
MIDDLEWARE_MANIFESTS = {
    "elasticsearch": "elasticsearch/cluster.yaml",
    "postgres": "postgres/cluster.yaml",
    "redis": "redis/release.yaml",
    "airflow": "airflow/release.yaml",
}


def _manifest(component: str) -> Path:
    if component in sizing_services():
        return STACKS / IMAGE_REGISTRY[component].file
    return COMPONENTS / MIDDLEWARE_MANIFESTS[component]


def _sizing_placeholders(path: Path) -> dict:
    found: dict = {}
    for key, default in PLACEHOLDER.findall(path.read_text(encoding="utf-8")):
        if key.endswith("_DO_NOT_DISRUPT") or key == PARTITIONS_HASH_KEY:
            continue
        assert found.setdefault(key, default) == default, f"{path.name}: {key} has two defaults"
    return found


@pytest.mark.parametrize("component", sorted(sizing_defaults()))
def test_manifest_carries_every_key_with_its_default(component):
    defaults = _sizing_placeholders(_manifest(component))
    if component == "airflow":
        defaults.update(_sizing_placeholders(STACKS / "airflow" / "release-patch.yaml"))
    assert defaults == sizing_defaults()[component]


def test_airflow_fit_checks_use_core_defaults_not_the_middleware_profile():
    values = sizing.effective({})
    assert (
        values["AIRFLOW_SCHEDULER_CPU_REQUEST"],
        values["AIRFLOW_SCHEDULER_CPU_LIMIT"],
        values["AIRFLOW_SCHEDULER_MEMORY_REQUEST"],
        values["AIRFLOW_SCHEDULER_MEMORY_LIMIT"],
    ) == ("1", "2", "2Gi", "4Gi")
    assert (
        values["AIRFLOW_DAG_PROCESSOR_CPU_REQUEST"],
        values["AIRFLOW_DAG_PROCESSOR_CPU_LIMIT"],
        values["AIRFLOW_DAG_PROCESSOR_MEMORY_REQUEST"],
        values["AIRFLOW_DAG_PROCESSOR_MEMORY_LIMIT"],
    ) == ("250m", "1", "512Mi", "2Gi")


def test_every_kustomization_applying_a_sized_manifest_reads_the_sizing_configmap():
    """Optional, so an environment that never wrote the ConfigMap keeps its defaults."""
    sized = {_manifest(component).parent for component in sizing_defaults()}
    reading = set()
    for profile in ("minimal", "core"):
        for doc in yaml.safe_load_all((PROFILES / profile / "stack.yaml").read_text()):
            if not doc or doc.get("kind") != "Kustomization":
                continue
            directory = REPO_ROOT / doc["spec"]["path"].removeprefix("./")
            if directory not in sized:
                continue
            post_build = doc["spec"].get("postBuild") or {}
            sources = post_build.get("substituteFrom", [])
            source = {"kind": "ConfigMap", "name": SIZING_CONFIGMAP, "optional": True}
            assert source in sources, doc["metadata"]["name"]
            # Flux substitutes nothing, defaults included, when it has no variables.
            assert post_build.get("substitute") or any(not s.get("optional") for s in sources), (
                f"{doc['metadata']['name']} has no variable when {SIZING_CONFIGMAP} is absent"
            )
            reading.add(directory)
    assert reading == sized


def test_every_field_an_operator_writes_names_a_key_the_manifests_carry():
    for component, defaults in sizing_defaults().items():
        assert sorted(sizing.field_paths(component).values()) == sorted(defaults), component


def test_the_node_sizes_the_checks_assume_are_the_largest_the_nodepools_allow():
    nodepools = COMPONENTS / "nodepools" / "nodepools.yaml"
    largest = {}
    for doc in yaml.safe_load_all(nodepools.read_text()):
        if doc and doc["kind"] == "NodePool":
            (cpus,) = [
                r["values"]
                for r in doc["spec"]["template"]["spec"]["requirements"]
                if r["key"] == "karpenter.azure.com/sku-cpu"
            ]
            largest[doc["metadata"]["name"]] = max(int(c) for c in cpus) * 1000
    assert largest == {pool: cpu for pool, (cpu, _) in sizing.NODE_SIZES.items()}


def test_the_pool_limits_the_checks_assume_are_the_nodepools():
    nodepools = COMPONENTS / "nodepools" / "nodepools.yaml"
    limits = {
        doc["metadata"]["name"]: doc["spec"]["limits"]
        for doc in yaml.safe_load_all(nodepools.read_text())
        if doc and doc["kind"] == "NodePool"
    }
    assert {
        pool: (sizing.millicores(limit["cpu"]), sizing.to_bytes(limit["memory"]))
        for pool, limit in limits.items()
    } == sizing.POOL_LIMITS


SMALL = {
    "elasticsearch": {"count": 1, "memory": {"request": "2Gi", "limit": "2Gi"}, "storage": "16Gi"},
    "postgres": {"instances": 2, "storage": "4Gi"},
    "partition": {"replicas": 1},
    "airflow": {"scheduler": {"cpu": {"request": "100m"}}},
    "indexer-queue": {"cpu": {"limit": 2}},
}


def test_a_resources_section_becomes_only_the_keys_it_sets():
    assert sizing.overrides(SMALL) == {
        "ES_COUNT": "1",
        "ES_MEMORY_REQUEST": "2Gi",
        "ES_MEMORY_LIMIT": "2Gi",
        "ES_STORAGE": "16Gi",
        "PG_INSTANCES": "2",
        "PG_STORAGE": "4Gi",
        "PARTITION_REPLICAS": "1",
        "AIRFLOW_SCHEDULER_CPU_REQUEST": "100m",
        "INDEXER_QUEUE_CPU_LIMIT": "2",
    }


@pytest.mark.parametrize(
    ("resources", "refusal"),
    [
        ({"kibana": {}}, "unknown component 'kibana'"),
        ({"legal": {"cpu": 1}}, "resources.legal.cpu: unknown field"),
        ({"elasticsearch": {"replicas": 1}}, "resources.elasticsearch.replicas: unknown field"),
        ({"legal": {"replicas": 0}}, "whole number of at least 1"),
        ({"legal": {"replicas": True}}, "whole number of at least 1"),
        ({"legal": {"replicas": "2"}}, "whole number of at least 1"),
        # A bare number renders as an integer, which Postgres refuses as a storage size.
        ({"postgres": {"storage": 21474836480}}, "must be a whole number with a unit"),
        ({"postgres": {"storage": "20G"}}, "must be a whole number with a unit"),
        ({"legal": {"memory": {"limit": "2"}}}, "must be a whole number with a unit"),
        ({"legal": {"cpu": {"limit": "two"}}}, "must be a CPU quantity"),
        # Finer than 1m would round to the limit and pass the comparison.
        ({"legal": {"cpu": {"request": "1.0004"}}}, "must be a CPU quantity"),
        ({"legal": {"replicas": {}}}, "whole number of at least 1"),
        ({"legal": {"typo": {}}}, "resources.legal.typo: unknown field"),
        (
            {"postgres": {"cpu": {"request": 9, "limit": 9}}},
            "postgres.cpu.request 9 does not fit one platform node of 8 CPU",
        ),
        (
            {"storage": {"memory": {"request": "64Gi", "limit": "64Gi"}}},
            "storage.memory.request 64Gi does not fit one osdu node",
        ),
        ({"elasticsearch": {"count": 9}}, "elasticsearch.count 9 needs 9 platform nodes"),
        ({"redis": {"replicas": 8}}, "redis.replicas 8 needs 9 platform nodes"),
        ({"legal": {"cpu": {"request": "50m"}}}, "legal.cpu.request 50m is below the 100m"),
        ({"legal": {"cpu": {"request": "2"}}}, "legal.cpu.request 2 exceeds legal.cpu.limit 1"),
        (
            {"redis": {"memory": {"request": "2Gi"}}},
            "redis.memory.request 2Gi exceeds redis.memory.limit 1Gi",
        ),
        ({"postgres": {"instances": 1}}, "postgres.instances 1 is below 2"),
        ({"postgres": {"walStorage": "2Gi"}}, "postgres.walStorage 2Gi is below 4Gi"),
        ({"storage": {"replicas": 220}}, "on the osdu NodePool total"),
        ({"elasticsearch": {"count": 130}}, "on the platform NodePool total"),
    ],
)
def test_a_value_the_cluster_would_refuse_is_refused_first(resources, refusal):
    with pytest.raises(sizing.SizingError, match=re.escape(refusal)):
        sizing.overrides(resources)


def test_empty_groupings_set_nothing():
    assert sizing.overrides({"legal": {}, "unit": {"cpu": {}}, "airflow": {"scheduler": {}}}) == {}


def test_every_problem_is_reported_at_once():
    with pytest.raises(sizing.SizingError) as raised:
        sizing.overrides({"postgres": {"instances": 1}, "legal": {"cpu": {"request": "50m"}}})
    assert "postgres.instances" in str(raised.value)
    assert "legal.cpu.request" in str(raised.value)


def _claim(size: str, **labels: str) -> dict:
    return {"metadata": {"labels": labels}, "spec": {"resources": {"requests": {"storage": size}}}}


CLAIMS = [
    _claim("128Gi", **{"elasticsearch.k8s.elastic.co/cluster-name": "elasticsearch"}),
    _claim("256Gi", **{"elasticsearch.k8s.elastic.co/cluster-name": "elasticsearch"}),
    _claim("10Gi", **{"cnpg.io/cluster": "postgresql", "cnpg.io/pvcRole": "PG_DATA"}),
    _claim("4Gi", **{"cnpg.io/cluster": "postgresql", "cnpg.io/pvcRole": "PG_WAL"}),
    _claim("8Gi", **{"app.kubernetes.io/name": "redis", "app.kubernetes.io/component": "master"}),
    _claim("1Gi", **{"app": "something-else"}),
]


def test_live_storage_is_the_largest_claim_behind_each_key():
    assert sizing.live_storage(CLAIMS) == {
        "ES_STORAGE": "256Gi",
        "PG_STORAGE": "10Gi",
        "PG_WAL_STORAGE": "4Gi",
        "REDIS_STORAGE": "8Gi",
    }


@pytest.mark.parametrize(
    ("values", "refusal"),
    [
        ({"ES_STORAGE": "200Gi"}, "elasticsearch.storage 200Gi is smaller than the 256Gi"),
        # Every key the file leaves out still renders its default.
        ({}, "elasticsearch.storage 128Gi is smaller than the 256Gi"),
        ({"ES_STORAGE": "256Gi", "PG_WAL_STORAGE": "2Gi"}, "postgres.walStorage 2Gi"),
        ({"ES_STORAGE": "256Gi", "REDIS_STORAGE": "16Gi"}, "redis.storage 16Gi differs"),
    ],
)
def test_a_running_environment_refuses_storage_its_volumes_cannot_take(values, refusal):
    with pytest.raises(sizing.SizingError, match=re.escape(refusal)):
        sizing.check_live(values, sizing.live_storage(CLAIMS))


def test_a_running_environment_takes_grown_storage():
    sizing.check_live({"ES_STORAGE": "512Gi", "PG_STORAGE": "20Gi"}, sizing.live_storage(CLAIMS))


def test_a_new_environment_takes_any_storage():
    sizing.check_live({"ES_STORAGE": "16Gi", "REDIS_STORAGE": "2Gi"}, {})


class TestApplySizing:
    @pytest.fixture
    def cluster(self, monkeypatch):
        state: dict[str, Any] = {"present": "", "reads": [], "applied": [], "ran": []}

        def read(args):
            state["reads"].append(args)
            return state["present"]

        monkeypatch.setattr(sizing, "_read", read)
        monkeypatch.setattr(
            sizing, "kubectl_apply_yaml", lambda content, _: state["applied"].append(content)
        )
        monkeypatch.setattr(sizing, "run_command", lambda argv, **_: state["ran"].append(argv))
        return state

    def test_overrides_are_written_as_the_configmap_flux_reads_and_watches(self, cluster):
        sizing.apply_sizing({"PG_INSTANCES": "2", "ES_COUNT": "1"})

        (applied,) = cluster["applied"]
        configmap = yaml.safe_load(applied)
        assert (configmap["metadata"]["name"], configmap["metadata"]["namespace"]) == (
            SIZING_CONFIGMAP,
            "osdu-flux",
        )
        assert configmap["metadata"]["labels"]["reconcile.fluxcd.io/watch"] == "Enabled"
        assert configmap["data"] == {"ES_COUNT": "1", "PG_INSTANCES": "2"}

    def test_no_overrides_remove_an_existing_configmap(self, cluster):
        cluster["present"] = f"configmap/{SIZING_CONFIGMAP}\n"

        sizing.apply_sizing({})

        assert cluster["reads"] == [
            [
                "get",
                "configmap",
                SIZING_CONFIGMAP,
                "-n",
                "osdu-flux",
                "--ignore-not-found",
                "-o",
                "name",
            ]
        ]
        assert cluster["applied"] == []
        assert cluster["ran"] == [
            ["kubectl", "delete", "configmap", SIZING_CONFIGMAP, "-n", "osdu-flux"]
        ]

    def test_no_overrides_and_no_configmap_change_nothing(self, cluster):
        sizing.apply_sizing({})

        assert cluster["applied"] == cluster["ran"] == []


class TestCheckStorage:
    @pytest.fixture
    def kubectl(self, monkeypatch):
        calls = []

        def run(argv, **_):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"items": CLAIMS}), stderr="")

        monkeypatch.setattr(sizing, "run_process", run)
        return calls

    def test_storage_is_checked_against_the_claims_in_platform(self, kubectl):
        with pytest.raises(sizing.SizingError, match="elasticsearch.storage 16Gi is smaller"):
            sizing.check_storage({"ES_STORAGE": "16Gi"})

        assert kubectl == [["kubectl", "get", "pvc", "-n", "platform", "-o", "json"]]

    def test_an_unreadable_cluster_is_not_read_as_empty(self, monkeypatch):
        monkeypatch.setattr(
            sizing,
            "run_process",
            lambda argv, **_: SimpleNamespace(returncode=1, stdout="", stderr="forbidden"),
        )

        with pytest.raises(sizing.SizingError, match="could not read pvc: forbidden"):
            sizing.check_storage({})
