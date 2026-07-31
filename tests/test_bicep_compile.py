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

"""Smoke tests that every Bicep template in infra/ compiles cleanly.

Guards against schema drift that would only surface at deploy time. Skipped
when the Azure CLI is not installed (e.g., contributor laptops without az).
"""

import re
import shutil
from pathlib import Path

import pytest

from spi.config import Profile
from spi.shell import run_process

REPO_ROOT = Path(__file__).resolve().parent.parent
INFRA_DIR = REPO_ROOT / "infra"
AZ = shutil.which("az") or ""


def _bicep_files():
    return sorted(INFRA_DIR.rglob("*.bicep"))


@pytest.mark.skipif(not AZ, reason="Azure CLI not installed")
@pytest.mark.parametrize(
    "bicep_file",
    _bicep_files(),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_bicep_compiles(bicep_file: Path):
    result = run_process(
        ["az", "bicep", "build", "--file", str(bicep_file), "--stdout"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"bicep build failed for {bicep_file.relative_to(REPO_ROOT)}:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


@pytest.mark.skipif(not AZ, reason="Azure CLI not installed")
def test_bicepparam_files_compile():
    """Each .bicepparam file must resolve against its referenced template."""
    param_files = sorted((INFRA_DIR / "params").glob("*.bicepparam"))
    assert param_files, "expected at least one .bicepparam in infra/params/"
    for pf in param_files:
        result = run_process(
            ["az", "bicep", "build-params", "--file", str(pf), "--stdout"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"bicep build-params failed for {pf.relative_to(REPO_ROOT)}:\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def test_flux_bicep_allows_every_profile():
    source = (INFRA_DIR / "flux.bicep").read_text()
    match = re.search(
        r"@allowed\(\[(?P<values>.*?)\]\)\s*param profile string",
        source,
        re.DOTALL,
    )
    assert match is not None, "profile @allowed block not found in infra/flux.bicep"

    allowed_block = match.group("values")
    for profile in Profile:
        assert f"'{profile.value}'" in allowed_block
