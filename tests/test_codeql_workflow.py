# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Contract tests for the CodeQL workflow."""

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CODEQL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "codeql.yml"


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(CODEQL_WORKFLOW.read_text(encoding="utf-8"))


def test_codeql_analyzes_actions_and_python_independently():
    job = _workflow()["jobs"]["analyze"]
    steps = {step["name"]: step for step in job["steps"] if "name" in step}

    assert job["strategy"] == {
        "fail-fast": False,
        "matrix": {"language": ["actions", "python"]},
    }
    assert steps["Initialize CodeQL"]["with"]["languages"] == "${{ matrix.language }}"
    assert (
        steps["Perform CodeQL Analysis"]["with"]["category"] == "/language:${{ matrix.language }}"
    )
