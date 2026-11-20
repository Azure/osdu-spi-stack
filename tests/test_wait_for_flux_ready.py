"""scripts/wait_for_flux_ready.sh --fail-fast against a stub kubectl serving one
Kustomization whose health check names a stalled Job or HelmRelease."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from spi.shell import run_process

REPO_ROOT = Path(__file__).resolve().parent.parent
WAIT_SCRIPT = REPO_ROOT / "scripts" / "wait_for_flux_ready.sh"
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"


def _bash4() -> str | None:
    # The script uses associative arrays; macOS /bin/bash is 3.2.
    for candidate in (shutil.which("bash"), "/opt/homebrew/bin/bash"):
        if not candidate or not Path(candidate).exists():
            continue
        probe = subprocess.run(
            [candidate, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True
        )
        if probe.stdout.strip().isdigit() and int(probe.stdout) >= 4:
            return candidate
    return None


BASH = _bash4()
pytestmark = pytest.mark.skipif(
    BASH is None or shutil.which("jq") is None, reason="needs bash 4+, jq"
)

STUB = """#!/usr/bin/env bash
case "$1 $2" in
  "get kustomizations") cat "{d}/kustomizations.json" ;;
  "get Job"|"get HelmRelease") cat "{d}/object.json" ;;
  "get pods") echo "pod/schema-load-abc12" ;;
  "logs "*) printf 'token %s\\nHTTP 500 from schema service\\n' "{jwt}" ;;
  "describe "*) echo "described $*" ;;
esac
"""


def _cluster(tmp_path: Path, *, kind: str, condition: str, gating: str = "true") -> dict:
    ref = f"{kind}/osdu/schema-load" if kind == "Job" else f"{kind}/osdu-flux/partition"
    kustomization = {
        "metadata": {"name": "spi-osdu-schema-load", "labels": {"spi-stack.gating": gating}},
        "status": {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "message": "health check failed after 33ms: failed early due to "
                    f"stalled resources: [{ref} status: 'Failed']",
                }
            ]
        },
    }
    (tmp_path / "kustomizations.json").write_text(json.dumps({"items": [kustomization]}))
    (tmp_path / "object.json").write_text(
        json.dumps({"status": {"conditions": [{"type": condition, "status": "True"}]}})
    )
    for name, body in (("kubectl", STUB.format(d=tmp_path, jwt=JWT)), ("spi", "#!/bin/sh\n")):
        stub = tmp_path / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    return {"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}


def _wait(env: dict, *args: str) -> subprocess.CompletedProcess:
    assert BASH is not None
    return run_process(
        [BASH, str(WAIT_SCRIPT), "--timeout", "1", "--heartbeat", "1", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(("kind", "condition"), [("Job", "Failed"), ("HelmRelease", "Stalled")])
def test_a_terminal_failure_ends_the_wait_at_once(tmp_path, kind, condition):
    env = _cluster(tmp_path, kind=kind, condition=condition)

    result = _wait(env, "--fail-fast", str(tmp_path / "out"))

    assert result.returncode == 3, result.stderr
    assert "will not recover" in result.stderr


def test_a_failed_job_leaves_its_redacted_pod_log_behind(tmp_path):
    env = _cluster(tmp_path, kind="Job", condition="Failed")
    out = tmp_path / "out"

    result = _wait(env, "--fail-fast", str(out))

    log = (out / "job-osdu-schema-load-schema-load-abc12.log").read_text(encoding="utf-8")
    assert "HTTP 500 from schema service" in log
    assert JWT not in log and JWT not in result.stdout
    assert "HTTP 500 from schema service" in result.stdout


@pytest.mark.parametrize(
    ("condition", "gating", "fail_fast"),
    [
        pytest.param("Complete", "true", True, id="job-not-failed"),
        pytest.param("Failed", "false", True, id="non-gating"),
        pytest.param("Failed", "true", False, id="flag-absent"),
    ],
)
def test_anything_short_of_a_terminal_gating_failure_keeps_waiting(
    tmp_path, condition, gating, fail_fast
):
    env = _cluster(tmp_path, kind="Job", condition=condition, gating=gating)
    args = ("--fail-fast", str(tmp_path / "out")) if fail_fast else ()

    result = _wait(env, *args)

    assert result.returncode == 1, result.stderr
    assert "timed out" in result.stderr
