# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Contract tests for the scheduled Azure smoke workflow.

The smoke is a production reliability signal. These tests protect the two
properties that issue #41 depends on:

* the unattended path begins at the bare profile; and
* bare still validates AKS + Flux, but does not require an ingress substrate it
  deliberately does not deploy.
"""

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "smoke.yml"
SWEEPER_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "sweeper.yml"
CI_SETUP = REPO_ROOT / "docs" / "CI_SETUP.md"


def _workflow(path: Path = SMOKE_WORKFLOW) -> dict[str, Any]:
    # BaseLoader keeps the YAML 1.1 word "on" as a string key.
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _steps(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {step["name"]: step for step in job["steps"] if "name" in step}


def test_bare_is_the_default_and_a_manual_choice():
    workflow = _workflow()
    profile = workflow["on"]["workflow_dispatch"]["inputs"]["profile"]

    assert profile["default"] == "bare"
    assert profile["options"] == ["bare", "core", "minimal"]
    assert workflow["jobs"]["provision"]["env"]["PROFILE"] == "${{ inputs.profile || 'bare' }}"


def test_selected_profile_is_exported_to_verify():
    provision = _workflow()["jobs"]["provision"]

    assert provision["outputs"]["profile"] == "${{ steps.env.outputs.profile }}"
    resolve = _steps(provision)["Resolve env name"]["run"]
    assert 'echo "profile=$PROFILE" >> "$GITHUB_OUTPUT"' in resolve


def test_bare_still_waits_for_flux_but_skips_ingress_probes():
    verify_steps = _steps(_workflow()["jobs"]["verify"])

    assert "if" not in verify_steps["Wait for Flux Kustomizations to be Ready"]
    for name in ("Acceptance probe (gateway reachable)", "Acceptance probe (HTTPS terminates)"):
        assert verify_steps[name]["if"] == "${{ needs.provision.outputs.profile != 'bare' }}"


def test_every_smoke_job_and_the_sweeper_use_the_same_environment():
    jobs = _workflow()["jobs"]
    sweeper = _workflow(SWEEPER_WORKFLOW)["jobs"]["sweep"]

    assert {jobs[name]["environment"] for name in ("provision", "verify", "teardown")} == {
        "azure-smoke"
    }
    assert sweeper["environment"] == "azure-smoke"


def test_verify_gate_outlives_the_schema_load_and_reference_timeouts():
    """The core profile's spi-osdu-schema-load Kustomization can legitimately
    run for its full timeout on a cold cluster, and spi-osdu-reference only
    starts once schema-load is Ready. Both the wait_for_flux_ready poll and
    the verify job's own timeout-minutes must outlast that combined window,
    or a healthy `--profile core` smoke run gets killed and torn down while
    still reconciling.
    """
    core_stack = list(
        yaml.safe_load_all(
            (
                REPO_ROOT / "software" / "stacks" / "osdu" / "profiles" / "core" / "stack.yaml"
            ).read_text(encoding="utf-8")
        )
    )

    def _timeout_minutes(name: str) -> int:
        for doc in core_stack:
            if (
                doc
                and doc.get("kind") == "Kustomization"
                and doc.get("metadata", {}).get("name") == name
            ):
                match = re.fullmatch(r"(\d+)m", doc["spec"]["timeout"])
                assert match, f"{name} timeout is not in minutes"
                return int(match.group(1))
        raise AssertionError(f"Kustomization {name} not found in core stack.yaml")

    combined_minutes = _timeout_minutes("spi-osdu-schema-load") + _timeout_minutes(
        "spi-osdu-reference"
    )

    verify = _workflow()["jobs"]["verify"]
    wait_step = _steps(verify)["Wait for Flux Kustomizations to be Ready"]
    match = re.search(r"--timeout\s+(\d+)", wait_step["run"])
    assert match, "wait_for_flux_ready.sh must be called with an explicit --timeout"
    # wait_for_flux_ready.sh's --timeout is in seconds (scripts/wait_for_flux_ready.sh).
    wait_timeout_minutes = int(match.group(1)) / 60

    assert wait_timeout_minutes > combined_minutes, (
        "wait_for_flux_ready.sh --timeout must outlast spi-osdu-schema-load plus "
        "spi-osdu-reference so a cold core deployment is not killed mid-reconcile"
    )
    assert int(verify["timeout-minutes"]) > wait_timeout_minutes, (
        "verify job timeout-minutes must exceed the wait_for_flux_ready.sh --timeout"
    )

    # The verify job also runs checkout/tool-install/login/get-credentials
    # steps before the wait starts, and the HTTPS acceptance probe can retry
    # for a while after the wait succeeds. A margin that is only "> 0" over
    # the wait timeout can be exhausted by those steps alone, letting the job
    # get cancelled while its probes are still healthy. Require the margin to
    # cover the probe's own worst-case retry budget plus a setup allowance.
    probe_step = _steps(verify)["Acceptance probe (HTTPS terminates)"]["run"]
    attempts_match = re.search(r"seq 1 (\d+)", probe_step)
    max_time_match = re.search(r"--max-time (\d+)", probe_step)
    sleep_match = re.search(r"sleep (\d+)", probe_step)
    assert attempts_match and max_time_match and sleep_match, (
        "could not parse the HTTPS acceptance probe's retry parameters"
    )
    probe_minutes = (
        int(attempts_match.group(1)) * (int(max_time_match.group(1)) + int(sleep_match.group(1)))
    ) / 60

    setup_buffer_minutes = 15  # checkout, tool installs, az login, get-credentials
    required_margin_minutes = probe_minutes + setup_buffer_minutes

    margin_minutes = int(verify["timeout-minutes"]) - wait_timeout_minutes
    assert margin_minutes >= required_margin_minutes, (
        "verify job timeout-minutes must reserve enough margin over the "
        "wait_for_flux_ready.sh --timeout to cover the HTTPS probe's own retry "
        "budget plus setup headroom, so a healthy core run isn't cancelled while "
        "its probes are still running"
    )


def test_smoke_environment_is_reviewer_free_and_protected_branch_only():
    setup = CI_SETUP.read_text(encoding="utf-8")
    environment_setup = setup.split("# 6.", maxsplit=1)[1].split("```", maxsplit=1)[0]

    assert '"reviewers": []' in environment_setup
    assert '"protected_branches": true' in environment_setup
    assert '"custom_branch_policies": false' in environment_setup
    assert '"deployment_branch_policy": null' not in environment_setup
