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
import json
import re
from pathlib import Path

import pytest
import yaml

from spi.bootstrap import ISTIO_REVISION_CONFIGMAP
from spi.config import IngressMode, Profile
from spi.ingress import (
    AZURE_DNS_LABEL_ANNOTATION,
    GATEWAY_NAME,
    GATEWAY_NAMESPACE,
    GATEWAY_SERVICE,
)
from spi.stack_version import STACK_VERSION_CONFIGMAP, STACK_VERSION_NAMESPACE

REPO_ROOT = Path(__file__).resolve().parent.parent
STACKS = REPO_ROOT / "software" / "stacks" / "osdu"
PROFILES_DIR = STACKS / "profiles"
INGRESS_DIR = STACKS / "ingress"

KUSTOMIZATION_KIND = "Kustomization"
FLUX_API_PREFIX = "kustomize.toolkit.fluxcd.io/"
STACK_VERSION_MANIFEST = (
    REPO_ROOT / "software" / "components" / "stack-version" / "stack-version.yaml"
)
NAMESPACES_MANIFEST = REPO_ROOT / "software" / "components" / "namespaces" / "namespaces.yaml"

SUBSTITUTE_ANNOTATION = "kustomize.toolkit.fluxcd.io/substitute"
PAYLOAD_FIELDS = ("data", "stringData")
PAYLOAD_KINDS = ("ConfigMap", "Secret")
# Flux envsubst covers POSIX parameter expansion, not just ${VAR}: ${VAR:=x},
# ${VAR:-x}, ${#VAR} and ${VAR/a/b} are all rewritten.
DOLLAR_RUN = re.compile(r"(?<!\$)(\$+)\{[^}]*\}")


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


def _substituted_paths() -> set:
    """Yield every path whose Kustomization runs Flux postBuild substitution.

    Either postBuild form substitutes: substituteFrom reads ConfigMaps and
    Secrets, substitute takes an inline map.
    """
    trees = [
        t for t in itertools.chain(PROFILES_DIR.iterdir(), INGRESS_DIR.iterdir()) if t.is_dir()
    ]
    return {
        item["spec"]["path"]
        for tree in trees
        for item in _flux_kustomizations(tree)
        if item["spec"].get("postBuild")
    }


def _opts_out_of_substitution(doc) -> bool:
    meta = doc.get("metadata") or {}
    annotations = meta.get("annotations") or {}
    labels = meta.get("labels") or {}
    return "disabled" in (
        annotations.get(SUBSTITUTE_ANNOTATION),
        labels.get(SUBSTITUTE_ANNOTATION),
    )


def _built_resources(directory: Path, seen: set | None = None):
    """Yield (path, doc) for every manifest a kustomization builds.

    Follows resource directories outside the kustomization's own path, as the
    gateway TLS overlays do via ../../components/gateway. Flux substitutes
    whatever kustomize renders, so anything short of the full graph leaves a
    resource that gets rewritten but never inspected.
    """
    seen = set() if seen is None else seen
    directory = directory.resolve()
    if directory in seen:
        return
    seen.add(directory)

    kustomization = directory / "kustomization.yaml"
    if not kustomization.is_file():
        return

    spec = yaml.safe_load(kustomization.read_text(encoding="utf-8")) or {}
    for entry in spec.get("resources", []):
        target = (directory / entry).resolve()
        if target.is_dir():
            yield from _built_resources(target, seen)
        elif target.is_file():
            for doc in yaml.safe_load_all(target.read_text(encoding="utf-8")):
                if doc:
                    yield target, doc


def _rewritten_expansions(text: str) -> list:
    """Return the expansions Flux would rewrite in a payload.

    Flux collapses each $$ into one literal dollar, so an even-length dollar
    run escapes the expansion outright while an odd-length run leaves a single
    dollar still driving a substitution: $${VAR} survives, $$${VAR} does not.
    """
    return [match.group(0) for match in DOLLAR_RUN.finditer(text) if len(match.group(1)) % 2]


def _payload_strings(doc):
    """Yield the ConfigMap/Secret payload values, where embedded scripts live.

    Manifest fields are excluded on purpose: substitution there is the point,
    as with the schema-load Job's image and the overlays' patched hostnames.
    A payload is application content Flux has no business rewriting.
    """
    if doc.get("kind") not in PAYLOAD_KINDS:
        return
    for field in PAYLOAD_FIELDS:
        for value in (doc.get(field) or {}).values():
            if isinstance(value, str):
                yield value


def _kustomization(tree: Path, name: str):
    for item in _flux_kustomizations(tree):
        if item.get("metadata", {}).get("name") == name:
            return item
    raise AssertionError(f"{tree}/stack.yaml has no Kustomization named {name}")


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


class TestIstioRevisionSubstitution:
    @pytest.mark.parametrize("profile", [Profile.CORE, Profile.MINIMAL], ids=lambda p: p.value)
    def test_spi_namespaces_substitutes_revision_from_configmap(self, profile):
        item = _kustomization(PROFILES_DIR / profile.value, "spi-namespaces")
        substitute_from = item["spec"]["postBuild"]["substituteFrom"]
        assert any(
            source.get("kind") == "ConfigMap"
            and source.get("name") == ISTIO_REVISION_CONFIGMAP
            and source.get("optional") is False
            for source in substitute_from
        )

    def test_namespaces_manifest_uses_istio_revision_placeholder(self):
        text = NAMESPACES_MANIFEST.read_text(encoding="utf-8")
        assert "istio.io/rev: ${ISTIO_REVISION}" in text

    def test_no_hardcoded_asm_revision_labels_under_software(self):
        hardcoded = []
        pattern = re.compile(r"""istio\.io/rev:\s*['"]?asm-1-""")
        for path in sorted((REPO_ROOT / "software").rglob("*.yaml")):
            if pattern.search(path.read_text(encoding="utf-8")):
                hardcoded.append(path.relative_to(REPO_ROOT).as_posix())
        assert not hardcoded, f"hardcoded istio revisions found: {hardcoded}"


class TestSchemaLoadImageSubstitution:
    def test_schema_load_substitutes_image_lock_and_replaces_immutable_job(self):
        item = _kustomization(PROFILES_DIR / Profile.CORE.value, "spi-osdu-schema-load")
        substitute_from = item["spec"]["postBuild"]["substituteFrom"]

        assert any(
            source.get("kind") == "ConfigMap"
            and source.get("name") == "osdu-image-lock"
            and source.get("optional") is not True
            for source in substitute_from
        )
        assert item["spec"]["force"] is True

    def test_schema_load_job_uses_image_lock_variables(self):
        job = yaml.safe_load((STACKS / "schema-load" / "job.yaml").read_text(encoding="utf-8"))
        image = job["spec"]["template"]["spec"]["containers"][0]["image"]

        assert image == "${SCHEMA_LOAD_IMAGE_REF}"

    def test_schema_load_script_keeps_its_shell_references(self):
        doc = yaml.safe_load((STACKS / "schema-load" / "script.yaml").read_text(encoding="utf-8"))
        script = doc["data"]["bootstrap.sh"]

        assert _opts_out_of_substitution(doc)
        assert "${SCHEMA_INFO_URL}" in script
        assert "${DATA_PARTITION}" in script


class TestSchemaLoadDeadline:
    def test_flux_timeout_exceeds_job_deadline(self):
        item = _kustomization(PROFILES_DIR / Profile.CORE.value, "spi-osdu-schema-load")
        timeout = item["spec"]["timeout"]
        match = re.fullmatch(r"(\d+)m", timeout)
        assert match, f"schema-load Kustomization timeout is not in minutes: {timeout}"

        jobs = [
            doc
            for _, doc in _built_resources(STACKS / "schema-load")
            if doc.get("kind") == "Job" and doc.get("metadata", {}).get("name") == "schema-load"
        ]
        assert len(jobs) == 1
        deadline = jobs[0]["spec"]["activeDeadlineSeconds"]

        assert int(match.group(1)) * 60 > deadline, (
            "spi-osdu-schema-load timeout must exceed the Job deadline for image pull "
            "and reconcile overhead"
        )

    # activeDeadlineSeconds counts from Job activation, so it includes node
    # provisioning, scheduling, and image pull; WAIT_DEADLINE_SECONDS counts
    # from inside bootstrap.sh once the Pod runs. Must match the 1800s
    # pod-startup allowance in software/stacks/osdu/schema-load/job.yaml.
    POD_STARTUP_ALLOWANCE_SECONDS = 1800

    def test_job_deadline_leaves_load_headroom_beyond_the_service_wait(self):
        jobs = [
            doc
            for _, doc in _built_resources(STACKS / "schema-load")
            if doc.get("kind") == "Job" and doc.get("metadata", {}).get("name") == "schema-load"
        ]
        assert len(jobs) == 1
        deadline = jobs[0]["spec"]["activeDeadlineSeconds"]

        doc = yaml.safe_load((STACKS / "schema-load" / "script.yaml").read_text(encoding="utf-8"))
        match = re.search(r"^\s*WAIT_DEADLINE_SECONDS=(\d+)$", doc["data"]["bootstrap.sh"], re.M)
        assert match, "bootstrap.sh must set a literal WAIT_DEADLINE_SECONDS"
        wait_deadline = int(match.group(1))

        # After the pod-startup allowance and the service wait, at least an
        # hour of the Job deadline must remain for the throttled schema load.
        headroom = deadline - self.POD_STARTUP_ALLOWANCE_SECONDS - wait_deadline
        assert headroom >= 3600, (
            "WAIT_DEADLINE_SECONDS plus the pod-startup allowance must leave at "
            "least 3600s of the Job deadline for the schema load itself"
        )


class TestSubstitutionLeavesScriptsIntact:
    """Flux envsubst runs over every resource a Kustomization builds, not just
    the file holding the placeholder, and replaces unknown expansions with an
    empty string. A payload under a substituted path is shredded unless it
    opts out.
    """

    def test_embedded_scripts_opt_out_of_substitution(self):
        offenders = []
        for rel in sorted(_substituted_paths()):
            directory = REPO_ROOT / rel.removeprefix("./")
            for path, doc in _built_resources(directory):
                if _opts_out_of_substitution(doc):
                    continue
                tokens = sorted(
                    {t for body in _payload_strings(doc) for t in _rewritten_expansions(body)}
                )
                if tokens:
                    name = (doc.get("metadata") or {}).get("name")
                    rel_path = path.relative_to(REPO_ROOT).as_posix()
                    offenders.append(f"{rel_path} ({doc.get('kind')}/{name}): {tokens}")

        assert not offenders, (
            f"embedded scripts under a postBuild-substituted path: {offenders}. "
            f"Flux would blank out each reference; annotate the resource with "
            f"{SUBSTITUTE_ANNOTATION}: disabled."
        )

    def test_walk_follows_resources_outside_the_kustomization_path(self):
        overlay = REPO_ROOT / "software" / "overlays" / "gateway-tls-single-host"
        visited = {path.relative_to(REPO_ROOT).as_posix() for path, _ in _built_resources(overlay)}

        assert "software/components/gateway/gateway.yaml" in visited, (
            f"the walk stopped inside {overlay.name}, so resources Flux substitutes "
            f"would go uninspected: {sorted(visited)}"
        )

    @pytest.mark.parametrize(
        "expansion",
        ["${VAR}", "${VAR:=default}", "${VAR:-default}", "${#VAR}", "${VAR/a/b}"],
    )
    def test_every_expansion_flux_rewrites_is_matched(self, expansion):
        assert _rewritten_expansions(expansion) == [expansion]

    @pytest.mark.parametrize("dollars", [1, 3, 5])
    def test_odd_dollar_runs_still_substitute(self, dollars):
        expansion = "$" * dollars + "{VAR}"
        assert _rewritten_expansions(expansion) == [expansion]

    @pytest.mark.parametrize("dollars", [2, 4, 6])
    def test_even_dollar_runs_are_escaped(self, dollars):
        assert _rewritten_expansions("$" * dollars + "{VAR}") == []


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
    def test_stack_tree_renders_only_the_stamp(self):
        rendered = [
            (doc.get("kind"), doc["metadata"]["name"])
            for _path, doc in _built_resources(PROFILES_DIR / Profile.BARE.value)
        ]
        assert rendered == [("ConfigMap", STACK_VERSION_CONFIGMAP)]

    def test_ingress_tree_is_empty(self):
        assert list(_flux_kustomizations(INGRESS_DIR / "bare")) == []


class TestStackVersionStamp:
    """Every profile carries the release its tree belongs to."""

    @pytest.mark.parametrize("profile", list(Profile), ids=lambda p: p.value)
    def test_profile_renders_the_stamp(self, profile):
        stamps = [
            doc
            for _path, doc in _built_resources(PROFILES_DIR / profile.value)
            if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == STACK_VERSION_CONFIGMAP
        ]
        assert len(stamps) == 1
        assert stamps[0]["metadata"]["namespace"] == STACK_VERSION_NAMESPACE

    def test_stamp_matches_the_release_manifest(self):
        manifest = json.loads((REPO_ROOT / ".release-please-manifest.json").read_text())
        stamp = yaml.safe_load(STACK_VERSION_MANIFEST.read_text(encoding="utf-8"))
        assert stamp["data"]["version"] == manifest["."]

    def test_release_please_rewrites_the_stamp(self):
        config = json.loads((REPO_ROOT / ".release-please-config.json").read_text())
        rel = STACK_VERSION_MANIFEST.relative_to(REPO_ROOT).as_posix()
        assert rel in config["packages"]["."]["extra-files"]
        assert "x-release-please-version" in next(
            line
            for line in STACK_VERSION_MANIFEST.read_text().splitlines()
            if line.lstrip().startswith("version:")
        )


class TestSingleRenderer:
    """One object, one Flux owner.

    Two Kustomizations rendering the same object each write their own desired
    state on every reconcile, so a base and an overlay of the same Gateway
    flip-flop forever. Byte-identical desired state is still contested
    ownership: either Kustomization can prune the object from its inventory.
    """

    @staticmethod
    def _root_owner(tree: Path) -> str:
        """The top-level Kustomization that renders a tree's own documents."""
        return f"<{tree.name} tree>"

    @classmethod
    def _renderings(cls, tree: Path) -> dict:
        """Map object identity to the {owner: source file} set.

        Both levels count. The top-level stack and ingress Kustomizations
        render the Flux Kustomization documents in their tree, and each of
        those renders whatever its spec.path builds. Recording only the
        second level would miss two trees declaring one Kustomization
        identity, which is contested ownership of that object just the same.
        """
        found: dict = {}
        root = cls._root_owner(tree)
        for item in _flux_kustomizations(tree):
            name = item["metadata"]["name"]
            found.setdefault((KUSTOMIZATION_KIND, item["metadata"]["namespace"], name), {})[
                root
            ] = tree.name
            directory = REPO_ROOT / item["spec"]["path"].removeprefix("./")
            for path, doc in _built_resources(directory):
                meta = doc.get("metadata") or {}
                key = (doc.get("kind"), meta.get("namespace") or "", meta.get("name"))
                found.setdefault(key, {})[name] = path.relative_to(REPO_ROOT).as_posix()
        return found

    @pytest.mark.parametrize("pair", PAIRS, ids=PAIR_IDS)
    def test_no_object_is_rendered_twice(self, pair):
        profile, mode = pair
        found: dict = {}
        for tree in (PROFILES_DIR / profile.value, _ingress_tree(profile, mode)):
            for key, owners in self._renderings(tree).items():
                found.setdefault(key, {}).update(owners)

        contested = {key: sorted(owners) for key, owners in found.items() if len(owners) > 1}

        assert not contested, (
            f"--profile {profile.value} --ingress-mode {mode.value} has objects claimed "
            f"by more than one Kustomization: {contested}. Each owner re-applies its own "
            "desired state every reconcile and the object never settles."
        )

    @pytest.mark.parametrize("pair", PAIRS, ids=PAIR_IDS)
    def test_gateway_owner_is_the_ingress_tree(self, pair):
        profile, mode = pair
        key = ("Gateway", GATEWAY_NAMESPACE, GATEWAY_NAME)
        profile_owners = self._renderings(PROFILES_DIR / profile.value).get(key, {})
        ingress_tree = _ingress_tree(profile, mode)
        ingress_owners = self._renderings(ingress_tree).get(key, {})
        if profile is Profile.BARE:
            assert not profile_owners
            assert not ingress_owners
            return

        assert not profile_owners, (
            "the Gateway's listeners are ingress-mode specific, so only the ingress "
            f"tree may render spi-gateway, found {sorted(profile_owners)}"
        )
        assert len(ingress_owners) == 1, (
            f"{ingress_tree.name}/stack.yaml must render spi-gateway exactly once, "
            f"found {sorted(ingress_owners)}"
        )

    def test_two_trees_declaring_one_kustomization_are_contested(self, tmp_path):
        """The root trees own their Kustomization documents, so they count too.

        Without the root-level record the guard walks only each child's
        spec.path and two trees declaring the same child look clean.
        """
        doc = (
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: spi-duplicate\n"
            "  namespace: osdu-flux\n"
            "spec:\n"
            "  path: ./software/components/inventory-handoff\n"
        )
        trees = []
        for name in ("first", "second"):
            tree = tmp_path / name
            tree.mkdir()
            (tree / "stack.yaml").write_text(doc, encoding="utf-8")
            trees.append(tree)

        found: dict = {}
        for tree in trees:
            for key, owners in self._renderings(tree).items():
                found.setdefault(key, {}).update(owners)

        contested = {key: sorted(owners) for key, owners in found.items() if len(owners) > 1}
        assert contested == {
            (KUSTOMIZATION_KIND, "osdu-flux", "spi-duplicate"): ["<first tree>", "<second tree>"]
        }

    @pytest.mark.parametrize("profile", [Profile.CORE, Profile.MINIMAL], ids=lambda p: p.value)
    def test_gateway_owner_name_is_shared_by_every_mode(self, profile):
        """One child identity, so switching --ingress-mode never prunes it.

        A per-mode name would make the top-level ingress Kustomization prune
        the outgoing child, and its MirrorPrune deletion takes the Gateway
        with it even once the incoming child has applied the object.
        """
        key = ("Gateway", GATEWAY_NAMESPACE, GATEWAY_NAME)
        owners = {
            mode: sorted(self._renderings(_ingress_tree(profile, mode)).get(key, {}))
            for mode in IngressMode
        }

        assert set(map(tuple, owners.values())) == {("spi-ingress-gateway",)}, (
            f"the {profile.value} profile renders the Gateway under more than one "
            f"Kustomization name: {owners}"
        )

    @pytest.mark.parametrize("profile", [Profile.CORE, Profile.MINIMAL], ids=lambda p: p.value)
    def test_legacy_gateway_inventory_is_orphaned(self, profile):
        gateway = _kustomization(PROFILES_DIR / profile.value, "spi-gateway")
        assert gateway["spec"]["path"] == "./software/components/inventory-handoff"
        assert gateway["spec"]["prune"] is False
        assert gateway["spec"]["deletionPolicy"] == "Orphan"

    @pytest.mark.parametrize("profile", [Profile.CORE, Profile.MINIMAL], ids=lambda p: p.value)
    def test_redis_source_handoff_disables_pruning(self, profile):
        redis = _kustomization(PROFILES_DIR / profile.value, "spi-redis")
        # Unlike retired inventories, Redis remains active and must be prunable later.
        assert redis["spec"]["prune"] is False

    @pytest.mark.parametrize("mode", ["dns", "dns-minimal"])
    def test_legacy_external_dns_inventory_is_orphaned(self, mode):
        external_dns = _kustomization(INGRESS_DIR / mode, "spi-external-dns")
        assert external_dns["spec"]["path"] == "./software/components/inventory-handoff"
        assert external_dns["spec"]["prune"] is False
        assert external_dns["spec"]["deletionPolicy"] == "Orphan"


def _software_documents():
    """Yield every untemplated manifest document under software/."""
    for path in sorted((REPO_ROOT / "software").rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        if "{{" in text:
            continue
        for doc in yaml.safe_load_all(text):
            if doc:
                yield path, doc


def _gateway_patch_ops(tree: Path) -> list:
    """JSON6902 ops the mode's Gateway owner applies to the platform Gateway."""
    owner = _kustomization(tree, "spi-ingress-gateway")
    directory = REPO_ROOT / owner["spec"]["path"].removeprefix("./")
    spec = yaml.safe_load((directory / "kustomization.yaml").read_text(encoding="utf-8")) or {}
    ops = []
    for patch in spec.get("patches", []):
        target = patch.get("target") or {}
        if target.get("kind") != "Gateway":
            continue
        # The patch must name the namespace so it scopes to the one Gateway.
        assert target.get("namespace") == GATEWAY_NAMESPACE, (
            f"{directory.name}: Gateway patch target must name {GATEWAY_NAMESPACE}"
        )
        ops.extend(yaml.safe_load(patch["patch"]))
    return ops


class TestAutomatedGatewayDeployment:
    """Managed Istio deploys the gateway workload and Service from the Gateway."""

    def test_gateway_lives_in_platform_without_a_bound_address(self):
        gateway = yaml.safe_load(
            (REPO_ROOT / "software" / "components" / "gateway" / "gateway.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert gateway["metadata"]["name"] == GATEWAY_NAME
        assert gateway["metadata"]["namespace"] == GATEWAY_NAMESPACE
        # A bound address tells Istio to reuse a Service instead of creating one.
        assert "addresses" not in gateway["spec"]

    def test_generated_service_is_documented_not_rendered(self):
        rendered = {
            ((doc.get("metadata") or {}).get("namespace") or "default", doc["metadata"]["name"])
            for _, doc in _software_documents()
            if doc.get("kind") == "Service"
        }
        assert (GATEWAY_NAMESPACE, GATEWAY_SERVICE) not in rendered, (
            "the Gateway's Service is Istio's to create; rendering it contests ownership"
        )
        design = (REPO_ROOT / "docs" / "design" / "gateway-ingress.md").read_text(encoding="utf-8")
        assert GATEWAY_SERVICE in design, (
            f"{GATEWAY_SERVICE} is referenced by the CLI but gateway-ingress.md never names it"
        )

    @pytest.mark.parametrize("mode", ["azure", "azure-minimal"])
    def test_azure_modes_carry_the_dns_label_on_the_gateway(self, mode):
        ops = [
            op
            for op in _gateway_patch_ops(INGRESS_DIR / mode)
            if op["path"] == "/spec/infrastructure"
        ]
        assert len(ops) == 1, f"{mode} must add spec.infrastructure exactly once, found {ops}"
        annotations = ops[0]["value"]["annotations"]
        assert annotations[AZURE_DNS_LABEL_ANNOTATION] == "${DNS_LABEL}"

    @pytest.mark.parametrize("mode", ["dns", "dns-minimal", "ip", "ip-minimal"])
    def test_other_modes_leave_the_gateway_infrastructure_alone(self, mode):
        ops = _gateway_patch_ops(INGRESS_DIR / mode)
        assert not [op for op in ops if op["path"].startswith("/spec/infrastructure")]
        assert "spi-ingress-dns-label" not in _declared_names(INGRESS_DIR / mode)

    def test_every_parent_ref_names_the_platform_gateway(self):
        expected = {"name": GATEWAY_NAME, "namespace": GATEWAY_NAMESPACE}
        refs = []
        for path, doc in _software_documents():
            if doc.get("kind") == "HTTPRoute":
                refs.extend((path, ref) for ref in doc["spec"]["parentRefs"])
            elif doc.get("kind") == "ClusterIssuer":
                for solver in doc["spec"]["acme"]["solvers"]:
                    route = (solver.get("http01") or {}).get("gatewayHTTPRoute") or {}
                    refs.extend((path, ref) for ref in route.get("parentRefs", []))
        assert refs
        wrong = [
            (path.relative_to(REPO_ROOT).as_posix(), ref)
            for path, ref in refs
            if {key: ref.get(key) for key in expected} != expected
        ]
        assert not wrong, f"parentRefs must attach to {expected}: {wrong}"

    def test_tls_listeners_reference_secrets_in_the_gateway_namespace(self):
        for mode in ("azure", "dns"):
            for op in _gateway_patch_ops(INGRESS_DIR / mode):
                for ref in (op.get("value") or {}).get("tls", {}).get("certificateRefs", []):
                    assert "namespace" not in ref, f"{mode}: {ref} needs a ReferenceGrant"
        grants = [doc for _, doc in _software_documents() if doc.get("kind") == "ReferenceGrant"]
        assert not [g for g in grants if any(f.get("kind") == "Gateway" for f in g["spec"]["from"])]

    @pytest.mark.parametrize(
        "mode", ["azure", "azure-minimal", "dns", "dns-minimal", "ip", "ip-minimal"]
    )
    def test_retired_gateway_owner_keeps_its_inventory_without_pruning(self, mode):
        """Pre-move clusters list a Gateway in aks-istio-ingress under this name.

        Admission denies Flux the delete, so the inventory must never be
        pruned, on a spec change or on the Kustomization's own deletion.
        """
        retired = _kustomization(INGRESS_DIR / mode, "spi-gateway-tls")
        assert retired["spec"]["path"] == "./software/components/inventory-handoff"
        assert retired["spec"]["prune"] is False
        assert retired["spec"]["deletionPolicy"] == "Orphan"
        owner = _kustomization(INGRESS_DIR / mode, "spi-ingress-gateway")
        assert owner["spec"]["prune"] is True
        assert "deletionPolicy" not in owner["spec"]
        assert "spi-gateway-tls" not in _dependency_names(INGRESS_DIR / mode)


class TestBorrowedPodDisruptionSubstitution:
    """Every service HelmRelease reads its own do-not-disrupt key from the image lock."""

    def test_every_service_release_substitutes_its_own_key(self):
        from spi.images import IMAGE_REGISTRY, do_not_disrupt_key

        releases = {
            entry.file: name
            for name, entry in IMAGE_REGISTRY.items()
            if entry.file.startswith(("services/", "services-reference/"))
        }
        manifests = sorted(
            path
            for directory in ("services", "services-reference")
            for path in (STACKS / directory).glob("*.yaml")
            if path.name != "kustomization.yaml"
        )
        assert {str(path.relative_to(STACKS)) for path in manifests} == set(releases)
        for path in manifests:
            doc = yaml.safe_load(path.read_text())
            service = releases[str(path.relative_to(STACKS))]
            value = doc["spec"]["values"]["karpenter"]["doNotDisrupt"]
            assert value == f"${{{do_not_disrupt_key(service)}:=false}}", path.name

    @pytest.mark.parametrize("name", ["spi-osdu-services", "spi-osdu-reference"])
    def test_service_kustomizations_substitute_from_the_image_lock(self, name):
        item = _kustomization(PROFILES_DIR / "core", name)
        sources = item["spec"]["postBuild"]["substituteFrom"]
        assert {"kind": "ConfigMap", "name": "osdu-image-lock"} in sources


class TestForkRbac:
    """The deploy identity's namespace Roles (ADR-032) ship with the core profile."""

    def test_core_substitutes_the_principal_from_cluster_config(self):
        item = _kustomization(PROFILES_DIR / "core", "spi-fork-rbac")
        sources = item["spec"]["postBuild"]["substituteFrom"]
        assert {"kind": "ConfigMap", "name": "spi-cluster-config", "optional": False} in sources
        assert {"name": "spi-namespaces"} in item["spec"]["dependsOn"]

    def test_roles_grant_no_write_beyond_the_lock_and_no_secrets(self):
        import yaml

        docs = list(
            yaml.safe_load_all(
                (STACKS.parent.parent / "components/fork-rbac/fork-rbac.yaml").read_text()
            )
        )
        roles = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Role"}
        bindings = [d for d in docs if d["kind"] == "RoleBinding"]
        assert set(roles) == {"spi-fork-deployer", "spi-fork-verifier"}
        for role in roles.values():
            for rule in role["rules"]:
                assert "secrets" not in rule["resources"]
                assert not {"create", "delete", "update"} & set(rule["verbs"])
                if "patch" in rule["verbs"]:
                    assert rule["resourceNames"] == ["osdu-image-lock"]
        verifier_verbs = {
            resource: set(rule["verbs"])
            for rule in roles["spi-fork-verifier"]["rules"]
            for resource in rule["resources"]
        }
        # spi status lists the members Jobs by label and reads a failed one's pods.
        assert {"get", "list"} <= verifier_verbs["jobs"]
        assert {"get", "list"} <= verifier_verbs["pods"]
        for binding in bindings:
            (subject,) = binding["subjects"]
            assert subject["kind"] == "User"
            assert subject["name"] == (
                "${DEPLOY_IDENTITY_PRINCIPAL_ID:=spi-deploy-identity-unprovisioned}"
            )


class TestLoadRegistry:
    """The named loads `spi load` reads from the cluster."""

    INIT = STACKS / "init"

    def _registry(self) -> dict:
        return yaml.safe_load((self.INIT / "load-registry.yaml").read_text(encoding="utf-8"))

    def test_init_layer_generates_the_configmap_under_a_stable_name(self):
        kustomization = yaml.safe_load(
            (self.INIT / "kustomization.yaml").read_text(encoding="utf-8")
        )
        assert kustomization["configMapGenerator"] == [
            {
                "name": "spi-load-registry",
                "namespace": "osdu",
                "files": ["registry.yaml=load-registry.yaml"],
            }
        ]
        assert kustomization["generatorOptions"] == {"disableNameSuffixHash": True}

    def test_only_the_core_profile_delivers_it(self):
        delivering = {
            profile.value
            for profile in Profile
            if "./software/stacks/osdu/init" in _referenced_paths(PROFILES_DIR / profile.value)
        }
        assert delivering == {"core"}

    def test_init_layer_substitutes_nothing_into_the_registry(self):
        assert "postBuild" not in _kustomization(PROFILES_DIR / "core", "spi-osdu-init")["spec"]

    def test_names_are_unique_and_requirements_resolve(self):
        loads = self._registry()["loads"]
        names = [load["name"] for load in loads]
        assert len(names) == len(set(names))
        for load in loads:
            assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", load["name"])
            assert set(load.get("requires", [])) <= set(names) - {load["name"]}

    def test_an_archive_source_is_pinned_by_commit(self):
        archives = [
            load["source"]
            for load in self._registry()["loads"]
            if load["source"]["type"] == "gitlab-archive"
        ]
        assert archives
        for source in archives:
            assert re.fullmatch(r"[0-9a-f]{40}", source["commit"])

    def test_the_schema_load_stays_with_flux(self):
        schemas = next(load for load in self._registry()["loads"] if load["name"] == "schemas")
        assert schemas["runner"] == "flux"
        assert schemas["source"] == {"type": "paired-image", "service": "schema"}
