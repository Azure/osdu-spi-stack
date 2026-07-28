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

"""Structural tests for the Flux stack profiles and ingress trees.

Every Profile/IngressMode the CLI accepts must resolve to a real path under
software/stacks/osdu/, and every dependsOn in the resulting pair of
Kustomization trees must name a Kustomization that pair actually declares.
Without these, an unbacked enum value deploys ~45 minutes of Azure infra and
then stalls Flux on DependencyNotReady instead of failing up front.
"""

import itertools
from pathlib import Path

import pytest
import yaml

from spi.config import IngressMode, Profile

REPO_ROOT = Path(__file__).resolve().parent.parent
STACKS = REPO_ROOT / "software" / "stacks" / "osdu"
PROFILES_DIR = STACKS / "profiles"
INGRESS_DIR = STACKS / "ingress"

KUSTOMIZATION_KIND = "Kustomization"
FLUX_API_PREFIX = "kustomize.toolkit.fluxcd.io/"


def _flux_kustomizations(tree: Path):
    """Yield every Flux Kustomization doc declared under a stack directory."""
    for path in sorted(tree.glob("*.yaml")):
        if path.name == "kustomization.yaml":
            continue
        for doc in yaml.safe_load_all(path.read_text()):
            if not doc or doc.get("kind") != KUSTOMIZATION_KIND:
                continue
            if not str(doc.get("apiVersion", "")).startswith(FLUX_API_PREFIX):
                continue
            yield doc


def _declared_names(tree: Path) -> set:
    return {k["metadata"]["name"] for k in _flux_kustomizations(tree)}


def _dependency_names(tree: Path) -> set:
    return {
        dep["name"] for k in _flux_kustomizations(tree) for dep in k["spec"].get("dependsOn", [])
    }


def _referenced_paths(tree: Path) -> set:
    return {k["spec"]["path"] for k in _flux_kustomizations(tree)}


def _ingress_tree(profile: Profile, mode: IngressMode) -> Path:
    if profile is Profile.BARE:
        return INGRESS_DIR / "bare"
    suffix = "-minimal" if profile is Profile.MINIMAL else ""
    return INGRESS_DIR / f"{mode.value}{suffix}"


PAIRS = list(itertools.product(Profile, IngressMode))
PAIR_IDS = [f"{p.value}+{m.value}" for p, m in PAIRS]


class TestTreesExist:
    @pytest.mark.parametrize("profile", list(Profile), ids=lambda p: p.value)
    def test_profile_directory_is_backed(self, profile):
        tree = PROFILES_DIR / profile.value
        assert (tree / "kustomization.yaml").is_file(), (
            f"Profile.{profile.name} has no kustomization.yaml at {tree}"
        )

    @pytest.mark.parametrize("pair", PAIRS, ids=PAIR_IDS)
    def test_ingress_directory_is_backed(self, pair):
        profile, mode = pair
        tree = _ingress_tree(profile, mode)
        assert (tree / "kustomization.yaml").is_file(), (
            f"--profile {profile.value} --ingress-mode {mode.value} resolves to "
            f"{tree}, which has no kustomization.yaml"
        )


class TestDependenciesResolve:
    @pytest.mark.parametrize("pair", PAIRS, ids=PAIR_IDS)
    def test_no_dangling_depends_on(self, pair):
        profile, mode = pair
        profile_tree = PROFILES_DIR / profile.value
        ingress_tree = _ingress_tree(profile, mode)

        declared = _declared_names(profile_tree) | _declared_names(ingress_tree)
        required = _dependency_names(profile_tree) | _dependency_names(ingress_tree)

        assert required <= declared, (
            f"--profile {profile.value} --ingress-mode {mode.value} has dependsOn "
            f"entries no Kustomization in the pair declares: {sorted(required - declared)}. "
            "Flux would stall these on DependencyNotReady indefinitely."
        )

    @pytest.mark.parametrize("pair", PAIRS, ids=PAIR_IDS)
    def test_referenced_paths_exist(self, pair):
        profile, mode = pair
        for tree in (PROFILES_DIR / profile.value, _ingress_tree(profile, mode)):
            for rel in sorted(_referenced_paths(tree)):
                target = REPO_ROOT / rel.removeprefix("./")
                assert target.is_dir(), f"{tree.name}/stack.yaml points at missing {rel}"


class TestMinimalProfileScope:
    """The minimal profile is middleware-only: nothing at layer 5 or above."""

    def test_declares_no_osdu_services(self):
        for k in _flux_kustomizations(PROFILES_DIR / Profile.MINIMAL.value):
            layer = k["metadata"]["labels"]["spi-stack.layer"]
            assert int(layer) < 5, (
                f"minimal profile declares {k['metadata']['name']} at layer {layer}; "
                "the profile is defined to stop below layer 5"
            )

    @pytest.mark.parametrize("mode", list(IngressMode), ids=lambda m: m.value)
    def test_ingress_declares_no_osdu_routes(self, mode):
        names = _declared_names(_ingress_tree(Profile.MINIMAL, mode))
        assert "spi-osdu-routes" not in names


class TestBareProfileScope:
    def test_stack_tree_is_empty(self):
        assert list(_flux_kustomizations(PROFILES_DIR / Profile.BARE.value)) == []

    def test_ingress_tree_is_empty(self):
        assert list(_flux_kustomizations(INGRESS_DIR / "bare")) == []
