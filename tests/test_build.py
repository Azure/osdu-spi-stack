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

"""Local builds: what a checkout must hold, what the ACR task runs, and what comes back."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from spi import build, cli
from spi.build import (
    BuildError,
    BuiltImage,
    Registry,
    build_image,
    checkout_state,
    environment_registry,
    maven_command,
    require_buildable,
    require_cluster_pull,
    require_local_manifest,
    resolve_jar,
    stage_context,
    task_document,
)

_DIGEST = "sha256:" + "a" * 64
_HEAD = "b" * 40
_REGISTRY = Registry(
    name="osdutest12345",
    login_server="osdutest12345.azurecr.io",
    resource_id="/subscriptions/sub/resourceGroups/spi-stack-test/providers/"
    "Microsoft.ContainerRegistry/registries/osdutest12345",
    resource_group="spi-stack-test",
    subscription="sub",
    cluster="spi-stack-test",
)


def _checkout(tmp_path: Path, service: str = "partition", module: str | None = None) -> Path:
    """A fork checkout with a descriptor, the canonical Dockerfile, and one built JAR."""

    root = tmp_path / "fork"
    (root / ".spi").mkdir(parents=True)
    (root / ".spi/service.yaml").write_text(
        yaml.safe_dump({"schemaVersion": 3, "service": {"name": service}}), encoding="utf-8"
    )
    (root / "build").mkdir()
    (root / "build/Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / "build/docker-entrypoint.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    _jar(root, module or f"{service}-azure")
    return root


def _jar(root: Path, module: str, name: str = "app-1.0-spring-boot.jar") -> Path:
    target = root / "provider" / module / "target"
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    path.write_bytes(b"jar")
    return path


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class TestRequireBuildable:
    def test_a_matching_checkout_passes(self, tmp_path):
        require_buildable("partition", _checkout(tmp_path))

    def test_unknown_service_is_refused(self, tmp_path):
        with pytest.raises(BuildError, match="Unknown service"):
            require_buildable("nope", _checkout(tmp_path))

    def test_the_loader_is_not_built_on_its_own(self, tmp_path):
        with pytest.raises(BuildError, match="Unknown service"):
            require_buildable("schema-load", _checkout(tmp_path))

    def test_a_checkout_of_another_service_is_refused(self, tmp_path):
        with pytest.raises(BuildError, match="builds 'legal', not 'partition'"):
            require_buildable("partition", _checkout(tmp_path, service="legal"))

    def test_a_checkout_without_the_dockerfile_is_refused(self, tmp_path):
        root = _checkout(tmp_path)
        (root / "build/Dockerfile").unlink()
        with pytest.raises(BuildError, match="build/Dockerfile"):
            require_buildable("partition", root)

    def test_a_checkout_without_a_descriptor_is_refused(self, tmp_path):
        root = _checkout(tmp_path)
        (root / ".spi/service.yaml").unlink()
        with pytest.raises(BuildError, match="service.yaml"):
            require_buildable("partition", root)


class TestResolveJar:
    def test_the_conventional_module_wins(self, tmp_path):
        root = _checkout(tmp_path)
        assert resolve_jar(root, "partition") == (
            "provider/partition-azure/target/app-1.0-spring-boot.jar"
        )

    def test_a_single_azure_jar_is_discovered_under_another_module_name(self, tmp_path):
        root = _checkout(tmp_path, service="entitlements", module="entitlements-v2-azure")
        assert resolve_jar(root, "entitlements").startswith("provider/entitlements-v2-azure/")

    def test_several_azure_jars_tiebreak_on_the_service_name(self, tmp_path):
        root = _checkout(tmp_path, service="entitlements", module="entitlements-v2-azure")
        _jar(root, "other-azure")
        assert resolve_jar(root, "entitlements").startswith("provider/entitlements-v2-azure/")

    def test_an_ambiguous_set_is_refused(self, tmp_path):
        root = _checkout(tmp_path, service="file", module="one-azure")
        _jar(root, "two-azure")
        with pytest.raises(BuildError, match="No single Spring Boot JAR"):
            resolve_jar(root, "file")

    def test_no_jar_names_the_build_step(self, tmp_path):
        root = _checkout(tmp_path)
        next((root / "provider").rglob("*.jar")).unlink()
        with pytest.raises(BuildError, match="found: none"):
            resolve_jar(root, "partition")

    def test_a_requested_path_is_taken_as_given(self, tmp_path):
        root = _checkout(tmp_path)
        _jar(root, "partition-azure", "second-spring-boot.jar")
        requested = "provider/partition-azure/target/second-spring-boot.jar"
        assert resolve_jar(root, "partition", requested) == requested

    def test_two_jars_in_the_conventional_module_are_not_guessed_between(self, tmp_path):
        root = _checkout(tmp_path)
        _jar(root, "partition-azure", "app-0.9-spring-boot.jar")
        with pytest.raises(BuildError, match="holds 2 Spring Boot JARs"):
            resolve_jar(root, "partition")

    @pytest.mark.parametrize("name", ["with space-spring-boot.jar", "pom.xml"])
    def test_a_path_the_task_line_cannot_carry_is_refused(self, tmp_path, name):
        root = _checkout(tmp_path)
        path = _jar(root, "partition-azure", name)
        with pytest.raises(BuildError, match="is not a .jar path"):
            resolve_jar(root, "partition", path.relative_to(root).as_posix())

    def test_a_requested_path_outside_the_checkout_is_refused(self, tmp_path):
        root = _checkout(tmp_path)
        outside = tmp_path / "elsewhere.jar"
        outside.write_bytes(b"jar")
        with pytest.raises(BuildError, match="not a JAR inside"):
            resolve_jar(root, "partition", str(outside))


class TestCheckoutState:
    def _git(self, monkeypatch, head, status, returncode=0):
        def fake(cmd, **kwargs):
            out = head if "rev-parse" in cmd else status
            return _completed(out, returncode)

        monkeypatch.setattr(build, "run_process", fake)

    def test_a_clean_tree_names_its_commit(self, monkeypatch, tmp_path):
        self._git(monkeypatch, _HEAD + "\n", "")
        assert checkout_state(tmp_path) == (_HEAD, False)

    def test_uncommitted_changes_mark_the_build_dirty(self, monkeypatch, tmp_path):
        self._git(monkeypatch, _HEAD, " M pom.xml\n")
        assert checkout_state(tmp_path) == (_HEAD, True)

    def test_a_directory_git_cannot_describe_is_refused(self, monkeypatch, tmp_path):
        self._git(monkeypatch, "", "", returncode=128)
        with pytest.raises(BuildError, match="not a git checkout"):
            checkout_state(tmp_path)


class TestTask:
    def test_the_task_builds_with_buildkit_and_pushes_the_same_image(self):
        document = yaml.safe_load(task_document("local/partition:sha-abc", "provider/x/app.jar"))
        build_step, push_step = document["steps"]
        assert build_step["env"] == ["DOCKER_BUILDKIT=1"]
        assert "-f build/Dockerfile" in build_step["build"]
        assert "--build-arg JAR_FILE=provider/x/app.jar" in build_step["build"]
        assert "--platform linux/amd64" in build_step["build"]
        assert push_step == {"push": ["$Registry/local/partition:sha-abc"]}

    def test_the_context_holds_only_what_the_dockerfile_reads(self, tmp_path):
        root = _checkout(tmp_path)
        (root / "src").mkdir()
        (root / "src/Main.java").write_text("class Main {}", encoding="utf-8")
        jar = resolve_jar(root, "partition")
        context = tmp_path / "context"
        context.mkdir()

        stage_context(root, jar, "local/partition:sha-abc", context)

        staged = sorted(
            p.relative_to(context).as_posix() for p in context.rglob("*") if p.is_file()
        )
        assert staged == ["acr-task.yaml", "build/Dockerfile", "build/docker-entrypoint.sh", jar]

    def test_a_link_under_build_is_not_followed_into_the_context(self, tmp_path):
        root = _checkout(tmp_path)
        secret = tmp_path / "secret"
        secret.write_text("token", encoding="utf-8")
        (root / "build/leak").symlink_to(secret)
        context = tmp_path / "context"
        context.mkdir()

        with pytest.raises(BuildError, match="symbolic links .*leak"):
            stage_context(root, resolve_jar(root, "partition"), "local/partition:x", context)
        assert not (context / "build").exists()

    def test_maven_uses_the_checkouts_settings_and_the_template_profiles(self, tmp_path):
        root = _checkout(tmp_path)
        assert maven_command(root)[3:] == ["clean", "install", "-P", "core,azure", "-DskipTests"]
        (root / ".mvn").mkdir()
        (root / ".mvn/community-maven.settings.xml").write_text("<settings/>", encoding="utf-8")
        command = maven_command(root, ["package"])
        assert "--settings" in command and command[-1] == "package"


class TestEnvironmentRegistry:
    def _wire(self, monkeypatch, registries):
        monkeypatch.setattr(
            build,
            "read_cluster_config",
            lambda: {
                "AZURE_RESOURCE_GROUP": "spi-stack-test",
                "AZURE_SUBSCRIPTION_ID": "sub",
                "AKS_CLUSTER_NAME": "spi-stack-test",
            },
        )
        calls = []

        def fake(cmd, **kwargs):
            calls.append(cmd)
            return _completed(json.dumps(registries))

        monkeypatch.setattr(build, "run_process", fake)
        return calls

    def test_the_one_registry_in_the_group_is_the_environments(self, monkeypatch):
        calls = self._wire(
            monkeypatch,
            [{"name": "osdutest12345", "loginServer": "OsduTest12345.azurecr.io", "id": "/id"}],
        )
        registry = environment_registry()
        assert registry.login_server == "osdutest12345.azurecr.io"
        assert registry.cluster == "spi-stack-test"
        assert calls[0][:5] == ["az", "acr", "list", "--resource-group", "spi-stack-test"]
        assert build.local_repository(registry, "partition") == (
            "osdutest12345.azurecr.io/local/partition"
        )

    @pytest.mark.parametrize("registries", [[], [{"name": "a"}, {"name": "b"}]])
    def test_any_other_count_is_refused(self, monkeypatch, registries):
        self._wire(monkeypatch, registries)
        with pytest.raises(BuildError, match="exactly the one"):
            environment_registry()

    def test_a_cluster_with_no_recorded_group_is_refused(self, monkeypatch):
        monkeypatch.setattr(build, "read_cluster_config", lambda: {})
        with pytest.raises(BuildError, match="records no resource group"):
            environment_registry()


class TestRegistryChecks:
    def test_a_pushed_digest_is_found(self, monkeypatch):
        monkeypatch.setattr(build, "run_process", lambda cmd, **kw: _completed(_DIGEST + "\n"))
        require_local_manifest(_REGISTRY, "partition", _DIGEST)

    def test_a_missing_digest_names_the_build(self, monkeypatch):
        monkeypatch.setattr(
            build, "run_process", lambda cmd, **kw: _completed("", 1, "manifest unknown")
        )
        with pytest.raises(BuildError, match="spi build partition --source"):
            require_local_manifest(_REGISTRY, "partition", _DIGEST)

    def _pull(self, monkeypatch, assignments, kubelet="kubelet-oid"):
        def fake(cmd, **kwargs):
            if cmd[:3] == ["az", "aks", "show"]:
                profile = {"kubeletidentity": {"objectId": kubelet}} if kubelet else {}
                return _completed(json.dumps({"identityProfile": profile}))
            assert cmd[:4] == ["az", "role", "assignment", "list"]
            assert cmd[cmd.index("--assignee") + 1] == kubelet
            assert cmd[cmd.index("--scope") + 1] == _REGISTRY.resource_id
            assert cmd[cmd.index("--role") + 1] == "AcrPull"
            assert "--include-inherited" in cmd
            return _completed(json.dumps(assignments))

        monkeypatch.setattr(build, "run_process", fake)

    def test_a_cluster_holding_the_role_can_pull(self, monkeypatch):
        self._pull(monkeypatch, [{"roleDefinitionName": "AcrPull"}])
        require_cluster_pull(_REGISTRY)

    def test_a_cluster_without_the_role_is_sent_to_spi_up(self, monkeypatch):
        self._pull(monkeypatch, [])
        with pytest.raises(BuildError, match="Run 'spi up' on this release"):
            require_cluster_pull(_REGISTRY)

    def test_a_cluster_reporting_no_kubelet_identity_is_refused(self, monkeypatch):
        self._pull(monkeypatch, [], kubelet="")
        with pytest.raises(BuildError, match="no kubelet identity"):
            require_cluster_pull(_REGISTRY)


class TestBuildImage:
    def _wire(
        self,
        monkeypatch,
        *,
        dirty=False,
        maven=0,
        task=0,
        digest=_DIGEST,
        packages=True,
        dirties=False,
    ):
        commands: list[list[str]] = []
        staged: dict = {}
        tree = {"dirty": dirty}
        monkeypatch.setattr(build.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(build, "checkout_state", lambda checkout: (_HEAD, tree["dirty"]))
        monkeypatch.setattr(build, "environment_registry", lambda: _REGISTRY)

        def fake_run_command(cmd, **kwargs):
            commands.append(cmd)
            if cmd[0] == "mvn":
                tree["dirty"] = tree["dirty"] or dirties
                for jar in Path(kwargs["cwd"]).glob("provider/*/target/*.jar") if packages else ():
                    later = jar.stat().st_mtime_ns + 10**9
                    os.utime(jar, ns=(later, later))
                return _completed(returncode=maven)
            context = Path(kwargs["cwd"])
            staged["files"] = sorted(
                p.relative_to(context).as_posix() for p in context.rglob("*") if p.is_file()
            )
            return _completed(returncode=task)

        monkeypatch.setattr(build, "run_command", fake_run_command)
        monkeypatch.setattr(build, "run_process", lambda cmd, **kw: _completed(digest + "\n"))
        return commands, staged

    def test_a_clean_checkout_builds_to_its_commit_tag(self, monkeypatch, tmp_path):
        commands, staged = self._wire(monkeypatch)

        built = build_image("partition", _checkout(tmp_path))

        assert built == BuiltImage(
            "partition",
            "osdutest12345.azurecr.io/local/partition",
            _DIGEST,
            "sha-" + "b" * 12,
            _HEAD,
            False,
        )
        assert built.reference == f"osdutest12345.azurecr.io/local/partition@{_DIGEST}"
        assert built.source_sha == _HEAD
        assert commands[0][0] == "mvn"
        assert commands[1][:5] == ["az", "acr", "run", "--registry", "osdutest12345"]
        assert "acr-task.yaml" in staged["files"]

    def test_a_dirty_checkout_never_reads_as_its_commit(self, monkeypatch, tmp_path):
        self._wire(monkeypatch, dirty=True)
        built = build_image("partition", _checkout(tmp_path))
        assert built.tag == "sha-" + "b" * 12 + "-dirty"
        assert built.source_sha == _HEAD + "-dirty"

    def test_a_jar_maven_did_not_just_build_never_reads_as_the_commit(self, monkeypatch, tmp_path):
        commands, _ = self._wire(monkeypatch)
        built = build_image("partition", _checkout(tmp_path), skip_maven=True)
        assert [command[0] for command in commands] == ["az"]
        assert built.tag == "sha-" + "b" * 12 + "-prebuilt"
        assert built.source_sha == _HEAD + "-prebuilt"

    def test_a_jar_maven_left_untouched_never_reads_as_the_commit(self, monkeypatch, tmp_path):
        self._wire(monkeypatch, packages=False)
        built = build_image("partition", _checkout(tmp_path), maven_arguments=["validate"])
        assert built.source_sha == _HEAD + "-prebuilt"

    def test_a_tree_maven_dirtied_never_reads_as_the_commit(self, monkeypatch, tmp_path):
        self._wire(monkeypatch, dirties=True)
        built = build_image("partition", _checkout(tmp_path), maven_arguments=["spotless:apply"])
        assert built.source_sha == _HEAD + "-dirty"

    def test_a_failed_maven_build_pushes_nothing(self, monkeypatch, tmp_path):
        commands, _ = self._wire(monkeypatch, maven=1)
        with pytest.raises(BuildError, match="Maven exited 1"):
            build_image("partition", _checkout(tmp_path))
        assert [command[0] for command in commands] == ["mvn"]

    def test_a_failed_task_is_not_a_built_image(self, monkeypatch, tmp_path):
        self._wire(monkeypatch, task=1)
        with pytest.raises(BuildError, match="was not pushed"):
            build_image("partition", _checkout(tmp_path))

    def test_a_registry_that_reports_no_digest_is_refused(self, monkeypatch, tmp_path):
        self._wire(monkeypatch, digest="")
        with pytest.raises(BuildError, match="reports no digest"):
            build_image("partition", _checkout(tmp_path))

    def test_a_missing_tool_is_named(self, monkeypatch, tmp_path):
        self._wire(monkeypatch)
        monkeypatch.setattr(build.shutil, "which", lambda name: None if name == "mvn" else name)
        with pytest.raises(BuildError, match="mvn is not on PATH"):
            build_image("partition", _checkout(tmp_path))


class TestBuildCli:
    def _built(self, dirty=False):
        return BuiltImage(
            "partition", "r.azurecr.io/local/partition", _DIGEST, "sha-bbb", _HEAD, dirty
        )

    def test_a_build_prints_the_reference_to_pin(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "_guarded_context", lambda output_json: "spi-test")
        captured = {}

        def fake_build(service, checkout, **kwargs):
            captured.update(service=service, checkout=checkout, **kwargs)
            return self._built()

        monkeypatch.setattr(cli, "build_image", fake_build)

        result = CliRunner().invoke(
            cli.app, ["build", "partition", "--source", str(tmp_path), "--", "package", "-o"]
        )

        assert result.exit_code == 0
        assert captured["maven_arguments"] == ["package", "-o"]
        assert f"r.azurecr.io/local/partition@{_DIGEST}" in result.output.replace("\n", "")

    def test_json_reports_the_image_and_its_state(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "_guarded_context", lambda output_json: "spi-test")
        monkeypatch.setattr(cli, "build_image", lambda *a, **kw: self._built(dirty=True))

        result = CliRunner().invoke(
            cli.app, ["build", "partition", "--source", str(tmp_path), "--json"]
        )

        outcome = json.loads(result.output.strip().splitlines()[-1])
        assert outcome["outcome"] == "built"
        assert outcome["image"] == f"r.azurecr.io/local/partition@{_DIGEST}"
        assert outcome["dirty"] is True

    def test_a_refused_build_exits_one(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "_guarded_context", lambda output_json: "spi-test")

        def refuse(*args, **kwargs):
            raise BuildError("Maven exited 1; no image was built.")

        monkeypatch.setattr(cli, "build_image", refuse)
        result = CliRunner().invoke(cli.app, ["build", "partition", "--source", str(tmp_path)])
        assert result.exit_code == 1
        assert "Maven exited 1" in result.output

    def test_a_source_that_is_not_a_directory_is_a_usage_error(self, tmp_path):
        result = CliRunner().invoke(
            cli.app, ["build", "partition", "--source", str(tmp_path / "missing")]
        )
        assert result.exit_code == 2
