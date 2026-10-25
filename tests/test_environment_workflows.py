# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Contract tests for the env-upgrade, env-refresh, and env-reset lifecycle workflows.

These pin what docs/design/environment-lifecycle.md promises: one serializing
concurrency group, the bare released `spi` for every operational call, a
clean skip for an absent or just-created declaration, maintenance set before
any mutation and cleared only after every check passes, and diagnostics
captured on failure.
"""

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from spi.shell import run_process

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_UPGRADE = REPO_ROOT / ".github" / "workflows" / "env-upgrade.yml"
ENV_REFRESH = REPO_ROOT / ".github" / "workflows" / "env-refresh.yml"
ENV_RESET = REPO_ROOT / ".github" / "workflows" / "env-reset.yml"
LIFECYCLE = (ENV_UPGRADE, ENV_REFRESH, ENV_RESET)
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
RELEASE_PLEASE_CONFIG = REPO_ROOT / ".release-please-config.json"
TAG_RULESET = REPO_ROOT / "docs" / "tag-ruleset.json"
CAPTURE_DIAGNOSTICS = REPO_ROOT / "scripts" / "capture_diagnostics.sh"


def _workflow(path: Path) -> dict[str, Any]:
    # BaseLoader keeps the YAML 1.1 word "on" as a string key.
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _steps(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {step["name"]: step for step in job["steps"] if "name" in step}


def _all_run_text(workflow: dict[str, Any]) -> str:
    chunks = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if "run" in step:
                chunks.append(step["run"])
    return "\n".join(chunks)


class TestTriggers:
    def test_env_upgrade_triggers_on_manual_dispatch_and_declaration_path(self):
        workflow = _workflow(ENV_UPGRADE)

        assert "workflow_dispatch" in workflow["on"]
        push = workflow["on"]["push"]
        assert push["branches"] == ["main"]
        assert push["paths"] == ["ops/environments/shared.yaml"]

    def test_env_refresh_triggers_weekdays_at_0400_utc_and_manual_dispatch(self):
        workflow = _workflow(ENV_REFRESH)

        assert "workflow_dispatch" in workflow["on"]
        schedules = workflow["on"]["schedule"]
        assert [entry["cron"] for entry in schedules] == ["0 4 * * 1-5"]


class TestConcurrencyAndEnvironment:
    def test_every_lifecycle_workflow_shares_one_serializing_concurrency_group(self):
        for path in LIFECYCLE:
            workflow = _workflow(path)
            assert workflow["concurrency"]["group"] == "env-shared"
            assert workflow["concurrency"]["cancel-in-progress"] == "false"

    def test_azure_touching_jobs_use_the_azure_shared_environment(self):
        for path in LIFECYCLE:
            workflow = _workflow(path)
            for name, job in workflow["jobs"].items():
                if name == "declare" or name == "verify-release":
                    continue
                assert job.get("environment") == "azure-shared", (
                    f"{path.name}:{name} must run under the azure-shared environment"
                )

    def test_permissions_are_minimal_and_include_id_token(self):
        for path in LIFECYCLE:
            workflow = _workflow(path)
            assert workflow["permissions"] == {"contents": "read", "id-token": "write"}


class TestExactWheelDiscipline:
    def test_no_lifecycle_step_invokes_uv_run_spi(self):
        for path in LIFECYCLE:
            run_text = _all_run_text(_workflow(path))
            assert "uv run spi" not in run_text, (
                f"{path.name} must never invoke the checkout's source CLI via uv run spi"
            )

    def test_every_azure_job_installs_the_exact_release_wheel(self):
        for path in LIFECYCLE:
            workflow = _workflow(path)
            for name, job in workflow["jobs"].items():
                if name in ("declare", "verify-release", "detect", "detect-existing-environment"):
                    continue
                steps = _steps(job)
                install_step = steps.get("Install exact release wheel")
                assert install_step, f"{path.name}:{name} must install the exact release wheel"
                run_text = install_step["run"]
                assert "gh release download" in run_text
                assert "pipx install" in run_text
                # The installed version is checked against the declared tag,
                # not merely installed and trusted.
                assert "spi --version" in run_text
                assert "STACK_VERSION" in run_text or "$STACK_VERSION" in run_text
                # Exact equality, not a substring: `spi 10.6.00` contains
                # `0.6.0` and would otherwise satisfy the declared version.
                assert '[[ "$INSTALLED" == "spi $VERSION" ]]' in run_text, (
                    f"{path.name}:{name} must compare the full `spi --version` output"
                )
                assert '*"$VERSION"*' not in run_text

    def test_declare_job_parses_with_source_python_not_the_cli(self):
        for path in LIFECYCLE:
            declare_steps = _steps(_workflow(path)["jobs"]["declare"])
            export_step = declare_steps["Validate and export declaration"]
            assert "scripts/export_environment.py" in export_step["run"]


class TestCleanSkip:
    def test_env_upgrade_skips_when_declaration_absent(self):
        gate_step = _steps(_workflow(ENV_UPGRADE)["jobs"]["declare"])[
            "Gate on declaration presence and first activation"
        ]
        assert "declaration_found" in gate_step["run"]
        assert "should_run=false" in gate_step["run"]

    def test_env_upgrade_skips_provisioning_on_first_creation_push(self):
        declare = _workflow(ENV_UPGRADE)["jobs"]["declare"]
        steps = _steps(declare)
        detect_step = steps["Detect first activation"]
        assert detect_step["if"] == (
            "steps.export.outputs.declaration_found == 'true' && github.event_name == 'push'"
        )
        gate_step = steps["Gate on declaration presence and first activation"]
        assert "initial_creation" in gate_step["run"]
        assert "should_run=false" in gate_step["run"]

        for job_name, job in _workflow(ENV_UPGRADE)["jobs"].items():
            if job_name == "declare":
                continue
            assert "needs.declare.outputs.should_run" in str(job.get("if", "")), (
                f"job {job_name} must gate on declare.outputs.should_run"
            )

    def test_env_refresh_skips_when_declaration_absent(self):
        workflow = _workflow(ENV_REFRESH)
        declare_gate = _steps(workflow["jobs"]["declare"])["Gate on declaration presence"]
        assert "declaration_found" in declare_gate["run"]
        assert "should_run=false" in declare_gate["run"]

        refresh_job = workflow["jobs"]["refresh"]
        assert refresh_job["if"] == "needs.declare.outputs.should_run == 'true'"

    def test_azure_detection_distinguishes_absence_from_api_failure(self):
        for path in LIFECYCLE:
            detect = _steps(_workflow(path)["jobs"]["detect"])[
                "Detect existing resource group and AKS cluster"
            ]["run"]
            assert "if ! RG_EXISTS=$(az group exists" in detect
            assert "if ! AKS_COUNT=$(az aks list" in detect
            assert "exit 1" in detect


class TestEnvRefreshDetectFailsClosed:
    """declare already gates should_run on the declaration's presence, so an
    absent resource group or cluster at detect time means the live
    environment was deleted, not a first-provision skip. env-upgrade's detect
    stays a clean skip: see
    test_env_upgrade_detect_still_treats_absence_as_a_first_provision_skip.
    """

    def test_detect_job_no_longer_exposes_an_exists_output(self):
        job = _workflow(ENV_REFRESH)["jobs"]["detect"]
        assert "outputs" not in job

    def test_detect_fails_closed_on_absent_resource_group_or_cluster(self):
        detect = _steps(_workflow(ENV_REFRESH)["jobs"]["detect"])[
            "Detect existing resource group and AKS cluster"
        ]["run"]
        assert "clean skip" not in detect
        assert "exists=" not in detect
        assert "does not exist even though" in detect
        assert "No AKS cluster named" in detect

    def test_refresh_job_no_longer_gates_on_a_detect_exists_output(self):
        refresh_job = _workflow(ENV_REFRESH)["jobs"]["refresh"]
        assert refresh_job["if"] == "needs.declare.outputs.should_run == 'true'"
        assert refresh_job["needs"] == ["declare", "detect"]

    def test_env_upgrade_detect_still_treats_absence_as_a_first_provision_skip(self):
        job = _workflow(ENV_UPGRADE)["jobs"]["detect"]
        assert job["outputs"]["exists"] == "${{ steps.detect.outputs.exists }}"
        detect = _steps(job)["Detect existing resource group and AKS cluster"]["run"]
        assert "exists=false" in detect
        assert "exists=true" in detect
        quiesce = _workflow(ENV_UPGRADE)["jobs"]["quiesce-existing"]
        assert "needs.detect.outputs.exists == 'true'" in quiesce["if"]


class TestMaintenanceOrdering:
    def test_env_upgrade_first_provision_lets_spi_up_create_maintenance(self):
        provision_steps = _steps(_workflow(ENV_UPGRADE)["jobs"]["provision"])
        up_step = provision_steps["spi up"]["run"]
        assert "--tag" in up_step
        assert "--refresh-images" in up_step
        assert "--name-suffix" in up_step
        # First provision does not call maintenance set itself; spi up --tag
        # writes the deploy record with maintenance already enabled.
        assert "maintenance set" not in up_step

    def test_env_upgrade_existing_environment_sets_maintenance_before_snapshot(self):
        quiesce_steps = _workflow(ENV_UPGRADE)["jobs"]["quiesce-existing"]["steps"]
        names = [s["name"] for s in quiesce_steps if "name" in s]
        assert names.index("Set maintenance") < names.index("Snapshot image lock")
        assert names.index("spi connect") < names.index("Set maintenance")

    def test_env_upgrade_recovers_an_incomplete_first_provision(self):
        steps = _steps(_workflow(ENV_UPGRADE)["jobs"]["quiesce-existing"])
        detect = steps["Detect initialized deployment"]
        assert "spi-deploy-record" in detect["run"]
        assert "--ignore-not-found" in detect["run"]
        assert "initialized=false" in detect["run"]
        assert steps["spi connect"]["if"] == "steps.deployment.outputs.initialized == 'true'"
        assert steps["Set maintenance"]["if"] == ("steps.deployment.outputs.initialized == 'true'")

    def test_image_lock_snapshot_tolerates_genuine_absence(self):
        snapshot = _steps(_workflow(ENV_UPGRADE)["jobs"]["quiesce-existing"])[
            "Snapshot image lock"
        ]["run"]
        assert "--ignore-not-found" in snapshot
        assert "rm -f osdu-image-lock-snapshot.yaml" in snapshot

    def test_env_upgrade_verify_clears_maintenance_only_after_assertions_pass(self):
        verify_steps = _workflow(ENV_UPGRADE)["jobs"]["verify"]["steps"]
        names = [s["name"] for s in verify_steps if "name" in s]

        wait_idx = names.index("Wait for Flux Kustomizations to be Ready")
        gateway_idx = names.index("Acceptance probe (gateway reachable)")
        https_idx = names.index("Acceptance probe (HTTPS terminates)")
        assert_idx = names.index("Verify deployed stack matches the declaration")
        clear_idx = names.index("Clear maintenance")
        deployable_idx = names.index("Require deployable status")

        assert wait_idx < gateway_idx < https_idx < assert_idx < clear_idx < deployable_idx

        clear_step = next(s for s in verify_steps if s.get("name") == "Clear maintenance")
        assert clear_step["if"] == "steps.verify.outputs.clear == 'true'", (
            "maintenance must never be cleared from an if: always() failure path"
        )

    def test_env_refresh_quiesce_precedes_plain_reconcile(self):
        workflow = _workflow(ENV_REFRESH)
        assert workflow["jobs"]["declare"]["outputs"]["image_branch"] == (
            "${{ steps.export.outputs.image_branch }}"
        )
        refresh = workflow["jobs"]["refresh"]
        assert refresh["env"]["IMAGE_BRANCH"] == "${{ needs.declare.outputs.image_branch }}"

        refresh_steps = refresh["steps"]
        names = [s["name"] for s in refresh_steps if "name" in s]

        quiesce_idx = names.index("Quiesce (set maintenance)")
        reconcile_idx = names.index("spi reconcile")
        wait_idx = names.index("Wait for Flux Kustomizations to be Ready")
        assert_idx = names.index("Verify stack version and source suspension are unchanged")
        clear_idx = names.index("Clear maintenance")
        deployable_idx = names.index("Require deployable status")

        assert quiesce_idx < reconcile_idx < wait_idx < assert_idx < clear_idx < deployable_idx

        reconcile_step = next(s for s in refresh_steps if s.get("name") == "spi reconcile")
        # Plain reconcile: canonicals move in their own step.
        assert "--refresh-images" not in reconcile_step["run"]

        clear_step = next(s for s in refresh_steps if s.get("name") == "Clear maintenance")
        assert clear_step["if"] == "steps.verify.outputs.clear == 'true'", (
            "maintenance must never be cleared from an if: always() failure path"
        )

    def test_env_refresh_requires_deploy_record_before_setting_maintenance(self):
        # A provision can die after creating AKS but before writing the deploy
        # record; refresh has no provision job to recover with, so it must
        # fail closed before `spi maintenance set` reads the record.
        refresh_steps = _workflow(ENV_REFRESH)["jobs"]["refresh"]["steps"]
        names = [s["name"] for s in refresh_steps if "name" in s]

        connect_idx = names.index("spi connect")
        require_idx = names.index("Require an initialized deployment")
        quiesce_idx = names.index("Quiesce (set maintenance)")
        assert connect_idx < require_idx < quiesce_idx

        require_step = next(
            s for s in refresh_steps if s.get("name") == "Require an initialized deployment"
        )
        assert "spi-deploy-record" in require_step["run"]
        assert "--ignore-not-found" in require_step["run"]
        assert "exit 1" in require_step["run"]


class TestForkCanonicalRefresh:
    STEP = "Refresh fork-sourced canonicals"
    WAIT = "Wait for Flux Kustomizations to be Ready"

    def test_env_refresh_refreshes_fork_canonicals_before_the_wait(self):
        job = _workflow(ENV_REFRESH)["jobs"]["refresh"]
        steps = _steps(job)
        names = list(steps)
        step = steps[self.STEP]
        wait_seconds = re.search(r"--timeout (\d+)", steps[self.WAIT]["run"])

        assert names.index("spi reconcile") < names.index(self.STEP) < names.index(self.WAIT)
        assert step["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
        # A failed refresh fails the run, which leaves maintenance set.
        assert "if" not in step and "continue-on-error" not in step
        assert wait_seconds is not None
        budget = int(job["timeout-minutes"]) - int(wait_seconds.group(1)) // 60
        assert budget - int(step["timeout-minutes"]) >= 20

    def _run_step(self, tmp_path, stack_version: str, spi_exit: int = 0):
        """Run the step's script against a stub `spi` that echoes its arguments."""
        job = _workflow(ENV_REFRESH)["jobs"]["refresh"]
        stub = tmp_path / "spi"
        stub.write_text(f'#!/usr/bin/env bash\necho "spi $*"\nexit {spi_exit}\n', encoding="utf-8")
        stub.chmod(0o755)
        env = {
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "STACK_VERSION": stack_version,
            "FORK_REFRESH_CLI_MIN_VERSION": job["env"]["FORK_REFRESH_CLI_MIN_VERSION"],
        }
        return run_process(
            ["bash", "-c", _steps(job)[self.STEP]["run"]], env=env, capture_output=True, text=True
        )

    @pytest.mark.parametrize(
        ("stack_version", "refreshed"),
        [("v0.21.0", False), ("v0.22.0", True), ("v0.100.0", True)],
    )
    def test_fork_refresh_runs_only_on_a_cli_that_has_the_flag(
        self, tmp_path, stack_version, refreshed
    ):
        result = self._run_step(tmp_path, stack_version)

        assert result.returncode == 0
        assert ("spi service refresh --forks" in result.stdout.splitlines()) is refreshed
        assert ("::notice::" in result.stdout) is not refreshed

    def test_a_failed_fork_refresh_fails_the_step(self, tmp_path):
        assert self._run_step(tmp_path, "v0.22.0", spi_exit=3).returncode == 3


class TestLoadStep:
    STEP = "Load default datasets"
    JOBS = [(ENV_REFRESH, "refresh"), (ENV_UPGRADE, "verify")]

    @pytest.mark.parametrize(("path", "job_name"), JOBS, ids=["refresh", "upgrade"])
    def test_load_runs_after_the_environment_is_deployable(self, path, job_name):
        job = _workflow(path)["jobs"][job_name]
        steps = _steps(job)
        names = list(steps)
        step = steps[self.STEP]
        wait_seconds = re.search(
            r"--timeout (\d+)", steps["Wait for Flux Kustomizations to be Ready"]["run"]
        )

        assert (
            names.index("Clear maintenance")
            < names.index("Require deployable status")
            < names.index(self.STEP)
            < names.index("Capture diagnostics on failure")
        )
        # The registry ships only with the core profile.
        assert step["if"] == "needs.declare.outputs.profile == 'core'"
        # A failed load fails the run; it cannot set maintenance again.
        assert "continue-on-error" not in step
        assert wait_seconds is not None
        budget = int(job["timeout-minutes"]) - int(wait_seconds.group(1)) // 60
        assert budget - int(step["timeout-minutes"]) >= 20
        # The cached Azure token lasts an hour.
        assert int(step["timeout-minutes"]) < 60

    def _run_step(self, tmp_path, path, job_name, stack_version: str, spi_exit: int = 0):
        job = _workflow(path)["jobs"][job_name]
        stub = tmp_path / "spi"
        stub.write_text(f'#!/usr/bin/env bash\necho "spi $*"\nexit {spi_exit}\n', encoding="utf-8")
        stub.chmod(0o755)
        env = {
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "STACK_VERSION": stack_version,
            "LOAD_CLI_MIN_VERSION": job["env"]["LOAD_CLI_MIN_VERSION"],
        }
        return run_process(
            ["bash", "-c", _steps(job)[self.STEP]["run"]], env=env, capture_output=True, text=True
        )

    @pytest.mark.parametrize(("path", "job_name"), JOBS, ids=["refresh", "upgrade"])
    @pytest.mark.parametrize(
        ("stack_version", "loaded"),
        [("v0.26.0", False), ("v0.27.0", True), ("v0.100.0", True)],
    )
    def test_load_runs_only_on_a_cli_that_has_the_command(
        self, tmp_path, path, job_name, stack_version, loaded
    ):
        result = self._run_step(tmp_path, path, job_name, stack_version)

        assert result.returncode == 0
        assert ("spi load" in result.stdout.splitlines()) is loaded
        assert ("::notice::" in result.stdout) is not loaded

    @pytest.mark.parametrize("spi_exit", [1, 2])
    def test_a_failed_or_refused_load_fails_the_step(self, tmp_path, spi_exit):
        result = self._run_step(tmp_path, ENV_REFRESH, "refresh", "v0.27.0", spi_exit=spi_exit)

        assert result.returncode == spi_exit


class TestDeclaredForks:
    STEP = "Reconcile declared forks"

    def _run(self, tmp_path, script: str, **env: str):
        """Run a step's script against a stub `spi` that echoes its arguments."""
        stub = tmp_path / "spi"
        stub.write_text('#!/usr/bin/env bash\necho "spi $*"\n', encoding="utf-8")
        stub.chmod(0o755)
        env["PATH"] = f"{tmp_path}{os.pathsep}{os.environ['PATH']}"
        return run_process(["bash", "-c", script], env=env, capture_output=True, text=True)

    def test_env_refresh_reconciles_before_the_fork_refresh_reads_the_lock(self):
        steps = _steps(_workflow(ENV_REFRESH)["jobs"]["refresh"])
        names = list(steps)
        step = steps[self.STEP]

        assert (
            names.index("Quiesce (set maintenance)")
            < names.index(self.STEP)
            < names.index("Refresh fork-sourced canonicals")
        )
        assert step["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
        # A fork left unreconciled fails the run, which leaves maintenance set.
        assert "if" not in step and "continue-on-error" not in step

    @pytest.mark.parametrize(
        ("stack_version", "reconciled"), [("v0.23.1", False), ("v0.24.0", True)]
    )
    def test_env_refresh_reconciles_only_on_a_cli_that_has_the_option(
        self, tmp_path, stack_version, reconciled
    ):
        job = _workflow(ENV_REFRESH)["jobs"]["refresh"]

        result = self._run(
            tmp_path,
            _steps(job)[self.STEP]["run"],
            STACK_VERSION=stack_version,
            DECLARED_FORKS_CLI_MIN_VERSION=job["env"]["DECLARED_FORKS_CLI_MIN_VERSION"],
        )

        assert result.returncode == 0
        assert ("spi onboard --reconcile --write" in result.stdout.splitlines()) is reconciled

    @pytest.mark.parametrize(
        ("declares_forks", "stack_version", "handed_over"),
        [("true", "v0.24.0", True), ("true", "v0.23.1", False), ("false", "v0.24.0", False)],
    )
    def test_env_upgrade_hands_over_only_a_declaration_that_lists_forks(
        self, tmp_path, declares_forks, stack_version, handed_over
    ):
        # Handing over a declaration with no forks would revoke every fork onboarded by hand.
        workflow = _workflow(ENV_UPGRADE)
        job = workflow["jobs"]["provision"]
        assert job["env"]["DECLARES_FORKS"] == "${{ needs.declare.outputs.declares_forks }}"
        assert "declares_forks" in workflow["jobs"]["declare"]["outputs"]

        result = self._run(
            tmp_path,
            _steps(job)["spi up"]["run"],
            ENV_NAME="shared",
            PROFILE="core",
            LOCATION="westus3",
            INGRESS_MODE="azure",
            IMAGE_BRANCH="master",
            NAME_SUFFIX="a43c7",
            STACK_VERSION=stack_version,
            DECLARES_FORKS=declares_forks,
            DECLARED_FORKS_CLI_MIN_VERSION=job["env"]["DECLARED_FORKS_CLI_MIN_VERSION"],
            AZURE_IMAGES_CLI_MIN_VERSION=job["env"]["AZURE_IMAGES_CLI_MIN_VERSION"],
            GITHUB_REPOSITORY="Azure/osdu-spi-stack",
            DECLARATION_PATH=workflow["env"]["DECLARATION_PATH"],
        )

        assert result.returncode == 0, result.stderr
        locator = "--declaration Azure/osdu-spi-stack:ops/environments/shared.yaml"
        assert (locator in result.stdout) is handed_over


class TestAssertionsAndDeployability:
    def test_env_upgrade_verify_asserts_ref_resolved_commit_and_suspended(self):
        verify_steps = _steps(_workflow(ENV_UPGRADE)["jobs"]["verify"])
        assertion = verify_steps["Verify deployed stack matches the declaration"]["run"]
        assert ".environment.stackVersion" in assertion
        assert ".environment.resolvedCommit" in assertion
        assert ".suspended" in assertion
        assert "STACK_VERSION" in assertion

    def test_env_refresh_verify_asserts_ref_and_suspended(self):
        refresh_steps = _steps(_workflow(ENV_REFRESH)["jobs"]["refresh"])
        assertion = refresh_steps["Verify stack version and source suspension are unchanged"]["run"]
        assert ".environment.stackVersion" in assertion
        assert ".suspended" in assertion

    def test_env_upgrade_verify_requires_maintenance_as_the_sole_blocker(self):
        # Exit 2 from `spi status --json` covers every deployability blocker
        # (a non-ready Kustomization, a missing deploy record, or maintenance
        # itself), so only the JSON fields can tell a readiness regression
        # from a clean maintenance window.
        verify_steps = _steps(_workflow(ENV_UPGRADE)["jobs"]["verify"])
        assertion = verify_steps["Verify deployed stack matches the declaration"]["run"]
        assert ".ready" in assertion
        assert ".maintenance" in assertion
        assert ".reason.code" in assertion
        assert 'REASON_CODE" != "maintenance"' in assertion

    def test_env_refresh_verify_requires_maintenance_as_the_sole_blocker(self):
        refresh_steps = _steps(_workflow(ENV_REFRESH)["jobs"]["refresh"])
        assertion = refresh_steps["Verify stack version and source suspension are unchanged"]["run"]
        assert ".ready" in assertion
        assert ".maintenance" in assertion
        assert ".reason.code" in assertion
        assert 'REASON_CODE" != "maintenance"' in assertion

    def test_every_workflow_requires_deployable_status_as_the_final_gate(self):
        for path, job_name in (
            (ENV_UPGRADE, "verify"),
            (ENV_REFRESH, "refresh"),
            (ENV_RESET, "verify"),
        ):
            steps = _steps(_workflow(path)["jobs"][job_name])
            deployable_step = steps["Require deployable status"]
            assert "spi status --json" in deployable_step["run"]
            assert "if" not in deployable_step and "continue-on-error" not in deployable_step
            assert int(deployable_step["env"]["DEPLOYABLE_TIMEOUT_SECONDS"]) <= 600
            assert int(deployable_step["timeout-minutes"]) * 60 > int(
                deployable_step["env"]["DEPLOYABLE_TIMEOUT_SECONDS"]
            )

    def test_bare_profile_skips_gateway_probes(self):
        for path, job_name in (
            (ENV_UPGRADE, "verify"),
            (ENV_REFRESH, "refresh"),
            (ENV_RESET, "verify"),
        ):
            steps = _steps(_workflow(path)["jobs"][job_name])
            assert steps["Acceptance probe (gateway reachable)"]["if"] == (
                "needs.declare.outputs.profile != 'bare'"
            )
            assert steps["Acceptance probe (HTTPS terminates)"]["if"] == (
                "needs.declare.outputs.profile != 'bare'"
            )


JOBS = [
    (ENV_UPGRADE, "verify", "Verify deployed stack matches the declaration"),
    (ENV_REFRESH, "refresh", "Verify stack version and source suspension are unchanged"),
    (ENV_RESET, "verify", "Verify deployed stack matches the declaration"),
]
JOB_IDS = ["upgrade", "refresh", "reset"]


def _stub_status(tmp_path: Path, responses: list[tuple[int, dict[str, Any]]]) -> None:
    """Stub `spi status --json` to answer each call with the next response, then the last."""
    for index, (code, payload) in enumerate(responses):
        (tmp_path / f"status-{index}.json").write_text(json.dumps(payload), encoding="utf-8")
        (tmp_path / f"status-{index}.code").write_text(str(code), encoding="utf-8")
    last = len(responses) - 1
    stub = tmp_path / "spi"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'n=$(cat "{tmp_path}/calls" 2>/dev/null || echo 0)\n'
        f'echo $((n + 1)) > "{tmp_path}/calls"\n'
        f'[ "$n" -gt {last} ] && n={last}\n'
        f'cat "{tmp_path}/status-$n.json"\n'
        f'exit "$(cat "{tmp_path}/status-$n.code")"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _status(*, maintenance: bool, deployable: bool, reason: str | None) -> dict[str, Any]:
    return {
        "ready": True,
        "deployable": deployable,
        "maintenance": maintenance,
        "suspended": True,
        "reason": {"code": reason, "message": f"{reason} message"} if reason else None,
        "environment": {"stackVersion": "v0.28.5", "resolvedCommit": "abc123"},
    }


class TestVerifyOnReRun:
    def _run(self, tmp_path, script: str, attempt: str, status: dict[str, Any]):
        _stub_status(tmp_path, [(0 if status["deployable"] else 2, status)])
        output = tmp_path / "github_output"
        output.write_text("", encoding="utf-8")
        env = {
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "STACK_VERSION": "v0.28.5",
            "GITHUB_RUN_ATTEMPT": attempt,
            "GITHUB_OUTPUT": str(output),
        }
        result = run_process(["bash", "-c", script], env=env, capture_output=True, text=True)
        return result, output.read_text(encoding="utf-8")

    @pytest.mark.parametrize(("path", "job_name", "step"), JOBS, ids=JOB_IDS)
    def test_first_attempt_in_maintenance_clears_it(self, tmp_path, path, job_name, step):
        script = _steps(_workflow(path)["jobs"][job_name])[step]["run"]
        status = _status(maintenance=True, deployable=False, reason="maintenance")

        result, output = self._run(tmp_path, script, "1", status)

        assert result.returncode == 0, result.stdout
        assert "clear=true" in output.splitlines()

    @pytest.mark.parametrize(("path", "job_name", "step"), JOBS, ids=JOB_IDS)
    def test_re_run_after_maintenance_cleared_skips_the_clear(self, tmp_path, path, job_name, step):
        script = _steps(_workflow(path)["jobs"][job_name])[step]["run"]
        status = _status(maintenance=False, deployable=True, reason=None)

        result, output = self._run(tmp_path, script, "2", status)

        assert result.returncode == 0, result.stdout
        assert "clear=false" in output.splitlines()

    @pytest.mark.parametrize(("path", "job_name", "step"), JOBS, ids=JOB_IDS)
    def test_first_attempt_with_maintenance_off_fails(self, tmp_path, path, job_name, step):
        script = _steps(_workflow(path)["jobs"][job_name])[step]["run"]
        status = _status(maintenance=False, deployable=True, reason=None)

        result, output = self._run(tmp_path, script, "1", status)

        assert result.returncode == 1
        assert "maintenance flag is not set" in result.stdout
        assert "clear=" not in output

    @pytest.mark.parametrize(("path", "job_name", "step"), JOBS, ids=JOB_IDS)
    def test_re_run_with_maintenance_off_and_not_deployable_fails(
        self, tmp_path, path, job_name, step
    ):
        script = _steps(_workflow(path)["jobs"][job_name])[step]["run"]
        status = _status(maintenance=False, deployable=False, reason="kustomization_not_ready")

        result, output = self._run(tmp_path, script, "2", status)

        assert result.returncode == 1
        assert "clear=" not in output


class TestRequireDeployablePolls:
    STEP = "Require deployable status"

    def _run(self, tmp_path, path, job_name, responses, timeout: str = "30"):
        _stub_status(tmp_path, responses)
        env = {
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "DEPLOYABLE_TIMEOUT_SECONDS": timeout,
            "DEPLOYABLE_POLL_SECONDS": "0",
        }
        script = _steps(_workflow(path)["jobs"][job_name])[self.STEP]["run"]
        result = run_process(["bash", "-c", script], env=env, capture_output=True, text=True)
        calls = int((tmp_path / "calls").read_text(encoding="utf-8"))
        return result, calls

    @pytest.mark.parametrize(("path", "job_name", "_step"), JOBS, ids=JOB_IDS)
    def test_a_transient_progressing_read_is_retried(self, tmp_path, path, job_name, _step):
        progressing = _status(maintenance=False, deployable=False, reason="kustomization_not_ready")
        deployable = _status(maintenance=False, deployable=True, reason=None)

        result, calls = self._run(tmp_path, path, job_name, [(2, progressing), (0, deployable)])

        assert result.returncode == 0, result.stdout
        assert calls == 2
        assert "kustomization_not_ready message" in result.stdout

    @pytest.mark.parametrize(("path", "job_name", "_step"), JOBS, ids=JOB_IDS)
    def test_not_deployable_past_the_deadline_fails(self, tmp_path, path, job_name, _step):
        progressing = _status(maintenance=False, deployable=False, reason="kustomization_not_ready")

        result, _ = self._run(tmp_path, path, job_name, [(2, progressing)], timeout="0")

        assert result.returncode == 2
        assert "not deployable after 0s" in result.stdout

    @pytest.mark.parametrize(("path", "job_name", "_step"), JOBS, ids=JOB_IDS)
    def test_a_contract_read_error_fails_without_retry(self, tmp_path, path, job_name, _step):
        result, calls = self._run(tmp_path, path, job_name, [(1, {"error": "boom"})])

        assert result.returncode == 1
        assert calls == 1


class TestDiagnostics:
    def test_every_azure_job_captures_diagnostics_on_failure(self):
        for path in LIFECYCLE:
            workflow = _workflow(path)
            for name, job in workflow["jobs"].items():
                run_text = "\n".join(step.get("run", "") for step in job.get("steps", []))
                if "gh release download" not in run_text:
                    continue
                steps = _steps(job)
                diag_step = steps.get("Capture diagnostics on failure")
                assert diag_step, f"{path.name}:{name} must capture diagnostics on failure"
                assert diag_step["if"] == "failure()"
                assert "capture_diagnostics.sh" in diag_step["run"]


class TestVerifyReleaseAsset:
    def test_env_upgrade_verifies_the_release_and_wheel_asset_exist(self):
        job = _workflow(ENV_UPGRADE)["jobs"]["verify-release"]
        step = _steps(job)["Verify the release and wheel asset exist"]
        assert "gh release view" in step["run"]
        assert "py3-none-any.whl" in step["run"]
        assert job["needs"] == "declare"

    def test_env_upgrade_refuses_wheels_without_the_environment_block(self):
        """The jq paths read `.environment`, which v0.10.0 first published."""
        job = _workflow(ENV_UPGRADE)["jobs"]["verify-release"]
        assert job["env"]["LIFECYCLE_CLI_MIN_VERSION"] == "v0.10.0"

    def test_env_refresh_checks_the_floor_before_setting_maintenance(self):
        job = _workflow(ENV_REFRESH)["jobs"]["refresh"]
        assert job["env"]["LIFECYCLE_CLI_MIN_VERSION"] == "v0.10.0"
        steps = list(_steps(job))
        floor = steps.index("Refuse a declaration older than the lifecycle CLI")
        maintenance = next(i for i, name in enumerate(steps) if "maintenance" in name.lower())
        assert floor < maintenance


class TestRevisionGatedConvergence:
    """An upgrade re-points the source while every Kustomization is still
    Ready for the revision being replaced, so a bare Ready wait can pass
    before any of the new revision is applied."""

    def test_wait_script_supports_an_expected_revision(self):
        script = (REPO_ROOT / "scripts" / "wait_for_flux_ready.sh").read_text(encoding="utf-8")
        assert "--expect-revision" in script
        assert "lastAppliedRevision" in script

    def test_verify_resolves_the_revision_before_waiting_on_it(self):
        verify_steps = _workflow(ENV_UPGRADE)["jobs"]["verify"]["steps"]
        names = [s["name"] for s in verify_steps if "name" in s]
        assert names.index("Resolve the upgraded source revision") < names.index(
            "Wait for Flux Kustomizations to be Ready"
        )

        steps = _steps(_workflow(ENV_UPGRADE)["jobs"]["verify"])
        resolve = steps["Resolve the upgraded source revision"]
        assert resolve["id"] == "revision"
        assert ".environment.resolvedCommit" in resolve["run"]
        # An absent commit must fail the job, never wait on an empty string,
        # which the script treats as "no revision expected".
        assert "exit 1" in resolve["run"]

        wait = steps["Wait for Flux Kustomizations to be Ready"]
        assert wait["env"]["EXPECT_REVISION"] == "${{ steps.revision.outputs.revision }}"
        assert '--expect-revision "$EXPECT_REVISION"' in wait["run"]


class TestBareProfileProvisioning:
    def test_spi_up_omits_ingress_mode_for_a_bare_declaration(self):
        # ingressMode is a required declaration key, but `spi up` rejects
        # --ingress-mode with `bare`, which deploys no ingress substrate.
        up_step = _steps(_workflow(ENV_UPGRADE)["jobs"]["provision"])["spi up"]["run"]
        assert '[ "$PROFILE" != "bare" ]' in up_step
        assert "ARGS+=(--ingress-mode" in up_step


class TestWaitScriptExactVersionUse:
    def test_wait_for_flux_ready_prefers_installed_spi(self):
        script = (REPO_ROOT / "scripts" / "wait_for_flux_ready.sh").read_text(encoding="utf-8")
        match = re.search(
            r"command -v spi.*?\n.*?spi status.*?\n.*?command -v uv.*?\n.*?uv run spi status",
            script,
            re.DOTALL,
        )
        assert match, (
            "wait_for_flux_ready.sh must prefer an installed spi and only fall "
            "back to uv run spi when it is absent"
        )

    def test_diagnostics_prefers_installed_spi(self):
        script = CAPTURE_DIAGNOSTICS.read_text(encoding="utf-8")
        match = re.search(
            r"command -v spi.*?\n.*?spi status.*?\n.*?command -v uv.*?\n.*?uv run spi status",
            script,
            re.DOTALL,
        )
        assert match, (
            "capture_diagnostics.sh must diagnose with the installed release CLI "
            "before falling back to source execution"
        )


class TestReleaseBumpSafeguards:
    def test_release_please_excludes_the_environment_declaration_path(self):
        config = json.loads(RELEASE_PLEASE_CONFIG.read_text(encoding="utf-8"))
        assert config["packages"]["."]["exclude-paths"] == ["ops/environments"]

    def test_bump_job_only_runs_after_a_real_release_push_with_successful_assets(self):
        job = _workflow(RELEASE_WORKFLOW)["jobs"]["bump-environment"]

        assert job["needs"] == ["release-please", "assets"]
        condition = job["if"]
        assert "github.event_name == 'push'" in condition
        assert "needs.release-please.outputs.release_created == 'true'" in condition
        assert "needs.assets.result == 'success'" in condition

    def test_bump_job_uses_a_fresh_app_token(self):
        steps = _steps(_workflow(RELEASE_WORKFLOW)["jobs"]["bump-environment"])
        token_step = steps["Generate GitHub App token"]
        assert token_step["uses"].startswith("actions/create-github-app-token@")
        assert token_step["with"]["app-id"] == "${{ secrets.RELEASE_APP_ID }}"

    def test_bump_job_skips_when_declaration_absent_or_already_current(self):
        steps = _steps(_workflow(RELEASE_WORKFLOW)["jobs"]["bump-environment"])
        gate_step = steps["Decide whether a bump PR is needed"]["run"]
        assert "declaration_found" in gate_step
        assert "proceed=false" in gate_step
        # Both the absent-declaration and already-current cases must skip.
        assert gate_step.count("proceed=false") == 2

        pr_step = steps["Open or advance the stackVersion bump PR"]
        assert pr_step["if"] == "steps.gate.outputs.proceed == 'true'"

    def test_bump_pr_touches_only_the_declaration_file(self):
        steps = _steps(_workflow(RELEASE_WORKFLOW)["jobs"]["bump-environment"])
        pr_step_run = steps["Open or advance the stackVersion bump PR"]["run"]

        assert "git diff --cached --name-only" in pr_step_run
        assert 'CHANGED" != "$DECL"' in pr_step_run
        assert "stackVersion:" in pr_step_run
        assert "git add" in pr_step_run

    def test_bump_commit_and_pr_title_follow_conventional_commits(self):
        steps = _steps(_workflow(RELEASE_WORKFLOW)["jobs"]["bump-environment"])
        pr_step_run = steps["Open or advance the stackVersion bump PR"]["run"]

        assert re.search(r"git commit -m \"chore\(env\): bump shared stackVersion", pr_step_run)
        assert re.search(r"TITLE=\"chore\(env\): bump shared stackVersion", pr_step_run)
        assert pr_step_run.count('--title "$TITLE"') == 2

    def test_bump_pr_rolls_forward_on_one_branch(self):
        steps = _steps(_workflow(RELEASE_WORKFLOW)["jobs"]["bump-environment"])
        pr_step_run = steps["Open or advance the stackVersion bump PR"]["run"]

        assert 'BRANCH="env-bump/shared"' in pr_step_run
        assert "gh pr create" in pr_step_run
        assert 'gh pr edit "$PR"' in pr_step_run


class TestTagRuleset:
    def test_tag_ruleset_targets_release_tags_and_restricts_update_and_deletion(self):
        ruleset = json.loads(TAG_RULESET.read_text(encoding="utf-8"))

        assert ruleset["target"] == "tag"
        assert ruleset["enforcement"] == "active"
        assert ruleset["conditions"]["ref_name"]["include"] == ["refs/tags/v*"]

        rule_types = {rule["type"] for rule in ruleset["rules"]}
        assert rule_types == {"update", "deletion"}
        # Creation must stay allowed so release-please can create new tags.
        assert "creation" not in rule_types


class TestImageBranchHandoff:
    """A declared imageBranch reaches only a CLI that still takes --image-branch."""

    def _run(self, tmp_path, script: str, **env: str):
        stub = tmp_path / "spi"
        stub.write_text('#!/usr/bin/env bash\necho "spi $*"\n', encoding="utf-8")
        stub.chmod(0o755)
        env["PATH"] = f"{tmp_path}{os.pathsep}{os.environ['PATH']}"
        return run_process(["bash", "-c", script], env=env, capture_output=True, text=True)

    @pytest.mark.parametrize(
        ("stack_version", "image_branch", "passed"),
        [
            ("v0.29.0", "release-0-30", True),
            ("v0.30.0", "release-0-30", False),
            ("v0.29.0", "", False),
        ],
    )
    def test_env_refresh_reconcile(self, tmp_path, stack_version, image_branch, passed):
        job = _workflow(ENV_REFRESH)["jobs"]["refresh"]

        result = self._run(
            tmp_path,
            _steps(job)["spi reconcile"]["run"],
            STACK_VERSION=stack_version,
            IMAGE_BRANCH=image_branch,
            AZURE_IMAGES_CLI_MIN_VERSION=job["env"]["AZURE_IMAGES_CLI_MIN_VERSION"],
        )

        assert result.returncode == 0, result.stderr
        assert ("--image-branch release-0-30" in result.stdout) is passed

    @pytest.mark.parametrize(("stack_version", "passed"), [("v0.29.0", True), ("v0.30.0", False)])
    def test_env_upgrade_up(self, tmp_path, stack_version, passed):
        job = _workflow(ENV_UPGRADE)["jobs"]["provision"]

        result = self._run(
            tmp_path,
            _steps(job)["spi up"]["run"],
            ENV_NAME="shared",
            PROFILE="core",
            LOCATION="westus3",
            INGRESS_MODE="azure",
            IMAGE_BRANCH="release-0-30",
            NAME_SUFFIX="a43c7",
            STACK_VERSION=stack_version,
            DECLARES_FORKS="false",
            DECLARED_FORKS_CLI_MIN_VERSION=job["env"]["DECLARED_FORKS_CLI_MIN_VERSION"],
            AZURE_IMAGES_CLI_MIN_VERSION=job["env"]["AZURE_IMAGES_CLI_MIN_VERSION"],
        )

        assert result.returncode == 0, result.stderr
        assert ("--image-branch release-0-30" in result.stdout) is passed


DRAIN_SCRIPT = REPO_ROOT / "scripts" / "drain_ephemeral_pins.sh"


def _run_script(tmp_path: Path, script: str, **env: str):
    """Run a step's script with tmp_path first on PATH, where its stubs live."""
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    env["PATH"] = f"{tmp_path}{os.pathsep}{os.environ['PATH']}"
    env["GITHUB_OUTPUT"] = str(output)
    result = run_process(["bash", "-c", script], env=env, capture_output=True, text=True)
    return result, output.read_text(encoding="utf-8")


class TestEnvResetTriggerAndGate:
    GATE = "Gate on declaration presence and confirmation"

    def test_env_reset_runs_only_on_a_confirmed_dispatch(self):
        workflow = _workflow(ENV_RESET)

        assert "schedule" not in workflow["on"]
        confirm = workflow["on"]["workflow_dispatch"]["inputs"]["confirm"]
        assert confirm["required"] == "true"
        assert confirm["type"] == "string"
        assert "push" not in workflow["on"]

    @pytest.mark.parametrize(
        ("found", "event", "confirm", "exit_code", "should_run"),
        [
            ("true", "schedule", "", 0, "true"),
            ("true", "workflow_dispatch", "shared", 0, "true"),
            ("true", "workflow_dispatch", "", 1, None),
            ("true", "workflow_dispatch", "Shared", 1, None),
            ("false", "workflow_dispatch", "", 0, "false"),
            ("false", "schedule", "", 0, "false"),
        ],
    )
    def test_a_dispatch_runs_only_when_confirmed_with_the_declared_name(
        self, tmp_path, found, event, confirm, exit_code, should_run
    ):
        script = _steps(_workflow(ENV_RESET)["jobs"]["declare"])[self.GATE]["run"]

        result, output = _run_script(
            tmp_path,
            script,
            DECLARATION_PATH="ops/environments/shared.yaml",
            DECLARATION_FOUND=found,
            DECLARED_ENV="shared",
            EVENT_NAME=event,
            CONFIRM=confirm,
        )

        assert result.returncode == exit_code, result.stdout
        if should_run is None:
            assert "should_run=" not in output
        else:
            assert f"should_run={should_run}" in output.splitlines()

    def test_every_job_gates_on_should_run(self):
        for name, job in _workflow(ENV_RESET)["jobs"].items():
            if name == "declare":
                continue
            assert "needs.declare.outputs.should_run == 'true'" in job["if"], name

    def test_the_release_is_verified_before_anything_is_deleted(self):
        jobs = _workflow(ENV_RESET)["jobs"]
        assert "gh release view" in _all_run_text({"jobs": {"v": jobs["verify-release"]}})
        assert "verify-release" in jobs["detect"]["needs"]
        assert "verify-release" in jobs["teardown"]["needs"]
        assert "needs.verify-release.result == 'success'" in jobs["teardown"]["if"]


class TestEnvResetDetect:
    STEP = "Detect existing resource group and AKS cluster"

    def _run(self, tmp_path, rg_exists: str, aks_count: str):
        stub = tmp_path / "az"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'case "$1" in group) echo {rg_exists} ;; aks) echo {aks_count} ;; esac\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
        script = _steps(_workflow(ENV_RESET)["jobs"]["detect"])[self.STEP]["run"]
        return _run_script(tmp_path, script, ENV_NAME="shared")

    def test_a_missing_resource_group_fails_rather_than_minting_new_identities(self, tmp_path):
        result, output = self._run(tmp_path, "false", "0")

        assert result.returncode == 1
        assert "env-upgrade" in result.stdout
        assert output == ""

    @pytest.mark.parametrize(("aks_count", "exists"), [("1", "true"), ("0", "false")])
    def test_a_missing_cluster_resumes_at_teardown(self, tmp_path, aks_count, exists):
        result, output = self._run(tmp_path, "true", aks_count)

        assert result.returncode == 0, result.stdout
        assert f"cluster_exists={exists}" in output.splitlines()


class TestEnvResetRetainedTags:
    """spi up refuses a suffix or locator the group does not retain, so detect
    checks both before anything is deleted."""

    STEP = "Require the retained tags to match the declaration"
    LOCATOR = "Azure/osdu-spi-stack:ops/environments/shared.yaml"

    def _run(self, tmp_path, tags: dict[str, str] | None, declares_forks: str = "true"):
        (tmp_path / "tags.json").write_text(json.dumps(tags), encoding="utf-8")
        stub = tmp_path / "az"
        stub.write_text(f'#!/usr/bin/env bash\ncat "{tmp_path}/tags.json"\n', encoding="utf-8")
        stub.chmod(0o755)
        workflow = _workflow(ENV_RESET)
        script = _steps(workflow["jobs"]["detect"])[self.STEP]["run"]
        return _run_script(
            tmp_path,
            script,
            ENV_NAME="shared",
            NAME_SUFFIX="a43c7",
            DECLARES_FORKS=declares_forks,
            GITHUB_REPOSITORY="Azure/osdu-spi-stack",
            DECLARATION_PATH=workflow["env"]["DECLARATION_PATH"],
        )

    def test_the_check_runs_before_any_job_that_deletes(self):
        jobs = _workflow(ENV_RESET)["jobs"]
        names = list(_steps(jobs["detect"]))

        assert names.index("Detect existing resource group and AKS cluster") < names.index(
            self.STEP
        )
        assert "detect" in jobs["quiesce"]["needs"] and "detect" in jobs["teardown"]["needs"]

    @pytest.mark.parametrize(
        "locator", [LOCATOR, "azure/OSDU-SPI-STACK:ops/environments/shared.yaml", None]
    )
    def test_matching_tags_pass(self, tmp_path, locator):
        tags = {"spi-name-suffix": "a43c7"}
        if locator:
            tags["spi-environment-declaration"] = locator

        result, _ = self._run(tmp_path, tags)

        assert result.returncode == 0, result.stdout

    @pytest.mark.parametrize("tags", [{"spi-name-suffix": "b12d9"}, {"spi-name-suffix": ""}, None])
    def test_a_suffix_the_group_does_not_retain_stops_the_reset(self, tmp_path, tags):
        result, _ = self._run(tmp_path, tags)

        assert result.returncode == 1
        assert "declares nameSuffix 'a43c7'" in result.stdout

    @pytest.mark.parametrize(
        "locator",
        [
            "contoso/osdu-spi-stack:ops/environments/shared.yaml",
            "Azure/osdu-spi-stack:ops/environments/dks.yaml",
        ],
    )
    def test_a_different_declaration_stops_the_reset(self, tmp_path, locator):
        tags = {"spi-name-suffix": "a43c7", "spi-environment-declaration": locator}

        result, _ = self._run(tmp_path, tags)

        assert result.returncode == 1
        assert f"declared by {locator}" in result.stdout

    def test_an_undeclared_rebuild_ignores_the_retained_locator(self, tmp_path):
        tags = {"spi-name-suffix": "a43c7", "spi-environment-declaration": "other/repo:x.yaml"}

        result, _ = self._run(tmp_path, tags, declares_forks="false")

        assert result.returncode == 0, result.stdout


class TestEnvResetQuiesceAndDrain:
    def test_maintenance_is_set_and_the_drain_holds_before_the_snapshot(self):
        job = _workflow(ENV_RESET)["jobs"]["quiesce"]
        steps = _steps(job)
        names = list(steps)

        assert (
            names.index("spi connect")
            < names.index("Set maintenance")
            < names.index("Sweep stale pins and drain fork deploys")
            < names.index("Snapshot image lock")
        )
        assert steps["Set maintenance"]["run"] == "spi maintenance set"
        drain = steps["Sweep stale pins and drain fork deploys"]
        assert "scripts/drain_ephemeral_pins.sh" in drain["run"]
        assert '--window "$DRAIN_WINDOW_SECONDS"' in drain["run"]
        assert '--quiescence "$QUIESCENCE_SECONDS"' in drain["run"]
        assert '--call-timeout "$DRAIN_CALL_TIMEOUT_SECONDS"' in drain["run"]
        # The sweep reads each pin's owning run.
        assert drain["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
        # A drain that runs out fails the job with maintenance still set.
        assert "continue-on-error" not in drain
        assert "cluster_exists == 'true'" in job["if"]

    def test_the_drain_fits_the_job_and_the_cached_token(self):
        job = _workflow(ENV_RESET)["jobs"]["quiesce"]
        drain_minutes = _drain_seconds(job["env"]) / 60

        assert drain_minutes + 10 <= int(job["timeout-minutes"]) < 60

    def test_teardown_follows_only_a_drained_or_absent_cluster(self):
        jobs = _workflow(ENV_RESET)["jobs"]
        teardown = jobs["teardown"]

        assert "quiesce" in teardown["needs"]
        condition = " ".join(teardown["if"].split())
        assert condition.startswith("!cancelled() &&")
        assert "always()" not in condition
        assert (
            "(needs.quiesce.result == 'success' || (needs.quiesce.result == 'skipped' "
            "&& needs.detect.outputs.cluster_exists == 'false'))"
        ) in condition
        assert jobs["provision"]["needs"] == ["declare", "teardown"]
        assert "needs.teardown.result == 'success'" in jobs["provision"]["if"]
        assert jobs["verify"]["needs"] == ["declare", "provision"]
        assert "needs.provision.result == 'success'" in jobs["verify"]["if"]


class TestEnvResetTeardown:
    STEP = "Confirm only managed identities remain"
    IDENTITY = {"type": "Microsoft.ManagedIdentity/userAssignedIdentities", "name": "osdu-identity"}

    def test_spi_down_runs_within_its_deletion_deadline(self):
        from spi.teardown import TEARDOWN_DEADLINE_SECONDS

        job = _workflow(ENV_RESET)["jobs"]["teardown"]
        steps = _steps(job)
        names = list(steps)

        assert steps["spi down"]["run"] == 'spi down --env "$ENV_NAME" --force'
        assert "--purge" not in _all_run_text({"jobs": {"t": job}})
        assert names.index("spi down") < names.index(self.STEP)
        assert TEARDOWN_DEADLINE_SECONDS // 60 + 10 <= int(job["timeout-minutes"]) < 60 + 10

    def _run(self, tmp_path, inventory: list[dict[str, str]], nodes_exist: str):
        (tmp_path / "inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
        stub = tmp_path / "az"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'case "$1" in resource) cat "{tmp_path}/inventory.json" ;; '
            f"group) echo {nodes_exist} ;; esac\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        script = _steps(_workflow(ENV_RESET)["jobs"]["teardown"])[self.STEP]["run"]
        return _run_script(tmp_path, script, ENV_NAME="shared")

    def test_identities_alone_with_the_nodes_group_gone_pass(self, tmp_path):
        result, _ = self._run(tmp_path, [self.IDENTITY, self.IDENTITY], "false")

        assert result.returncode == 0, result.stdout
        assert "2 managed identities" in result.stdout

    def test_a_leftover_resource_stops_the_reset(self, tmp_path):
        leftover = {"type": "Microsoft.DocumentDB/databaseAccounts", "name": "osdu-cosmos"}

        result, _ = self._run(tmp_path, [self.IDENTITY, leftover], "false")

        assert result.returncode == 1
        assert "Microsoft.DocumentDB/databaseAccounts osdu-cosmos" in result.stdout

    def test_a_group_without_identities_stops_the_reset(self, tmp_path):
        result, _ = self._run(tmp_path, [], "false")

        assert result.returncode == 1
        assert "no managed identities" in result.stdout

    def test_a_surviving_nodes_group_stops_the_reset(self, tmp_path):
        result, _ = self._run(tmp_path, [self.IDENTITY], "true")

        assert result.returncode == 1
        assert "spi-stack-shared-nodes still exists" in result.stdout


class TestEnvResetRebuild:
    def _up(self, tmp_path, declares_forks: str, profile: str = "core"):
        stub = tmp_path / "spi"
        stub.write_text('#!/usr/bin/env bash\necho "spi $*"\n', encoding="utf-8")
        stub.chmod(0o755)
        workflow = _workflow(ENV_RESET)
        script = _steps(workflow["jobs"]["provision"])["spi up"]["run"]
        return _run_script(
            tmp_path,
            script,
            ENV_NAME="shared",
            PROFILE=profile,
            LOCATION="westus3",
            INGRESS_MODE="azure",
            NAME_SUFFIX="a43c7",
            STACK_VERSION="v0.32.3",
            DECLARES_FORKS=declares_forks,
            GITHUB_REPOSITORY="Azure/osdu-spi-stack",
            DECLARATION_PATH=workflow["env"]["DECLARATION_PATH"],
        )

    @pytest.mark.parametrize(("declares_forks", "handed_over"), [("true", True), ("false", False)])
    def test_spi_up_rebuilds_from_the_declaration(self, tmp_path, declares_forks, handed_over):
        result, _ = self._up(tmp_path, declares_forks)

        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith(
            "spi up --env shared --profile core --location westus3 --name-suffix a43c7 "
            "--tag v0.32.3 --refresh-images --ingress-mode azure"
        )
        locator = "--declaration Azure/osdu-spi-stack:ops/environments/shared.yaml"
        assert (locator in result.stdout) is handed_over
        assert "--image-branch" not in result.stdout

    def test_spi_up_omits_ingress_mode_for_a_bare_declaration(self, tmp_path):
        result, _ = self._up(tmp_path, "false", profile="bare")

        assert result.returncode == 0, result.stderr
        assert "--ingress-mode" not in result.stdout

    def test_verify_waits_probes_clears_then_loads(self):
        job = _workflow(ENV_RESET)["jobs"]["verify"]
        steps = _steps(job)
        names = list(steps)

        assert (
            names.index("Resolve the deployed source revision")
            < names.index("Wait for Flux Kustomizations to be Ready")
            < names.index("Verify deployed stack matches the declaration")
            < names.index("Clear maintenance")
            < names.index("Require deployable status")
            < names.index("Load default datasets")
            < names.index("Capture diagnostics on failure")
        )
        wait = steps["Wait for Flux Kustomizations to be Ready"]
        assert '--expect-revision "$EXPECT_REVISION"' in wait["run"]
        assert steps["Clear maintenance"]["if"] == "steps.verify.outputs.clear == 'true'"
        load = steps["Load default datasets"]
        assert load["run"] == "spi load"
        assert load["if"] == "needs.declare.outputs.profile == 'core'"
        assert "continue-on-error" not in load

    def test_the_reset_fits_its_seven_hour_budget(self):
        """Every bounded phase at its cap, with the 75-minute cold provision the
        lifecycle design budgets and minutes left over for job setup."""
        from spi.teardown import TEARDOWN_DEADLINE_SECONDS

        jobs = _workflow(ENV_RESET)["jobs"]
        verify = _steps(jobs["verify"])
        wait = re.search(
            r"--timeout (\d+)", verify["Wait for Flux Kustomizations to be Ready"]["run"]
        )
        assert wait is not None
        probe = (REPO_ROOT / "scripts" / "probe_gateway.sh").read_text(encoding="utf-8")
        assert "seq 1 20" in probe and "--max-time 15" in probe and "sleep 30" in probe
        https_probe_seconds = 20 * (15 + 30)
        deployable = verify["Require deployable status"]["env"]["DEPLOYABLE_TIMEOUT_SECONDS"]

        minutes = (
            _drain_seconds(jobs["quiesce"]["env"]) / 60
            + TEARDOWN_DEADLINE_SECONDS / 60
            + 75
            + int(wait.group(1)) / 60
            + https_probe_seconds / 60
            + int(deployable) / 60
            + int(verify["Load default datasets"]["timeout-minutes"])
        )

        assert minutes <= 7 * 60 - 5


def _drain_seconds(env: dict[str, str]) -> int:
    """The drain's longest run: a final poll lands on its deadline, then each
    of its two calls may run to the call timeout."""
    return (
        int(env["DRAIN_WINDOW_SECONDS"])
        + int(env["QUIESCENCE_SECONDS"])
        + 2 * int(env["DRAIN_CALL_TIMEOUT_SECONDS"])
    )


GNU_TIMEOUT = shutil.which("timeout") or shutil.which("gtimeout")


class TestDrainScript:
    """scripts/drain_ephemeral_pins.sh against a stub `spi` whose sweep and
    status answers advance one per poll, then repeat the last."""

    PIN = {"ephemeral": True, "runId": 39864717423, "sourceRepo": "Azure/osdu-spi-search"}

    def _run(self, tmp_path, polls, *args: str):
        for index, (sweep_code, sweep, status_code, status) in enumerate(polls):
            (tmp_path / f"sweep-{index}").write_text(
                f"panel output\n{json.dumps(sweep)}\n", encoding="utf-8"
            )
            (tmp_path / f"sweep-{index}.code").write_text(str(sweep_code), encoding="utf-8")
            (tmp_path / f"status-{index}").write_text(json.dumps(status), encoding="utf-8")
            (tmp_path / f"status-{index}.code").write_text(str(status_code), encoding="utf-8")
        if not GNU_TIMEOUT:
            passthrough = tmp_path / "timeout"
            passthrough.write_text('#!/usr/bin/env bash\nshift\nexec "$@"\n', encoding="utf-8")
            passthrough.chmod(0o755)
        last = len(polls) - 1
        stub = tmp_path / "spi"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'kind=sweep; [ "$1" = status ] && kind=status\n'
            f'n=$(cat "{tmp_path}/$kind.calls" 2>/dev/null || echo 0)\n'
            f'echo $((n + 1)) > "{tmp_path}/$kind.calls"\n'
            f'[ "$n" -gt {last} ] && n={last}\n'
            f'[ -f "{tmp_path}/$kind-$n.sleep" ] && sleep "$(cat "{tmp_path}/$kind-$n.sleep")"\n'
            f'cat "{tmp_path}/$kind-$n"\n'
            f'exit "$(cat "{tmp_path}/$kind-$n.code")"\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
        env = {"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
        result = run_process(
            ["bash", str(DRAIN_SCRIPT), "--poll", "0", *args],
            env=env,
            capture_output=True,
            text=True,
        )
        calls = tmp_path / "status.calls"
        return result, int(calls.read_text(encoding="utf-8")) if calls.exists() else 0

    @staticmethod
    def _sweep(*swept: str) -> dict[str, Any]:
        return {"outcome": "swept", "swept": list(swept), "kept": [], "refreshRequired": False}

    @staticmethod
    def _state(pins: dict[str, Any], maintenance: bool = True) -> dict[str, Any]:
        return {"maintenance": maintenance, "deployable": False, "images": {"pins": pins}}

    def test_an_empty_set_held_for_the_quiescence_drains(self, tmp_path):
        result, _ = self._run(
            tmp_path, [(0, self._sweep(), 2, self._state({}))], "--quiescence", "0"
        )

        assert result.returncode == 0, result.stderr
        assert "drained" in result.stdout

    def test_a_finished_run_is_swept_and_the_drain_completes(self, tmp_path):
        polls = [
            (0, self._sweep(), 2, self._state({"search": self.PIN})),
            (0, self._sweep("search"), 2, self._state({})),
        ]

        result, calls = self._run(tmp_path, polls, "--quiescence", "0")

        assert result.returncode == 0, result.stderr
        assert calls == 2
        assert "run 39864717423 of Azure/osdu-spi-search" in result.stdout
        assert "swept pins whose owning run finished: search" in result.stdout
        assert "panel output" in result.stdout

    def test_operator_pins_do_not_hold_the_drain(self, tmp_path):
        operator = {"ephemeral": False, "image": "example.azurecr.io/local/search:1"}

        result, _ = self._run(
            tmp_path,
            [(0, self._sweep(), 2, self._state({"search": operator}))],
            "--quiescence",
            "0",
        )

        assert result.returncode == 0, result.stderr

    def test_a_pin_past_the_window_fails_and_names_it(self, tmp_path):
        result, _ = self._run(
            tmp_path,
            [(0, self._sweep(), 2, self._state({"search": self.PIN}))],
            "--window",
            "0",
        )

        assert result.returncode == 1
        assert "maintenance stays set" in result.stderr
        assert "search: run 39864717423" in result.stderr

    def test_maintenance_not_set_fails_at_once(self, tmp_path):
        result, calls = self._run(
            tmp_path, [(0, self._sweep(), 0, self._state({}, maintenance=False))]
        )

        assert result.returncode == 1
        assert calls == 1
        assert "maintenance is not set" in result.stderr

    @pytest.mark.parametrize(
        ("sweep_code", "status_code", "status", "problem"),
        [
            (1, 2, {}, "stale-pin sweep failed (exit 1): boom"),
            (0, 1, {"error": "boom"}, "spi status --json failed (exit 1)"),
            (0, 2, {"maintenance": True}, "no maintenance flag or pin state"),
        ],
        ids=["sweep", "status", "payload"],
    )
    def test_an_unreadable_pin_set_never_counts_as_empty(
        self, tmp_path, sweep_code, status_code, status, problem
    ):
        sweep = {"outcome": "error", "detail": "boom"} if sweep_code else self._sweep()

        result, _ = self._run(
            tmp_path,
            [(sweep_code, sweep, status_code, status)],
            "--window",
            "0",
            "--quiescence",
            "0",
        )

        assert result.returncode == 1
        assert problem in result.stderr

    def test_a_pin_seen_during_the_quiescence_restarts_it(self, tmp_path):
        polls = [
            (0, self._sweep(), 2, self._state({})),
            (0, self._sweep(), 2, self._state({"search": self.PIN})),
        ]

        result, calls = self._run(tmp_path, polls, "--window", "0", "--quiescence", "600")

        assert result.returncode == 1
        assert calls == 2
        assert "quiescence restarts" in result.stdout

    @pytest.mark.skipif(not GNU_TIMEOUT, reason="needs GNU timeout")
    def test_a_sweep_stuck_on_flux_is_stopped_and_the_next_poll_drains(self, tmp_path):
        (tmp_path / "sweep-0.sleep").write_text("30", encoding="utf-8")
        polls = [
            (0, self._sweep("search"), 2, self._state({})),
            (0, self._sweep(), 2, self._state({})),
        ]

        result, calls = self._run(tmp_path, polls, "--call-timeout", "1", "--quiescence", "0")

        assert result.returncode == 0, result.stderr
        assert "stale-pin sweep ran past 00:01 and was stopped" in result.stderr
        assert calls == 1

    @pytest.mark.parametrize(
        "args",
        [["--window", "10m"], ["--quiescence"], ["--bogus"], ["--call-timeout", "0"]],
    )
    def test_bad_arguments_exit_2(self, tmp_path, args):
        result, _ = self._run(tmp_path, [(0, self._sweep(), 2, self._state({}))], *args)

        assert result.returncode == 2
