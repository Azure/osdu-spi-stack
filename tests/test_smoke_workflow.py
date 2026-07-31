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


def test_smoke_environment_is_reviewer_free_and_protected_branch_only():
    setup = CI_SETUP.read_text(encoding="utf-8")
    environment_setup = setup.split("# 6.", maxsplit=1)[1].split("```", maxsplit=1)[0]

    assert '"reviewers": []' in environment_setup
    assert '"protected_branches": true' in environment_setup
    assert '"custom_branch_policies": false' in environment_setup
    assert '"deployment_branch_policy": null' not in environment_setup
