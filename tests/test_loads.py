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

"""Named loads: registry validation, Job state, the re-run plan, the facts
block, the Job manifest, and the `spi load` exit codes."""

import json
from pathlib import Path

import pytest
import yaml
from _quantities import _millicores
from typer.testing import CliRunner

from spi import cli, loads, pins, templates
from spi.loads import Action

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_FILE = REPO_ROOT / "software" / "stacks" / "osdu" / "init" / "load-registry.yaml"
CHART_VALUES = REPO_ROOT / "software" / "charts" / "osdu-spi-init" / "values.yaml"
STACK = REPO_ROOT / "software" / "stacks" / "osdu" / "profiles" / "core" / "stack.yaml"
COMMIT = "99f8fc88d8ad838b5738ac5ad92ac643538b5766"
SOURCE = f"osdu/data/data-definitions@{COMMIT}:ReferenceValues/Manifests/reference-data"
OUTCOME = (
    "load-records outcome: loaded: source=osdu/data/data-definitions@99f8fc88d8ad "
    "total=80103 loaded=80103 skipped=0 failed=0"
)
LOCK_SERVICES = {
    "schema-load": {
        "repository": "ghcr.io/example-org/schema-load",
        "tag": "sha-0a1b2c3d4e5f",
        "digest": "sha256:" + "a" * 64,
    }
}
TARGET = loads.Target(
    partitions=("opendes",),
    legal_tag_base="demo-legaltag",
    entitlements_domain="dataservices.energy",
    lock_services=LOCK_SERVICES,
)


@pytest.fixture(scope="module")
def registry() -> loads.Registry:
    return loads.parse_registry(REGISTRY_FILE.read_text(encoding="utf-8"))


def _job(
    phase: str,
    *,
    source: str = SOURCE,
    partition: str = "opendes",
    name: str = "load-reference-data-opendes-99f8fc88",
    created: str = "2026-10-02T15:00:00Z",
    outcome: str = "",
) -> dict:
    status: dict = {"startTime": created}
    if phase == "complete":
        status.update(succeeded=1, completionTime="2026-10-02T15:04:11Z")
        status["conditions"] = [{"type": "Complete", "status": "True"}]
    elif phase == "failed":
        status.update(failed=3, conditions=[{"type": "Failed", "status": "True"}])
    elif phase == "retrying":
        status.update(failed=1, active=1)
    else:
        status.update(active=1)
    annotations = {loads.SOURCE_ANNOTATION: source}
    if outcome:
        annotations[loads.OUTCOME_ANNOTATION] = outcome
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": created,
            "labels": {
                loads.ROLE_LABEL: loads.LOAD_ROLE,
                loads.DATASET_LABEL: "reference-data",
                loads.PARTITION_LABEL: partition,
            },
            "annotations": annotations,
        },
        "status": status,
    }


def _schema_job(phase: str = "complete", digest: str = "a" * 64) -> dict:
    job = _job(phase, name="schema-load")
    job["metadata"]["labels"] = {"app.kubernetes.io/component": "schema-load"}
    job["spec"] = {
        "template": {
            "spec": {"containers": [{"image": f"ghcr.io/example-org/schema-load@sha256:{digest}"}]}
        }
    }
    return job


def _facts(registry, jobs=(), schema_job=None, partitions=("opendes",), outcome_of=None):
    return loads.build_facts(
        registry,
        list(partitions),
        list(jobs),
        schema_job,
        LOCK_SERVICES,
        outcome_of or (lambda job: loads._annotation(job, loads.OUTCOME_ANNOTATION)),
    )


# --- registry ----------------------------------------------------------------


def test_the_shipped_registry_parses(registry):
    assert [load.name for load in registry.loads] == ["schemas", "reference-data"]
    reference = registry.get("reference-data")
    assert reference.source.identity == SOURCE
    assert reference.requires == ["schemas"]
    assert loads.job_name(reference, "opendes") == "load-reference-data-opendes-99f8fc88"


def _registry_with(**overrides) -> str:
    document = yaml.safe_load(REGISTRY_FILE.read_text(encoding="utf-8"))
    document["loads"][1].update(overrides)
    return yaml.safe_dump(document)


@pytest.mark.parametrize(
    "text",
    [
        _registry_with(name="schemas"),
        _registry_with(requires=["well-data"]),
        _registry_with(requires=["reference-data"]),
        _registry_with(runner="cron"),
        _registry_with(name="Reference_Data"),
        _registry_with(source={"type": "gitlab-archive", "project": "p", "path": "x", "ref": "v1"}),
        _registry_with(
            source={"type": "gitlab-archive", "project": "p", "path": "x", "commit": "99f8fc88"}
        ),
        _registry_with(source={"type": "paired-image", "service": "schema"}),
        "apiVersion: spi.osdu.dev/v2\nloads: []\n",
        "loads: [",
    ],
    ids=[
        "duplicate-name",
        "unknown-requirement",
        "self-requirement",
        "unknown-runner",
        "bad-name",
        "archive-without-commit",
        "archive-with-short-commit",
        "job-with-image-source",
        "newer-api-version",
        "not-yaml",
    ],
)
def test_an_invalid_registry_is_refused(text):
    with pytest.raises(loads.LoadError) as raised:
        loads.parse_registry(text)

    assert raised.value.code == "registry_unreadable"
    assert raised.value.refused


def test_a_requirement_cycle_is_refused():
    document = yaml.safe_load(REGISTRY_FILE.read_text(encoding="utf-8"))
    document["loads"][0]["requires"] = ["reference-data"]

    with pytest.raises(loads.LoadError, match="cycle"):
        loads.parse_registry(yaml.safe_dump(document))


def test_a_job_name_longer_than_kubernetes_allows_is_refused(registry):
    with pytest.raises(loads.LoadError) as raised:
        loads.job_name(registry.get("reference-data"), "p" * 40)

    assert raised.value.code == "name_too_long"


# --- the re-run plan ---------------------------------------------------------


@pytest.mark.parametrize(
    ("newest", "plain", "forced"),
    [
        (None, Action.CREATE, Action.CREATE),
        (_job("running"), Action.WAIT, Action.RECREATE),
        (_job("retrying"), Action.WAIT, Action.RECREATE),
        (_job("complete"), Action.NONE, Action.RECREATE),
        (_job("complete", source="osdu/data/data-definitions@old"), *[Action.SUPERSEDE] * 2),
        (_job("failed"), Action.RECREATE, Action.RECREATE),
    ],
    ids=["none", "running", "retrying", "complete-same", "complete-other", "failed"],
)
def test_plan_follows_the_newest_job(registry, newest, plain, forced):
    load = registry.get("reference-data")

    assert loads.plan_load(load, newest, force=False) is plain
    assert loads.plan_load(load, newest, force=True) is forced


def test_the_newest_job_is_chosen_per_partition(registry):
    load = registry.get("reference-data")
    jobs = [
        _job("complete", name="new", created="2026-10-02T15:00:00Z"),
        _job("failed", name="old", created="2026-09-01T00:00:00Z"),
        _job("running", name="other", partition="second", created="2026-10-03T00:00:00Z"),
    ]

    assert loads._name(loads.newest_job(jobs, load, "opendes") or {}) == "new"
    assert loads._name(loads.newest_job(jobs, load, "second") or {}) == "other"
    assert loads.newest_job(jobs, load, "third") is None


# --- the facts block ---------------------------------------------------------


def test_facts_name_every_registry_entry_before_anything_ran(registry):
    assert _facts(registry) == {
        "schemas": {
            "state": "absent",
            "source": "ghcr.io/example-org/schema-load@sha256:" + "a" * 64,
            "version": "sha-0a1b2c3d4e5f",
            "partitions": {"opendes": {"state": "absent"}},
        },
        "reference-data": {
            "state": "absent",
            "source": "osdu/data/data-definitions@99f8fc88d8ad",
            "version": "v0.30.0",
            "partitions": {"opendes": {"state": "absent"}},
        },
    }


def test_facts_report_a_complete_load_with_its_counts(registry):
    facts = _facts(registry, [_job("complete", outcome=OUTCOME)], _schema_job())

    assert facts["schemas"]["state"] == "complete"
    assert facts["schemas"]["partitions"]["opendes"]["completedAt"] == "2026-10-02T15:04:11Z"
    assert facts["reference-data"]["partitions"]["opendes"] == {
        "state": "complete",
        "completedAt": "2026-10-02T15:04:11Z",
        "records": {"total": 80103, "loaded": 80103, "failed": 0},
    }


def test_facts_omit_counts_when_the_outcome_was_not_captured(registry):
    entry = _facts(registry, [_job("complete")])["reference-data"]["partitions"]["opendes"]

    assert entry == {"state": "complete", "completedAt": "2026-10-02T15:04:11Z"}


def test_facts_call_a_load_from_another_source_stale(registry):
    facts = _facts(registry, [_job("complete", source="osdu/data/data-definitions@old")])

    assert facts["reference-data"]["state"] == "stale"


def test_facts_carry_the_outcome_of_a_failed_load(registry):
    outcome = "load-records outcome: schema_behind: 2 of 584 kinds are not registered: a, b"
    facts = _facts(registry, [_job("failed", outcome=outcome)])

    assert facts["reference-data"]["partitions"]["opendes"] == {
        "state": "failed",
        "message": outcome,
    }


def test_facts_do_not_read_the_outcome_of_a_running_job(registry):
    def outcome_of(job):
        raise AssertionError("a running Job has no outcome")

    facts = _facts(registry, [_job("retrying")], outcome_of=outcome_of)

    assert facts["reference-data"]["partitions"]["opendes"] == {"state": "running"}


def test_facts_report_the_weakest_partition(registry):
    jobs = [_job("complete"), _job("running", partition="second", name="second")]
    facts = _facts(registry, jobs, _schema_job(), partitions=("opendes", "second", "third"))

    reference = facts["reference-data"]
    assert {name: entry["state"] for name, entry in reference["partitions"].items()} == {
        "opendes": "complete",
        "second": "running",
        "third": "absent",
    }
    assert reference["state"] == "absent"
    assert list(facts["schemas"]["partitions"]) == ["opendes"]


def test_schemas_are_stale_until_flux_replaces_the_job_for_a_moved_lock(registry):
    facts = _facts(registry, schema_job=_schema_job(digest="b" * 64))

    assert facts["schemas"]["state"] == "stale"


def test_schemas_failed_and_running_follow_the_job(registry):
    assert _facts(registry, schema_job=_schema_job("failed"))["schemas"]["state"] == "failed"
    assert _facts(registry, schema_job=_schema_job("running"))["schemas"]["state"] == "running"


def test_collect_facts_is_empty_without_a_registry(monkeypatch):
    monkeypatch.setattr(loads, "read_registry", lambda: None)
    monkeypatch.setattr(loads, "read_load_jobs", lambda: [])
    monkeypatch.setattr(loads, "read_schema_job", lambda: None)

    assert loads.collect_facts(["opendes"], LOCK_SERVICES) == {}


def test_collect_facts_is_empty_for_a_registry_this_cli_predates(monkeypatch):
    def unreadable():
        raise loads.LoadError("registry_unreadable", "newer", refused=True)

    monkeypatch.setattr(loads, "read_registry", unreadable)
    monkeypatch.setattr(loads, "read_load_jobs", lambda: [])
    monkeypatch.setattr(loads, "read_schema_job", lambda: None)

    assert loads.collect_facts(["opendes"], LOCK_SERVICES) == {}


# --- the Job manifest --------------------------------------------------------


def _rendered_job() -> dict:
    return yaml.safe_load(
        templates.load_job(
            name="load-reference-data-opendes-99f8fc88",
            dataset="reference-data",
            partition="opendes",
            source=SOURCE,
            env={"PARTITION": "opendes", "LEGAL_TAG": "opendes-demo-legaltag"},
        )
    )


def test_load_job_carries_the_labels_and_source_the_cli_reads_back():
    job = _rendered_job()

    assert job["metadata"]["namespace"] == loads.LOAD_NAMESPACE
    assert job["metadata"]["annotations"] == {loads.SOURCE_ANNOTATION: SOURCE}
    for labels in (job["metadata"]["labels"], job["spec"]["template"]["metadata"]["labels"]):
        assert labels[loads.ROLE_LABEL] == loads.LOAD_ROLE
        assert labels[loads.DATASET_LABEL] == "reference-data"
        assert labels[loads.PARTITION_LABEL] == "opendes"
        assert labels["app.kubernetes.io/managed-by"] == "osdu-spi-stack"


def test_load_job_is_kept_as_the_record_and_outlives_the_cli_wait():
    spec = _rendered_job()["spec"]

    assert "ttlSecondsAfterFinished" not in spec
    assert spec["backoffLimit"] == 2
    assert spec["activeDeadlineSeconds"] == templates.LOAD_JOB_DEADLINE_SECONDS
    assert loads.WAIT_SECONDS > spec["activeDeadlineSeconds"]


def test_load_job_meets_what_admission_grants_the_init_jobs():
    pod = _rendered_job()["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert pod["serviceAccountName"] == "workload-identity-sa"
    assert (
        _rendered_job()["spec"]["template"]["metadata"]["labels"]["azure.workload.identity/use"]
        == "true"
    )
    assert pod["restartPolicy"] == "Never"
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "runAsUser": 1000,
        "capabilities": {"drop": ["ALL"]},
    }
    assert _millicores(container["resources"]["requests"]["cpu"]) >= 100
    assert container["command"] == ["python", "/scripts/load_records.py"]
    assert container["env"] == [
        {"name": "PYTHONUNBUFFERED", "value": "1"},
        {"name": "PARTITION", "value": "opendes"},
        {"name": "LEGAL_TAG", "value": "opendes-demo-legaltag"},
    ]


def test_load_job_follows_the_init_chart_image_placement_and_scripts():
    values = yaml.safe_load(CHART_VALUES.read_text(encoding="utf-8"))
    pod = _rendered_job()["spec"]["template"]["spec"]

    image = f"{values['image']['repository']}:{values['image']['tag']}"
    assert pod["containers"][0]["image"] == image == templates.LOAD_JOB_IMAGE
    assert pod["nodeSelector"] == values["nodeSelector"]
    assert pod["tolerations"] == values["tolerations"]
    assert pod["serviceAccountName"] == values["serviceAccountName"]
    assert pod["volumes"] == [
        {"name": "scripts", "configMap": {"name": "osdu-spi-init-scripts", "defaultMode": 0o755}}
    ]


def test_schema_reconcile_waits_as_long_as_the_kustomization_does():
    documents = yaml.safe_load_all(STACK.read_text(encoding="utf-8"))
    kustomization = next(
        doc for doc in documents if doc and doc["metadata"]["name"] == loads.SCHEMA_KUSTOMIZATION
    )

    assert kustomization["spec"]["timeout"] == loads.SCHEMA_RECONCILE_TIMEOUT


# --- running loads -----------------------------------------------------------


class _Cluster:
    """The load Jobs of a fake cluster, with every write `spi load` makes."""

    def __init__(self, monkeypatch, registry, jobs=(), schema_job=None, finish="complete"):
        self.jobs: list[dict] = list(jobs)
        self.schema_job = schema_job if schema_job is not None else _schema_job()
        self.finish = finish
        self.created: list[dict] = []
        self.commands: list[list[str]] = []
        monkeypatch.setattr(loads, "read_load_jobs", lambda: list(self.jobs))
        monkeypatch.setattr(loads, "read_schema_job", lambda: self.schema_job)
        monkeypatch.setattr(loads, "read_outcome", self._outcome)
        monkeypatch.setattr(loads, "_legal_tag_seeded", lambda partition: True)
        monkeypatch.setattr(pins, "require_deployable", lambda: None)
        monkeypatch.setattr(loads, "kubectl_apply_yaml", self._apply)
        monkeypatch.setattr(loads, "run_command", self._run)
        monkeypatch.setattr(loads, "_kubectl_read_json", self._read)
        monkeypatch.setattr(loads, "_progress", lambda name: "")
        monkeypatch.setattr(loads, "display_yaml", lambda *args, **kwargs: None)
        monkeypatch.setattr(loads.time, "sleep", lambda seconds: None)

    def _outcome(self, job):
        return loads._annotation(job, loads.OUTCOME_ANNOTATION) or (
            OUTCOME if loads.job_phase(job) == "complete" else "load-records outcome: schema_behind"
        )

    def _apply(self, manifest, description):
        document = yaml.safe_load(manifest)
        self.created.append(document)
        labels = document["metadata"]["labels"]
        self.jobs.append(
            _job(
                self.finish,
                name=document["metadata"]["name"],
                partition=labels[loads.PARTITION_LABEL],
                source=document["metadata"]["annotations"][loads.SOURCE_ANNOTATION],
                created="2026-10-05T00:00:00Z",
            )
        )

    def _run(self, command, **kwargs):
        self.commands.append(command)
        if command[:3] == ["kubectl", "delete", "job"]:
            self.jobs = [job for job in self.jobs if loads._name(job) != command[3]]

    def _read(self, args, describe):
        return next((job for job in self.jobs if loads._name(job) == args[2]), None)

    def deleted(self) -> list[str]:
        return [c[3] for c in self.commands if c[:3] == ["kubectl", "delete", "job"]]

    def names(self) -> list[str]:
        return [loads._name(job) for job in self.jobs]


def _run(registry, datasets=(), partition=None, force=False, wait=True, target=TARGET):
    loads.run_loads(registry, target, list(datasets), partition, force, wait)


def test_a_first_load_creates_the_job_and_records_its_outcome(monkeypatch, registry):
    cluster = _Cluster(monkeypatch, registry)

    _run(registry)

    assert [doc["metadata"]["name"] for doc in cluster.created] == [
        "load-reference-data-opendes-99f8fc88"
    ]
    env = {
        e["name"]: e["value"]
        for e in cluster.created[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env == {
        "PYTHONUNBUFFERED": "1",
        "PARTITION": "opendes",
        "LEGAL_TAG": "opendes-demo-legaltag",
        "ENTITLEMENTS_DOMAIN": "dataservices.energy",
        "SOURCE_PROJECT": "osdu/data/data-definitions",
        "SOURCE_COMMIT": COMMIT,
        "SOURCE_PATH": "ReferenceValues/Manifests/reference-data",
    }
    annotate = next(c for c in cluster.commands if c[:2] == ["kubectl", "annotate"])
    assert annotate[-1] == f"{loads.OUTCOME_ANNOTATION}={OUTCOME}"
    assert cluster.deleted() == []


def test_a_second_run_of_a_complete_load_changes_nothing(monkeypatch, registry):
    cluster = _Cluster(monkeypatch, registry, [_job("complete", outcome=OUTCOME)])

    _run(registry)

    assert cluster.created == []
    assert cluster.commands == []


def test_a_complete_load_without_its_outcome_gets_it_recorded(monkeypatch, registry):
    cluster = _Cluster(monkeypatch, registry, [_job("complete")])

    _run(registry)

    assert cluster.created == []
    assert [c[:2] for c in cluster.commands] == [["kubectl", "annotate"]]


def test_force_recreates_a_complete_load(monkeypatch, registry):
    cluster = _Cluster(monkeypatch, registry, [_job("complete", outcome=OUTCOME)])

    _run(registry, ["reference-data"], force=True)

    assert cluster.deleted() == ["load-reference-data-opendes-99f8fc88"]
    assert len(cluster.created) == 1


def test_a_failed_load_is_deleted_and_recreated(monkeypatch, registry):
    cluster = _Cluster(monkeypatch, registry, [_job("failed")])

    _run(registry)

    assert cluster.deleted() == ["load-reference-data-opendes-99f8fc88"]
    assert len(cluster.created) == 1


def test_a_load_from_another_source_is_replaced_once_the_new_one_completes(monkeypatch, registry):
    old = _job("complete", source="osdu/data/data-definitions@old", name="load-old")
    cluster = _Cluster(monkeypatch, registry, [old])

    _run(registry)

    assert cluster.names() == ["load-reference-data-opendes-99f8fc88"]
    assert cluster.deleted() == ["load-old"]


def test_the_previous_load_stays_when_its_replacement_fails(monkeypatch, registry):
    old = _job("complete", source="osdu/data/data-definitions@old", name="load-old")
    cluster = _Cluster(monkeypatch, registry, [old], finish="failed")

    with pytest.raises(loads.LoadError) as raised:
        _run(registry)

    assert raised.value.code == "load_failed"
    assert not raised.value.refused
    assert "schema_behind" in str(raised.value)
    assert "load-old" in cluster.names()


def test_a_running_load_is_waited_on_not_duplicated(monkeypatch, registry):
    cluster = _Cluster(monkeypatch, registry, [_job("running")])
    polls = iter([_job("running"), _job("complete")])
    monkeypatch.setattr(loads, "_kubectl_read_json", lambda args, describe: next(polls))

    _run(registry)

    assert cluster.created == []
    assert cluster.deleted() == []


def test_no_wait_starts_the_job_and_returns(monkeypatch, registry):
    cluster = _Cluster(monkeypatch, registry, finish="running")

    _run(registry, wait=False)

    assert len(cluster.created) == 1
    assert cluster.commands == []


def test_each_partition_gets_its_own_job_and_partition_narrows_it(monkeypatch, registry):
    target = loads.Target(
        ("opendes", "second"), "demo-legaltag", "dataservices.energy", LOCK_SERVICES
    )
    cluster = _Cluster(monkeypatch, registry)
    _run(registry, target=target)
    assert cluster.names() == [
        "load-reference-data-opendes-99f8fc88",
        "load-reference-data-second-99f8fc88",
    ]

    narrowed = _Cluster(monkeypatch, registry)
    _run(registry, partition="second", target=target)
    assert narrowed.names() == ["load-reference-data-second-99f8fc88"]


@pytest.mark.parametrize("phase", ["running", "failed"])
def test_an_unmet_requirement_refuses_before_anything_is_created(monkeypatch, registry, phase):
    cluster = _Cluster(monkeypatch, registry, schema_job=_schema_job(phase))

    with pytest.raises(loads.LoadError) as raised:
        _run(registry, ["reference-data"])

    assert raised.value.code == "requires_unmet"
    assert raised.value.refused
    assert f"reference-data requires schemas, which is {phase}" in str(raised.value)
    assert cluster.created == [] and cluster.commands == []


def test_a_stale_requirement_is_satisfied(monkeypatch, registry):
    cluster = _Cluster(monkeypatch, registry, schema_job=_schema_job(digest="b" * 64))

    _run(registry, ["reference-data"])

    assert len(cluster.created) == 1


def test_a_missing_legal_tag_refuses_before_anything_is_created(monkeypatch, registry):
    cluster = _Cluster(monkeypatch, registry)
    monkeypatch.setattr(loads, "_legal_tag_seeded", lambda partition: False)

    with pytest.raises(loads.LoadError) as raised:
        _run(registry)

    assert raised.value.code == "legal_tag_missing" and raised.value.refused
    assert cluster.created == []


def test_an_environment_that_is_not_deployable_refuses_a_job_load(monkeypatch, registry):
    cluster = _Cluster(monkeypatch, registry)

    def refuse():
        raise pins.PinError("Environment is under maintenance")

    monkeypatch.setattr(pins, "require_deployable", refuse)

    with pytest.raises(loads.LoadError) as raised:
        _run(registry)

    assert raised.value.code == "not_deployable" and raised.value.refused
    assert cluster.created == []


def test_forcing_schemas_deletes_the_job_and_reconciles_its_kustomization(monkeypatch, registry):
    cluster = _Cluster(monkeypatch, registry)

    def refuse():
        raise AssertionError("the schema re-run is how an environment that is not ready recovers")

    monkeypatch.setattr(pins, "require_deployable", refuse)

    _run(registry, ["schemas"], force=True)

    assert cluster.created == []
    assert cluster.commands == [
        ["kubectl", "delete", "job", "schema-load", "-n", "osdu", "--ignore-not-found"],
        [
            "flux",
            "reconcile",
            "kustomization",
            "spi-osdu-schema-load",
            "-n",
            "osdu-flux",
            "--timeout",
            "155m",
        ],
    ]


def test_schemas_without_force_only_report(monkeypatch, registry):
    cluster = _Cluster(monkeypatch, registry)

    _run(registry, ["schemas"])

    assert cluster.created == [] and cluster.commands == []


def test_an_unknown_dataset_or_partition_is_refused(monkeypatch, registry):
    _Cluster(monkeypatch, registry)

    with pytest.raises(loads.LoadError) as dataset:
        _run(registry, ["well-data"])
    with pytest.raises(loads.LoadError) as partition:
        _run(registry, partition="nowhere")

    assert dataset.value.code == "unknown_dataset" and dataset.value.refused
    assert partition.value.code == "unknown_partition" and partition.value.refused


# --- spi load ----------------------------------------------------------------


@pytest.fixture
def connected(monkeypatch, registry):
    monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-test")
    monkeypatch.setattr(cli, "_environment_label", lambda: "dev v0.27.0 profile core")
    monkeypatch.setattr(loads, "read_registry", lambda: registry)
    monkeypatch.setattr(loads, "read_target", lambda: TARGET)
    return registry


def test_spi_load_exits_zero_and_ends_json_output_with_the_envelope(monkeypatch, connected):
    _Cluster(monkeypatch, connected)

    result = runner.invoke(cli.app, ["load", "--json"])

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert envelope["outcome"] == "ok"
    assert envelope["loads"]["reference-data"]["state"] == "complete"


def test_spi_load_exits_two_when_refused(monkeypatch, connected):
    cluster = _Cluster(monkeypatch, connected, schema_job=_schema_job("running"))

    result = runner.invoke(cli.app, ["load", "--dataset", "reference-data", "--json"])

    assert result.exit_code == 2
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert (envelope["outcome"], envelope["code"]) == ("refused", "requires_unmet")
    assert cluster.created == []


def test_spi_load_exits_one_when_a_load_fails(monkeypatch, connected):
    _Cluster(monkeypatch, connected, finish="failed")

    result = runner.invoke(cli.app, ["load"])

    assert result.exit_code == 1
    assert "reference-data failed for opendes" in result.output


def test_spi_load_exits_two_without_a_registry(monkeypatch, connected):
    monkeypatch.setattr(loads, "read_registry", lambda: None)

    result = runner.invoke(cli.app, ["load"])

    assert result.exit_code == 2
    assert "no load registry" in result.output


def test_spi_load_status_prints_the_document_and_changes_nothing(monkeypatch, connected):
    cluster = _Cluster(monkeypatch, connected, [_job("complete", outcome=OUTCOME)])

    result = runner.invoke(cli.app, ["load", "--status", "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["apiVersion"] == "spi.osdu.dev/v1"
    assert document["loads"]["reference-data"]["partitions"]["opendes"]["records"]["total"] == 80103
    assert cluster.created == [] and cluster.commands == []


def test_spi_load_status_table_names_each_load(monkeypatch, connected):
    _Cluster(monkeypatch, connected, [_job("complete", outcome=OUTCOME)])

    result = runner.invoke(cli.app, ["load", "--status"])

    assert result.exit_code == 0, result.output
    for expected in ("schemas", "reference-data", "complete", "80103", "v0.30.0"):
        assert expected in result.output


def test_progress_reads_the_loader_container_of_the_newest_pod(monkeypatch):
    pods = {
        "items": [
            {"metadata": {"name": "retry", "creationTimestamp": "2026-10-02T13:22:22Z"}},
            {"metadata": {"name": "first", "creationTimestamp": "2026-10-02T13:21:56Z"}},
        ]
    }
    monkeypatch.setattr(loads, "_kubectl_read_json", lambda args, describe: pods)
    commands = []

    def run_process(command, **kwargs):
        commands.append(command)
        stdout = "  kinds 584/584 registered\n  PROGRESS: [5000/80103] loaded=5000 failed=0\n"
        return type("Result", (), {"returncode": 0, "stdout": stdout})()

    monkeypatch.setattr(loads, "run_process", run_process)

    assert loads._progress("load-x") == "[5000/80103] loaded=5000 failed=0"
    assert commands[0][:3] == ["kubectl", "logs", "retry"]
    assert commands[0][commands[0].index("-c") + 1] == "loader"


def test_a_changed_path_at_the_same_commit_is_another_source(registry):
    load = registry.get("reference-data")
    moved = load.model_copy(
        update={"source": load.source.model_copy(update={"path": "ReferenceValues/Other"})}
    )
    loaded = _job("complete")

    assert loads.plan_load(load, loaded, force=False) is Action.NONE
    assert loads.plan_load(moved, loaded, force=False) is Action.SUPERSEDE
    assert moved.source.identity != load.source.identity


def test_a_replacement_started_without_waiting_is_cleaned_up_by_the_next_run(monkeypatch, registry):
    old = _job("complete", source="osdu/data/data-definitions@old", name="load-old")
    cluster = _Cluster(monkeypatch, registry, [old], finish="running")
    _run(registry, wait=False)
    assert cluster.names() == ["load-old", "load-reference-data-opendes-99f8fc88"]

    cluster.jobs[-1] = _job("complete", created="2026-10-05T00:00:00Z", outcome=OUTCOME)
    _run(registry)

    assert cluster.names() == ["load-reference-data-opendes-99f8fc88"]
    assert cluster.deleted() == ["load-old"]


def test_a_requirement_unmet_after_this_run_changed_something_is_not_a_refusal(
    monkeypatch, registry
):
    cluster = _Cluster(monkeypatch, registry)

    def reconcile(command, **kwargs):
        cluster.commands.append(command)
        if command[:3] == ["kubectl", "delete", "job"]:
            cluster.schema_job = _schema_job("running")

    monkeypatch.setattr(loads, "run_command", reconcile)

    with pytest.raises(loads.LoadError) as raised:
        _run(registry, force=True, wait=False)

    assert raised.value.code == "requires_unmet"
    assert not raised.value.refused
    assert "Already started in this run: schemas" in str(raised.value)
    assert cluster.created == []
