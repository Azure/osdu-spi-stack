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

"""API server tests for the osdu-spi-init chart's Jobs.

A render cannot prove the API server accepts a Job: podFailurePolicy rules are
checked against the pod template server-side, and a live Job's spec is
immutable, so an upgrade that patches one fails only against a real server.
These run against the disposable cluster SPI_CHART_TEST_CONTEXT names (CI
creates a kind cluster) and are skipped without one. The upgrade test first
installs the chart at the git revision SPI_PREVIOUS_CHART_REF names.
"""

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest
import yaml

from spi.shell import run_process

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_PATH = "software/charts/osdu-spi-init"
CONTEXT = os.environ.get("SPI_CHART_TEST_CONTEXT", "")
PREVIOUS_REF = os.environ.get("SPI_PREVIOUS_CHART_REF", "")

pytestmark = pytest.mark.skipif(
    not CONTEXT or not all(shutil.which(tool) for tool in ("helm", "kubectl", "git")),
    reason="SPI_CHART_TEST_CONTEXT, helm, kubectl, or git not available",
)

# Every component renders: all toggles default on, and members need a member.
_SETS = [
    "--set",
    "partitions[0]=opendes",
    "--set",
    "partitions[1]=second",
    "--set",
    "entitlementsMembers[0]=deployer-client-id",
    "--set",
    "entitlementsMemberUsers[0]=member-client-id",
]


def _run(cmd: list[str], stdin: str | None = None):
    return run_process(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _kubectl(*args: str, stdin: str | None = None):
    return _run(["kubectl", "--context", CONTEXT, *args], stdin)


def _helm(*args: str):
    return _run(["helm", "--kube-context", CONTEXT, *args])


@pytest.fixture
def namespace():
    """A namespace of its own, so runs never see each other's Jobs."""
    name = f"spi-init-{uuid.uuid4().hex[:8]}"
    created = _kubectl("create", "namespace", name)
    assert created.returncode == 0, created.stderr
    yield name
    _kubectl("delete", "namespace", name, "--wait=false")


def _render(chart: Path, namespace: str) -> list[dict]:
    result = _run(
        [
            "helm",
            "template",
            "osdu-spi-init",
            str(chart),
            "--set",
            f"namespace={namespace}",
            *_SETS,
        ]
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc and doc["kind"] == "Job"]


def _dry_run(jobs: list[dict]):
    manifest = yaml.safe_dump_all(jobs)
    return _kubectl("apply", "--dry-run=server", "-f", "-", stdin=manifest)


def test_rendered_jobs_pass_server_side_validation(namespace):
    """Every Job the chart renders, podFailurePolicy included, is accepted by
    the API server as rendered."""
    jobs = _render(REPO_ROOT / CHART_PATH, namespace)
    assert len(jobs) == 8
    result = _dry_run(jobs)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("created (server dry run)") == 8, result.stdout


def test_a_rule_naming_the_injected_container_is_rejected(namespace):
    """The reason the rule names no container: istio-validation is injected at
    admission, so it is absent from the template the server checks against."""
    job = _render(REPO_ROOT / CHART_PATH, namespace)[0]
    job["spec"]["podFailurePolicy"]["rules"][0]["onExitCodes"]["containerName"] = "istio-validation"
    result = _dry_run([job])
    assert result.returncode != 0
    assert "containerName" in result.stderr


def _export_chart(ref: str, dest: Path) -> Path:
    """The chart as it is at a git revision, written under dest."""
    listed = _run(["git", "-C", str(REPO_ROOT), "ls-tree", "-r", "--name-only", ref, CHART_PATH])
    assert listed.returncode == 0, listed.stderr
    for path in listed.stdout.split():
        shown = _run(["git", "-C", str(REPO_ROOT), "show", f"{ref}:{path}"])
        assert shown.returncode == 0, shown.stderr
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(shown.stdout, encoding="utf-8")
    return dest / CHART_PATH


def _live_jobs(namespace: str) -> dict[str, dict]:
    result = _kubectl("get", "jobs", "-n", namespace, "-o", "json")
    assert result.returncode == 0, result.stderr
    return {job["metadata"]["name"]: job for job in json.loads(result.stdout)["items"]}


@pytest.mark.skipif(not PREVIOUS_REF, reason="SPI_PREVIOUS_CHART_REF not set")
def test_upgrade_from_the_previous_chart_recreates_changed_jobs(namespace, tmp_path):
    """Helm patches a Job that keeps its name, and the API server rejects a
    patch to a Job's spec, so upgrading a running environment must succeed and
    leave only Jobs that match the new chart. A Job that lacked the
    podFailurePolicy cannot gain it in place, so it must have been replaced."""
    previous = _export_chart(PREVIOUS_REF, tmp_path)
    args = ["-n", namespace, "--set", f"namespace={namespace}", *_SETS]

    installed = _helm("install", "osdu-spi-init", str(previous), *args)
    assert installed.returncode == 0, installed.stderr
    before = _live_jobs(namespace)
    assert before

    upgraded = _helm("upgrade", "osdu-spi-init", str(REPO_ROOT / CHART_PATH), *args)
    assert upgraded.returncode == 0, upgraded.stderr
    after = _live_jobs(namespace)

    rendered = _render(REPO_ROOT / CHART_PATH, namespace)
    assert sorted(after) == sorted(job["metadata"]["name"] for job in rendered)
    for job in after.values():
        assert job["spec"]["podFailurePolicy"]["rules"][0]["action"] == "Ignore"
    live_uids = {job["metadata"]["uid"] for job in after.values()}
    for name, job in before.items():
        if "podFailurePolicy" not in job["spec"]:
            assert job["metadata"]["uid"] not in live_uids, name
