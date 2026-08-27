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

"""Contract test for digest-first image rendering in the shared service chart.

Guards the ``image.digest`` values contract consumed by every core and
reference service HelmRelease: a resolved digest renders `repository@digest`,
and an empty digest falls back to today's `repository:tag` behavior. Skipped
when Helm is not installed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "software" / "charts" / "osdu-spi-service"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="Helm not installed")


def _rendered_image(extra_set: dict[str, str]) -> str:
    set_args = []
    for key, value in extra_set.items():
        set_args += ["--set", f"{key}={value}"]

    result = subprocess.run(
        ["helm", "template", "chart-image-test", str(CHART_DIR), *set_args],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    for doc in yaml.safe_load_all(result.stdout):
        if doc and doc.get("kind") == "Deployment":
            containers = doc["spec"]["template"]["spec"]["containers"]
            return containers[0]["image"]
    raise AssertionError("no Deployment rendered")


def test_renders_repository_at_digest_when_digest_set():
    image = _rendered_image(
        {
            "image.repository": "community.opengroup.org:5555/osdu/partition-master",
            "image.tag": "abc1234",
            "image.digest": "sha256:" + "a" * 64,
        }
    )
    assert image == f"community.opengroup.org:5555/osdu/partition-master@sha256:{'a' * 64}"


def test_falls_back_to_repository_colon_tag_when_digest_empty():
    image = _rendered_image(
        {
            "image.repository": "community.opengroup.org:5555/osdu/partition-master",
            "image.tag": "abc1234",
        }
    )
    assert image == "community.opengroup.org:5555/osdu/partition-master:abc1234"
