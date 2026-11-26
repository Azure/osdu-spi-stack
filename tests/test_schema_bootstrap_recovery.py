"""Operator Schema recovery keeps maintenance and normal deploy guards intact."""

import copy
from dataclasses import replace
from typing import Callable, NotRequired, TypedDict

import pytest
from typer.testing import CliRunner

from spi import cli, loads, pins, status
from spi.build import BuiltImage, Registry
from spi.deploy_record import DeployRecord, DeployRecordError
from spi.pins import PinError, ServicePin
from spi.status import KustomizationReadiness, KustomizationState, StatusError, StatusReason

DIGEST = "sha256:" + "a" * 64
IMAGE = f"osdudev123.azurecr.io/local/schema@{DIGEST}"


class ClusterState(TypedDict):
    record: DeployRecord | None
    states: list[KustomizationState]
    bootstrap: StatusReason | None
    schema_job: dict | None
    lock: dict
    reconciled: list[str]
    before_write: NotRequired[Callable[[], None]]
    after_write: NotRequired[Callable[[], None]]


@pytest.fixture
def cluster(monkeypatch) -> ClusterState:
    states = [
        KustomizationState(name, True, "ReconciliationSucceeded", "")
        for name in ("spi-osdu-services", "spi-osdu-init", "spi-osdu-members")
    ] + [
        KustomizationState(
            "spi-osdu-schema-load",
            False,
            "HealthCheckFailed",
            "Job/osdu/schema-load status: 'Failed'",
        ),
        KustomizationState(
            "spi-osdu-reference", False, "DependencyNotReady", "spi-osdu-schema-load is not ready"
        ),
        KustomizationState("osdu-spi-stack-system-stack", False, "Progressing", "Reconciling"),
    ]
    state: ClusterState = {
        "record": DeployRecord("v1", "b" * 40, "now", "1", "core", True, "dev1"),
        "states": states,
        "bootstrap": None,
        "schema_job": {"status": {"conditions": [{"type": "Failed", "status": "True"}]}},
        "lock": {
            "metadata": {"annotations": {}, "resourceVersion": "1"},
            "data": {"SCHEMA_IMAGE_REPOSITORY": "canonical/schema", "SCHEMA_IMAGE_TAG": "old"},
        },
        "reconciled": [],
    }

    def readiness():
        reason = StatusReason("kustomization_not_ready", "schema-load failed", "schema-load")
        return KustomizationReadiness((), tuple(state["states"]), False, reason, reason)

    def mutate(compute, description):
        if callback := state.pop("before_write", None):
            callback()
        patch = compute(copy.deepcopy(state["lock"]))
        state["lock"]["data"] = patch["data"]
        state["lock"]["metadata"]["annotations"] = patch["metadata"]["annotations"]
        if callback := state.pop("after_write", None):
            callback()

    registry = Registry("osdudev123", "osdudev123.azurecr.io", "/registry", "rg", "sub", "aks")
    monkeypatch.setattr(status, "collect_kustomization_readiness", readiness)
    monkeypatch.setattr(status, "collect_bootstrap_blocker", lambda: state["bootstrap"])
    monkeypatch.setattr(loads, "read_schema_job", lambda: state["schema_job"])
    monkeypatch.setattr(pins, "read_deploy_record", lambda **kwargs: state["record"])
    monkeypatch.setattr(pins, "read_lock", lambda **kwargs: copy.deepcopy(state["lock"]))
    monkeypatch.setattr(pins, "mutate_lock", mutate)
    monkeypatch.setattr(pins, "environment_registry", lambda: registry)
    monkeypatch.setattr(pins, "require_cluster_pull", lambda registry: None)
    monkeypatch.setattr(pins, "require_local_manifest", lambda *args: None)
    monkeypatch.setattr(
        pins, "reconcile_consumers", lambda names: state["reconciled"].extend(names)
    )
    monkeypatch.setattr(cli, "verify_spi_cluster", lambda: "spi-stack-dev1")
    monkeypatch.setattr(cli, "_environment_label", lambda: "dev1")
    return state


def borrow():
    return ServicePin(
        mr="",
        branch="",
        repository="repo",
        tag="tag",
        canonical_repository="canonical",
        canonical_tag="old",
        canonical_created_at="",
        canonical_digest="",
        applied_at="now",
        ephemeral=True,
    )


def test_default_preflight_still_refuses_failed_bootstrap(cluster):
    with pytest.raises(PinError, match="not ready"):
        pins.require_local_pinnable("schema")


def test_named_recovery_accepts_only_the_schema_failure(cluster):
    pins.require_local_pinnable("schema", recovery_environment="dev1")
    assert cluster["record"].maintenance
    assert cluster["lock"]["metadata"]["annotations"] == {}


@pytest.mark.parametrize("service", ["storage", "partition", "schema-load"])
def test_recovery_refuses_other_services(cluster, service):
    with pytest.raises(PinError):
        pins.require_local_pinnable(service, recovery_environment="dev1")


@pytest.mark.parametrize("environment,maintenance", [("other", True), ("dev1", False)])
def test_recovery_requires_named_environment_in_maintenance(cluster, environment, maintenance):
    cluster["record"] = replace(cluster["record"], maintenance=maintenance)
    with pytest.raises(PinError):
        pins.require_local_pinnable("schema", recovery_environment=environment)


def test_recovery_requires_a_deploy_record(cluster):
    cluster["record"] = None
    with pytest.raises(PinError):
        pins.require_local_pinnable("schema", recovery_environment="dev1")


@pytest.mark.parametrize("missing", ["spi-osdu-services", "spi-osdu-init", "spi-osdu-members"])
def test_recovery_requires_complete_core_initialization(cluster, missing):
    cluster["states"] = [s for s in cluster["states"] if s.name != missing]
    with pytest.raises(PinError):
        pins.require_local_pinnable("schema", recovery_environment="dev1")


@pytest.mark.parametrize(
    "state",
    [
        KustomizationState("spi-osdu-services", False, "HealthCheckFailed", "API failed"),
        KustomizationState("spi-redis", False, "HealthCheckFailed", "Redis failed"),
        KustomizationState("spi-osdu-reference", False, "HealthCheckFailed", "Other failure"),
        KustomizationState("spi-osdu-schema-load", True, "ReconciliationSucceeded", ""),
        KustomizationState("spi-osdu-schema-load", False, "HealthCheckFailed", "Unrelated failure"),
    ],
)
def test_recovery_refuses_non_schema_bootstrap_failures(cluster, state):
    cluster["states"] = [s for s in cluster["states"] if s.name != state.name] + [state]
    with pytest.raises(PinError):
        pins.require_local_pinnable("schema", recovery_environment="dev1")


def test_recovery_refuses_unseeded_entitlements(cluster):
    cluster["bootstrap"] = StatusReason("bootstrap_failed", "members failed", "members")
    with pytest.raises(PinError, match="members"):
        pins.require_local_pinnable("schema", recovery_environment="dev1")


@pytest.mark.parametrize(
    "job",
    [
        None,
        {},
        {"status": {"active": 1}},
        {"status": {"conditions": [{"type": "Failed", "status": "False"}]}},
    ],
)
def test_recovery_requires_a_confirmed_failed_schema_job(cluster, job):
    cluster["schema_job"] = job
    with pytest.raises(PinError):
        pins.require_local_pinnable("schema", recovery_environment="dev1")


@pytest.mark.parametrize("dependency", ["spi-osdu-schema-load", "spi-osdu-reference"])
def test_recovery_accepts_the_parent_health_check_cascade(cluster, dependency):
    cluster["states"][-1] = KustomizationState(
        "osdu-spi-stack-system-stack",
        False,
        "HealthCheckFailed",
        f"timeout waiting for: [Kustomization/osdu-flux/{dependency} status: 'InProgress']",
    )
    pins.require_local_pinnable("schema", recovery_environment="dev1")


@pytest.mark.parametrize(
    "resources",
    [
        "Kustomization/osdu-flux/spi-redis status: 'Failed'",
        "Kustomization/other/spi-osdu-schema-load status: 'Failed'",
        "Kustomization/osdu-flux/spi-osdu-schema-load status: 'Failed', Deployment/osdu/schema status: 'Failed'",
        "no named resources",
    ],
)
def test_recovery_refuses_an_unrelated_parent_health_check(cluster, resources):
    cluster["states"][-1] = KustomizationState(
        "osdu-spi-stack-system-stack", False, "HealthCheckFailed", resources
    )
    with pytest.raises(PinError):
        pins.require_local_pinnable("schema", recovery_environment="dev1")


@pytest.mark.parametrize("name", ["storage", "schema-load"])
def test_recovery_refuses_borrowed_or_pinned_loader(cluster, name):
    cluster["lock"]["metadata"]["annotations"][pins.PINS_ANNOTATION] = pins.encode_pins(
        {name: replace(borrow(), ephemeral=name != "schema-load")}
    )
    with pytest.raises(PinError):
        pins.require_local_pinnable("schema", recovery_environment="dev1")


def test_recovery_writes_only_schema_and_preserves_maintenance(cluster):
    [(service, pin)] = pins.pin_service_image("schema", IMAGE, recovery_environment="dev1")
    assert service == "schema"
    assert pin.origin == "local"
    assert not pin.ephemeral
    assert pin.canonical_repository == "canonical/schema"
    assert pins.decode_pins(cluster["lock"]) == {"schema": pin}
    assert cluster["reconciled"] == ["schema"]
    assert cluster["record"].maintenance


def test_recovery_reset_restores_canonical_while_maintenance_stays_enabled(cluster):
    pins.pin_service_image("schema", IMAGE, recovery_environment="dev1")
    pins.reset_service("schema")
    assert pins.decode_pins(cluster["lock"]) == {}
    assert cluster["lock"]["data"]["SCHEMA_IMAGE_REPOSITORY"] == "canonical/schema"
    assert cluster["lock"]["data"]["SCHEMA_IMAGE_TAG"] == "old"
    assert cluster["record"].maintenance


def test_recovery_rechecks_borrows_inside_lock_transaction(cluster):
    before = copy.deepcopy(cluster["lock"]["data"])

    def concurrent_borrow():
        cluster["lock"]["metadata"]["annotations"][pins.PINS_ANNOTATION] = pins.encode_pins(
            {"storage": borrow()}
        )

    cluster["before_write"] = concurrent_borrow
    with pytest.raises(PinError):
        pins.pin_service_image("schema", IMAGE, recovery_environment="dev1")
    assert cluster["lock"]["data"] == before
    assert cluster["reconciled"] == []


@pytest.mark.parametrize("change", ["maintenance", "revision", "disappeared"])
def test_recovery_reverts_if_the_deploy_record_changes(cluster, change):
    before = copy.deepcopy(cluster["lock"]["data"])

    def intervene():
        record = cluster["record"]
        cluster["record"] = (
            None
            if change == "disappeared"
            else replace(
                record, **({"maintenance": False} if change == "maintenance" else {"ref": "v2"})
            )
        )

    cluster["after_write"] = intervene
    with pytest.raises(PinError):
        pins.pin_service_image("schema", IMAGE, recovery_environment="dev1")
    assert cluster["lock"]["data"] == before
    assert pins.decode_pins(cluster["lock"]) == {}
    assert cluster["reconciled"] == []


@pytest.mark.parametrize("error", [StatusError("unreadable"), DeployRecordError("unreadable")])
def test_recovery_fails_closed_on_unreadable_state(cluster, monkeypatch, error):
    def fail(**kwargs):
        raise error

    if isinstance(error, StatusError):
        monkeypatch.setattr(status, "collect_kustomization_readiness", fail)
    else:
        monkeypatch.setattr(pins, "read_deploy_record", fail)
    with pytest.raises(PinError, match="unreadable"):
        pins.require_local_pinnable("schema", recovery_environment="dev1")


def test_recovery_does_not_accept_ghcr_or_ephemeral_images(cluster):
    with pytest.raises(PinError):
        pins.pin_service_image(
            "schema", f"ghcr.io/azure/schema@{DIGEST}", recovery_environment="dev1"
        )
    with pytest.raises(PinError):
        pins.pin_service_image("schema", IMAGE, ephemeral=True, recovery_environment="dev1")


def test_recovery_option_pins_the_candidate_without_clearing_maintenance(cluster):
    result = CliRunner().invoke(
        cli.app, ["service", "pin", "schema", "--image", IMAGE, "--recover-bootstrap", "dev1"]
    )
    assert result.exit_code == 0, result.output
    assert cluster["record"].maintenance
    assert set(pins.decode_pins(cluster["lock"])) == {"schema"}


def test_source_recovery_checks_before_build_and_passes_through(cluster, monkeypatch, tmp_path):
    built = BuiltImage("schema", IMAGE.split("@")[0], DIGEST, "sha-test", "b" * 40, False)
    monkeypatch.setattr(cli, "build_image", lambda *args, **kwargs: built)
    monkeypatch.setattr(cli, "_await_local_rollout", lambda *args: None)
    result = CliRunner().invoke(
        cli.app,
        ["service", "pin", "schema", "--source", str(tmp_path), "--recover-bootstrap", "dev1"],
    )
    assert result.exit_code == 0, result.output
    assert pins.decode_pins(cluster["lock"])["schema"].source_sha == "b" * 40


def test_source_recovery_refusal_spends_no_build(cluster, monkeypatch, tmp_path):
    def unexpected_build(*args, **kwargs):
        pytest.fail("A refused recovery must not build")

    monkeypatch.setattr(cli, "build_image", unexpected_build)
    result = CliRunner().invoke(
        cli.app,
        ["service", "pin", "schema", "--source", str(tmp_path), "--recover-bootstrap", "other"],
    )
    assert result.exit_code == 1, result.output
    assert pins.decode_pins(cluster["lock"]) == {}
