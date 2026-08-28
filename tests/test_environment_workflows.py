# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Contract tests for the env-upgrade and env-refresh lifecycle workflows.

These pin the properties docs/design/environment-lifecycle.md promises for
the shared backing environment:

* both workflows share one serializing concurrency group;
* a lifecycle job never calls `uv run spi`, only the bare `spi` installed
  from the declared release wheel;
* an absent declaration, or (for env-upgrade) a declaration created on this
  very push, is a clean skip rather than an automatic provision;
* maintenance is set before any mutation and cleared only after readiness,
  gateway probes, and a ref/suspension assertion have all passed; and
* every long-lived job captures diagnostics on failure.
"""

import json
import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_UPGRADE = REPO_ROOT / ".github" / "workflows" / "env-upgrade.yml"
ENV_REFRESH = REPO_ROOT / ".github" / "workflows" / "env-refresh.yml"
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

    def test_env_refresh_triggers_weekdays_at_0500_utc_and_manual_dispatch(self):
        workflow = _workflow(ENV_REFRESH)

        assert "workflow_dispatch" in workflow["on"]
        schedules = workflow["on"]["schedule"]
        assert any(entry["cron"] == "0 5 * * 1-5" for entry in schedules)


class TestConcurrencyAndEnvironment:
    def test_both_workflows_share_one_serializing_concurrency_group(self):
        for path in (ENV_UPGRADE, ENV_REFRESH):
            workflow = _workflow(path)
            assert workflow["concurrency"]["group"] == "env-shared"
            assert workflow["concurrency"]["cancel-in-progress"] == "false"

    def test_azure_touching_jobs_use_the_azure_shared_environment(self):
        for path in (ENV_UPGRADE, ENV_REFRESH):
            workflow = _workflow(path)
            for name, job in workflow["jobs"].items():
                if name == "declare" or name == "verify-release":
                    continue
                assert job.get("environment") == "azure-shared", (
                    f"{path.name}:{name} must run under the azure-shared environment"
                )

    def test_permissions_are_minimal_and_include_id_token(self):
        for path in (ENV_UPGRADE, ENV_REFRESH):
            workflow = _workflow(path)
            assert workflow["permissions"] == {"contents": "read", "id-token": "write"}


class TestExactWheelDiscipline:
    def test_no_lifecycle_step_invokes_uv_run_spi(self):
        for path in (ENV_UPGRADE, ENV_REFRESH):
            run_text = _all_run_text(_workflow(path))
            assert "uv run spi" not in run_text, (
                f"{path.name} must never invoke the checkout's source CLI via uv run spi"
            )

    def test_every_azure_job_installs_the_exact_release_wheel(self):
        for path in (ENV_UPGRADE, ENV_REFRESH):
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
        for path in (ENV_UPGRADE, ENV_REFRESH):
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
        for path in (ENV_UPGRADE, ENV_REFRESH):
            detect = _steps(_workflow(path)["jobs"]["detect"])[
                "Detect existing resource group and AKS cluster"
            ]["run"]
            assert "if ! RG_EXISTS=$(az group exists" in detect
            assert "if ! AKS_COUNT=$(az aks list" in detect
            assert "exit 1" in detect


class TestEnvRefreshDetectFailsClosed:
    """declare already gates should_run on the declaration's presence, so by
    the time detect runs, the declaration is known present. An absent
    resource group or missing cluster there means the live environment was
    deleted, not a legitimate first-provision skip.

    env-upgrade's own detect job is intentionally left as a clean skip: see
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
        assert "if" not in clear_step, (
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
        # Plain reconcile: no image refresh on this schedule.
        assert reconcile_step["run"].strip() == 'spi reconcile --image-branch "$IMAGE_BRANCH"'

        clear_step = next(s for s in refresh_steps if s.get("name") == "Clear maintenance")
        assert "if" not in clear_step, (
            "maintenance must never be cleared from an if: always() failure path"
        )

    def test_env_refresh_requires_deploy_record_before_setting_maintenance(self):
        # A prior provision can die after creating AKS but before writing
        # the deploy record; unlike env-upgrade, refresh has no provision
        # job to recover with, so it must fail closed rather than let
        # `spi maintenance set` surface the record's own error uninterpreted.
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


class TestAssertionsAndDeployability:
    def test_env_upgrade_verify_asserts_ref_resolved_commit_and_suspended(self):
        verify_steps = _steps(_workflow(ENV_UPGRADE)["jobs"]["verify"])
        assertion = verify_steps["Verify deployed stack matches the declaration"]["run"]
        assert ".stack.ref" in assertion
        assert ".stack.resolvedCommit" in assertion
        assert ".suspended" in assertion
        assert "STACK_VERSION" in assertion

    def test_env_refresh_verify_asserts_ref_and_suspended(self):
        refresh_steps = _steps(_workflow(ENV_REFRESH)["jobs"]["refresh"])
        assertion = refresh_steps["Verify stack version and source suspension are unchanged"]["run"]
        assert ".stack.ref" in assertion
        assert ".suspended" in assertion

    def test_env_upgrade_verify_requires_maintenance_as_the_sole_blocker(self):
        # Exit 2 from `spi status --json` covers every deployability blocker
        # (a non-ready Kustomization, a missing deploy record, or
        # maintenance itself; ADR-030), so the ref/suspended assertions
        # alone cannot tell a readiness regression from a clean maintenance
        # window. Assert the JSON fields directly instead.
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

    def test_both_workflows_require_deployable_status_as_the_final_gate(self):
        for path, job_name in ((ENV_UPGRADE, "verify"), (ENV_REFRESH, "refresh")):
            steps = _steps(_workflow(path)["jobs"][job_name])
            deployable_step = steps["Require deployable status"]
            assert deployable_step["run"].strip() == "spi status --json"

    def test_bare_profile_skips_gateway_probes(self):
        for path, job_name in ((ENV_UPGRADE, "verify"), (ENV_REFRESH, "refresh")):
            steps = _steps(_workflow(path)["jobs"][job_name])
            assert steps["Acceptance probe (gateway reachable)"]["if"] == (
                "needs.declare.outputs.profile != 'bare'"
            )
            assert steps["Acceptance probe (HTTPS terminates)"]["if"] == (
                "needs.declare.outputs.profile != 'bare'"
            )


class TestDiagnostics:
    def test_every_azure_job_captures_diagnostics_on_failure(self):
        for path in (ENV_UPGRADE, ENV_REFRESH):
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


class TestRevisionGatedConvergence:
    """An upgrade re-points the source while every Kustomization is still
    Ready for the revision being replaced, so a bare Ready wait can pass
    before any of the new revision is applied (ADR-029)."""

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
        assert ".stack.resolvedCommit" in resolve["run"]
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

        pr_step = steps["Open the stackVersion bump PR"]
        assert pr_step["if"] == "steps.gate.outputs.proceed == 'true'"

    def test_bump_pr_touches_only_the_declaration_file(self):
        steps = _steps(_workflow(RELEASE_WORKFLOW)["jobs"]["bump-environment"])
        pr_step_run = steps["Open the stackVersion bump PR"]["run"]

        assert "git diff --cached --name-only" in pr_step_run
        assert 'CHANGED" != "$DECL"' in pr_step_run
        assert "stackVersion:" in pr_step_run
        assert "git add" in pr_step_run

    def test_bump_commit_and_pr_title_follow_conventional_commits(self):
        steps = _steps(_workflow(RELEASE_WORKFLOW)["jobs"]["bump-environment"])
        pr_step_run = steps["Open the stackVersion bump PR"]["run"]

        assert re.search(r"git commit -m \"chore\(env\): bump shared stackVersion", pr_step_run)
        assert re.search(r"--title \"chore\(env\): bump shared stackVersion", pr_step_run)


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
