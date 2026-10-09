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
from pathlib import Path

import pytest
import yaml
from _quantities import _millicores

from spi.shell import run_process

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "software" / "charts" / "osdu-spi-service"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="Helm not installed")


def _rendered_deployment(
    extra_set: dict[str, str], extra_set_string: dict[str, str] | None = None
) -> dict:
    set_args = []
    for key, value in extra_set.items():
        set_args += ["--set", f"{key}={value}"]
    for key, value in (extra_set_string or {}).items():
        set_args += ["--set-string", f"{key}={value}"]

    result = run_process(
        ["helm", "template", "chart-image-test", str(CHART_DIR), *set_args],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    for doc in yaml.safe_load_all(result.stdout):
        if doc and doc.get("kind") == "Deployment":
            return doc
    raise AssertionError("no Deployment rendered")


def _rendered_image(extra_set: dict[str, str]) -> str:
    deployment = _rendered_deployment(extra_set)
    return deployment["spec"]["template"]["spec"]["containers"][0]["image"]


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


def test_requests_the_cpu_admission_will_grant_every_container():
    deployment = _rendered_deployment({"redisTls": "true"})
    pod = deployment["spec"]["template"]["spec"]
    containers = pod["containers"] + pod["initContainers"]

    for container in containers:
        requested = container["resources"]["requests"]["cpu"]
        assert _millicores(requested) >= 100, container["name"]


def test_run_as_user_defaults_to_1000():
    deployment = _rendered_deployment({})
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert container["securityContext"]["runAsUser"] == 1000


def test_run_as_user_override_preserves_safeguards_for_every_container():
    deployment = _rendered_deployment({"redisTls": "true", "runAsUser": "999"})
    pod = deployment["spec"]["template"]["spec"]
    containers = pod["containers"] + pod["initContainers"]

    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    for container in containers:
        assert container["securityContext"] == {
            "allowPrivilegeEscalation": False,
            "runAsNonRoot": True,
            "runAsUser": 999,
            "capabilities": {"drop": ["ALL"]},
        }


def test_environment_value_from_is_preserved():
    deployment = _rendered_deployment(
        {
            "env[0].name": "SDMS_KEYVAULT_URL",
            "env[0].valueFrom.configMapKeyRef.name": "osdu-config",
            "env[0].valueFrom.configMapKeyRef.key": "KEYVAULT_URL",
        }
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert container["env"] == [
        {
            "name": "SDMS_KEYVAULT_URL",
            "valueFrom": {
                "configMapKeyRef": {
                    "name": "osdu-config",
                    "key": "KEYVAULT_URL",
                }
            },
        }
    ]


def test_environment_value_is_always_rendered_as_a_string():
    deployment = _rendered_deployment(
        {
            "env[0].name": "FEATURE_FLAG_ENABLE_RESTORE",
            "env[0].value": "true",
        }
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert container["env"] == [
        {
            "name": "FEATURE_FLAG_ENABLE_RESTORE",
            "value": "true",
        }
    ]


_ISTIO_PROXY = yaml.safe_load((CHART_DIR / "values.yaml").read_text())["istioProxyPin"]["image"]


# Flux substitutes the lock key into a quoted value, so the chart sees a string.
@pytest.mark.parametrize(
    ("value", "istio", "expected"),
    [
        ("true", "false", {"karpenter.sh/do-not-disrupt": "true"}),
        ("false", "false", {}),
        (None, "false", {}),
        ("false", "true", {"sidecar.istio.io/proxyImage": _ISTIO_PROXY}),
        (
            "true",
            "true",
            {"sidecar.istio.io/proxyImage": _ISTIO_PROXY, "karpenter.sh/do-not-disrupt": "true"},
        ),
    ],
)
def test_do_not_disrupt_annotation_renders_only_for_the_string_true(value, istio, expected):
    strings = {"karpenter.doNotDisrupt": value} if value is not None else {}
    deployment = _rendered_deployment({"istioProxyPin.enabled": istio}, strings)
    annotations = deployment["spec"]["template"]["metadata"].get("annotations") or {}
    assert annotations == expected
