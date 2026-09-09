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

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SOFTWARE = REPO_ROOT / "software"


def test_local_chart_releases_reconcile_per_revision():
    invalid_releases = []

    for path in sorted(SOFTWARE.rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        if "kind: HelmRelease" not in text:
            continue

        for document in yaml.safe_load_all(text):
            if not document or document.get("kind") != "HelmRelease":
                continue

            chart_spec = ((document.get("spec") or {}).get("chart") or {}).get("spec") or {}
            if not str(chart_spec.get("chart", "")).startswith("./"):
                continue
            if chart_spec.get("reconcileStrategy") != "Revision":
                invalid_releases.append(str(path.relative_to(REPO_ROOT)))

    assert not invalid_releases, (
        "Local-path HelmReleases must set spec.chart.spec.reconcileStrategy to Revision:\n"
        + "\n".join(invalid_releases)
    )
