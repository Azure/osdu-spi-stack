# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Release-tag deployment and source-finalization contracts."""

from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from spi import azure_infra, cli, deploy
from spi.config import Config, Profile
from spi.images import ResolvedImage


def _git_repository(ref_field: str, ref: str, revision: str) -> dict:
    return {
        "spec": {"ref": {ref_field: ref}},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "artifact": {"revision": revision},
        },
    }


def test_resolved_revision_accepts_tag_and_commit():
    sha = "a" * 40

    assert (
        deploy._resolved_revision(
            _git_repository("tag", "v0.6.0", f"v0.6.0@sha1:{sha}"),
            "v0.6.0",
            "tag",
        )
        == sha
    )


def test_resolved_revision_rejects_wrong_ref():
    with pytest.raises(RuntimeError, match="expected"):
        deploy._resolved_revision(
            _git_repository("tag", "v0.5.2", f"v0.5.2@sha1:{'a' * 40}"),
            "v0.6.0",
            "tag",
        )


def test_resolved_revision_accepts_qualified_tag_form():
    sha = "a" * 40

    assert (
        deploy._resolved_revision(
            _git_repository("tag", "v0.6.0", f"refs/tags/v0.6.0@sha1:{sha}"),
            "v0.6.0",
            "tag",
        )
        == sha
    )


def test_resolved_revision_rejects_same_named_branch_for_tag_deployment():
    sha = "a" * 40

    with pytest.raises(RuntimeError, match="does not identify"):
        deploy._resolved_revision(
            _git_repository("tag", "v0.6.0", f"refs/heads/v0.6.0@sha1:{sha}"),
            "v0.6.0",
            "tag",
        )


def test_resolved_revision_accepts_branch_form_for_branch_deployment():
    sha = "a" * 40

    assert (
        deploy._resolved_revision(
            _git_repository("branch", "main", f"main@sha1:{sha}"),
            "main",
            "branch",
        )
        == sha
    )
    assert (
        deploy._resolved_revision(
            _git_repository("branch", "main", f"refs/heads/main@sha1:{sha}"),
            "main",
            "branch",
        )
        == sha
    )


def test_resolved_revision_rejects_same_named_tag_for_branch_deployment():
    sha = "a" * 40

    with pytest.raises(RuntimeError, match="does not identify"):
        deploy._resolved_revision(
            _git_repository("branch", "main", f"refs/tags/main@sha1:{sha}"),
            "main",
            "branch",
        )


def test_finalize_suspends_before_record(monkeypatch):
    calls = []
    config = Config.from_env("shared", repo_tag="v0.6.0")
    monkeypatch.setattr(deploy, "_wait_for_git_repository", lambda: calls.append("wait"))
    monkeypatch.setattr(
        deploy,
        "_set_source_suspended",
        lambda suspended, check=True: calls.append(f"suspend:{suspended}:{check}"),
    )
    monkeypatch.setattr(
        deploy,
        "_read_git_repository",
        lambda: _git_repository("tag", "v0.6.0", f"v0.6.0@sha1:{'a' * 40}"),
    )

    def run(command, **kwargs):
        calls.append(command[:3])
        stdout = (
            "/subscriptions/sub/resourceGroups/spi-stack-shared"
            if command[:3]
            == [
                "az",
                "group",
                "show",
            ]
            else ""
        )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(deploy, "run_command", run)

    def record(**kwargs):
        calls.append(("record", kwargs))
        return SimpleNamespace(maintenance=True)

    monkeypatch.setattr(deploy, "upsert_deploy_record", record)
    monkeypatch.setattr(deploy, "display_result", lambda _message: None)

    deploy._finalize_gitops_source(config)

    assert calls.index("suspend:True:True") < next(
        index
        for index, value in enumerate(calls)
        if isinstance(value, tuple) and value[0] == "record"
    )
    record_call = next(
        value for value in calls if isinstance(value, tuple) and value[0] == "record"
    )
    assert record_call[1]["initial_maintenance"] is True
    assert record_call[1]["env"] == "shared"
    assert record_call[1]["profile"] == "core"


def test_finalize_resuspends_on_reconcile_failure(monkeypatch):
    calls = []
    config = Config.from_env("shared", repo_tag="v0.6.0")
    monkeypatch.setattr(deploy, "_wait_for_git_repository", lambda: None)
    monkeypatch.setattr(
        deploy,
        "_set_source_suspended",
        lambda suspended, check=True: calls.append((suspended, check)),
    )

    def fail(command, **kwargs):
        raise RuntimeError("reconcile failed")

    monkeypatch.setattr(deploy, "run_command", fail)

    with pytest.raises(RuntimeError, match="reconcile failed"):
        deploy._finalize_gitops_source(config)

    assert calls == [(False, True), (True, False)]


def test_finalize_resuspends_when_revision_names_wrong_namespace(monkeypatch):
    """A same-named branch artifact must still fail closed: re-suspend, no record."""
    calls = []
    config = Config.from_env("shared", repo_tag="v0.6.0")
    monkeypatch.setattr(deploy, "_wait_for_git_repository", lambda: None)
    monkeypatch.setattr(
        deploy,
        "_set_source_suspended",
        lambda suspended, check=True: calls.append((suspended, check)),
    )
    monkeypatch.setattr(
        deploy,
        "run_command",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        deploy,
        "_read_git_repository",
        lambda: _git_repository("tag", "v0.6.0", f"refs/heads/v0.6.0@sha1:{'a' * 40}"),
    )
    record_calls = []
    monkeypatch.setattr(
        deploy, "upsert_deploy_record", lambda **kwargs: record_calls.append(kwargs)
    )

    with pytest.raises(RuntimeError, match="does not identify"):
        deploy._finalize_gitops_source(config)

    assert calls == [(False, True), (True, False)]
    assert record_calls == []


def test_finalize_resuspends_when_final_suspend_fails(monkeypatch):
    """A failing final suspend must not leave the source resumed or swallow the error."""
    calls = []
    config = Config.from_env("shared", repo_tag="v0.6.0")
    monkeypatch.setattr(deploy, "_wait_for_git_repository", lambda: None)

    def set_suspended(suspended, check=True):
        calls.append((suspended, check))
        if suspended and check:
            raise RuntimeError("patch failed")

    monkeypatch.setattr(deploy, "_set_source_suspended", set_suspended)
    monkeypatch.setattr(
        deploy,
        "run_command",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        deploy,
        "_read_git_repository",
        lambda: _git_repository("tag", "v0.6.0", f"v0.6.0@sha1:{'a' * 40}"),
    )
    record_calls = []
    monkeypatch.setattr(
        deploy, "upsert_deploy_record", lambda **kwargs: record_calls.append(kwargs)
    )

    with pytest.raises(RuntimeError, match="patch failed"):
        deploy._finalize_gitops_source(config)

    assert calls == [(False, True), (True, True), (True, False)]
    assert record_calls == []


def test_up_rejects_explicit_branch_with_tag():
    result = CliRunner().invoke(
        cli.app,
        ["up", "--env", "shared", "--branch", "main", "--tag", "v0.6.0"],
    )

    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_up_rejects_tag_from_source_checkout():
    result = CliRunner().invoke(cli.app, ["up", "--env", "shared", "--tag", "v0.6.0"])

    assert result.exit_code == 2
    assert "released spi wheel" in result.output


def test_up_rejects_wheel_version_that_does_not_match_tag(monkeypatch):
    monkeypatch.setattr(cli, "__version__", "0.6.1")

    result = CliRunner().invoke(cli.app, ["up", "--env", "shared", "--tag", "v0.6.0"])

    assert result.exit_code == 2
    assert "requires spi 0.6.0" in result.output


def test_name_suffix_override_must_match_existing(monkeypatch):
    monkeypatch.setattr("spi.azure_infra.read_rg_suffix_tag", lambda _rg: "abc12")
    monkeypatch.setattr("spi.azure_infra.detect_legacy_keyvault", lambda _rg, _env: False)

    with pytest.raises(typer.BadParameter, match="recorded suffix"):
        cli._resolve_name_suffix("shared", True, requested_suffix="other")


def test_empty_name_suffix_requires_environment():
    with pytest.raises(typer.BadParameter, match="--name-suffix requires --env"):
        cli._resolve_name_suffix("", True, requested_suffix="")


def test_empty_name_suffix_with_environment_preserves_legacy_marker(monkeypatch):
    monkeypatch.setattr("spi.azure_infra.read_rg_suffix_tag", lambda _rg: None)
    monkeypatch.setattr("spi.azure_infra.detect_legacy_keyvault", lambda _rg, _env: False)
    monkeypatch.setattr(
        "spi.config.generate_name_suffix",
        lambda: pytest.fail("explicit empty suffix must not generate a random suffix"),
    )
    monkeypatch.setattr(
        cli,
        "run_command",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="false", stderr=""),
    )

    assert cli._resolve_name_suffix("shared", True, requested_suffix="") == ""


def test_connect_cluster_reuses_hardened_kubeconfig_sequence(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["az", "account", "show"]:
            return SimpleNamespace(returncode=0, stdout="tenant-id\n", stderr="")
        if command[:4] == ["kubectl", "config", "view", "--minify"]:
            return SimpleNamespace(returncode=0, stdout="cluster-user\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(azure_infra, "run_command", run)

    azure_infra.connect_cluster("spi-stack-shared", "spi-stack-shared")

    assert commands[0][:3] == ["az", "aks", "get-credentials"]
    assert commands[1] == ["kubelogin", "convert-kubeconfig", "-l", "azurecli"]
    assert commands[-1][:4] == ["kubectl", "config", "set-credentials", "cluster-user"]
    assert "--exec-env=AZURE_TENANT_ID=tenant-id" in commands[-1]


def _resolved_images() -> dict[str, ResolvedImage]:
    return {
        "storage": ResolvedImage(
            name="storage",
            repository="registry/storage",
            tag="a" * 40,
            created_at="2026-08-27T00:00:00Z",
            digest="sha256:storage",
        )
    }


def test_omitted_image_refresh_preserves_existing_lock(monkeypatch):
    config = Config.from_env("shared", profile=Profile.CORE)
    backfills = []
    monkeypatch.setattr(deploy, "read_lock", lambda required=False: {"data": {"existing": "lock"}})
    monkeypatch.setattr(
        deploy,
        "apply_schema_load_backfill",
        lambda branch: backfills.append(branch),
    )
    resolve = pytest.fail
    monkeypatch.setattr(
        deploy,
        "_resolve_image_lock",
        lambda _branch: resolve("registry lookup must not run"),
    )
    monkeypatch.setattr(
        deploy,
        "apply_image_lock",
        lambda *_args, **_kwargs: resolve("lock write must not run"),
    )

    assert deploy._ensure_image_lock(config, None, "master", {}) == {}
    assert backfills == ["master"]


def test_omitted_image_refresh_creates_missing_lock_under_the_source_policy(monkeypatch):
    config = Config.from_env("shared", profile=Profile.CORE)
    resolved = _resolved_images()
    calls, policies = [], []
    monkeypatch.setattr(deploy, "read_lock", lambda required=False: None)
    monkeypatch.setattr(
        deploy,
        "_resolve_image_lock",
        lambda branch, sources=None: policies.append(sources) or resolved,
    )
    monkeypatch.setattr(
        deploy,
        "apply_image_lock",
        lambda images, branch: calls.append((images, branch)) or {},
    )

    deploy._ensure_image_lock(config, None, "master", {}, {"partition": "Acme/partition"})

    assert calls == [(resolved, "master")]
    assert policies == [{"partition": "Acme/partition"}]


def test_up_resolves_under_the_source_policy_before_provisioning(monkeypatch):
    config = Config.from_env("shared", profile=Profile.CORE)
    events = []

    class Provisioned(Exception):
        pass

    def provision(*args, **kwargs):
        events.append("provision")
        raise Provisioned

    monkeypatch.setattr(deploy, "_source_policy", lambda cfg: {"partition": "Acme/partition"})
    monkeypatch.setattr(
        deploy,
        "_resolve_image_lock",
        lambda branch, sources=None: events.append(("resolve", sources)) or {},
    )
    monkeypatch.setattr(deploy, "provision_azure_infra", provision)

    with pytest.raises(Provisioned):
        deploy.deploy_azure(config, refresh_images=True)

    assert events == [("resolve", {"partition": "Acme/partition"}), "provision"]


def test_bootstrap_projects_the_source_policy_beside_the_roster(monkeypatch):
    from spi import onboard

    config = Config.from_env("shared", profile=Profile.CORE)
    synced = []
    monkeypatch.setattr(
        onboard, "sync_projection_from_identity", lambda identity, rg: synced.append("roster") or {}
    )
    monkeypatch.setattr(onboard, "sync_sources_from_tags", lambda rg: synced.append(rg) or {})

    deploy._project_trusted_repos(config)

    assert synced == ["roster", config.resource_group]


def test_explicit_no_refresh_fails_when_core_lock_is_missing(monkeypatch):
    config = Config.from_env("shared", profile=Profile.CORE)
    monkeypatch.setattr(deploy, "read_lock", lambda required=False: None)

    with pytest.raises(RuntimeError, match="--no-refresh-images"):
        deploy._ensure_image_lock(config, False, "master", {})


def test_explicit_no_refresh_preserves_existing_core_lock(monkeypatch):
    config = Config.from_env("shared", profile=Profile.CORE)
    backfills = []
    monkeypatch.setattr(deploy, "read_lock", lambda required=False: {"data": {}})
    monkeypatch.setattr(
        deploy,
        "apply_schema_load_backfill",
        lambda branch: backfills.append(branch),
    )
    monkeypatch.setattr(
        deploy,
        "apply_image_lock",
        lambda *_args, **_kwargs: pytest.fail("lock write must not run"),
    )

    assert deploy._ensure_image_lock(config, False, "master", {}) == {}
    assert backfills == ["master"]


def test_explicit_refresh_uses_pre_resolved_images(monkeypatch):
    config = Config.from_env("shared", profile=Profile.CORE)
    resolved = _resolved_images()
    calls = []
    monkeypatch.setattr(
        deploy,
        "read_lock",
        lambda required=False: pytest.fail("explicit refresh must use the CAS helper directly"),
    )
    monkeypatch.setattr(
        deploy,
        "apply_image_lock",
        lambda images, branch: calls.append((images, branch)) or {},
    )

    deploy._ensure_image_lock(config, True, "master", resolved)

    assert calls == [(resolved, "master")]


def test_non_core_profile_never_reads_or_writes_image_lock(monkeypatch):
    config = Config.from_env("shared", profile=Profile.MINIMAL)
    monkeypatch.setattr(
        deploy,
        "read_lock",
        lambda required=False: pytest.fail("non-core deploy must not read the lock"),
    )
    monkeypatch.setattr(
        deploy,
        "apply_image_lock",
        lambda *_args, **_kwargs: pytest.fail("non-core deploy must not write the lock"),
    )

    assert deploy._ensure_image_lock(config, None, "master", {}) == {}


DECLARATION = """\
env: shared
stackVersion: v0.24.0
profile: core
location: westus3
ingressMode: azure
imageBranch: master
nameSuffix: a43c7
forks:
  - service: partition
    repo: Acme/osdu-spi-partition
    canonicalSource: fork
"""
LOCATOR = "Acme/ops:environments/shared.yaml"


def _flat(output: str) -> str:
    """CLI output with the error box and its wrapping removed, whatever the terminal width."""
    return " ".join(output.replace("│", " ").split())


def _declared():
    from spi.environment import Declared, parse_declaration, parse_locator

    return Declared(parse_locator(LOCATOR), parse_declaration(DECLARATION))


class TestUpDeclaration:
    @pytest.fixture
    def up(self, monkeypatch):
        """Run `spi up` as the v0.24.0 wheel, capturing what reaches the deploy."""
        seen = {}
        monkeypatch.setattr(cli, "_up_declaration", lambda env, option: _declared())
        monkeypatch.setattr(cli, "__version__", "0.24.0")
        monkeypatch.setattr(cli, "check_prerequisites", lambda tools: None)
        monkeypatch.setattr(
            cli,
            "_resolve_up_context",
            lambda env, requested_suffix: (requested_suffix, {}, ("", "")),
        )
        monkeypatch.setattr(cli, "_show_config", lambda config: None)
        monkeypatch.setattr(cli, "_show_next_steps", lambda config: None)

        def deploy_azure(config, **kwargs):
            seen.update(config=config, **kwargs)

        monkeypatch.setattr(deploy, "deploy_azure", deploy_azure)
        return lambda *args: (CliRunner().invoke(cli.app, ["up", "--env", "shared", *args]), seen)

    def test_the_declaration_supplies_the_options_and_is_recorded(self, up):
        result, seen = up()

        assert result.exit_code == 0, result.output
        config = seen["config"]
        assert (config.repo_tag, config.location, config.name_suffix) == (
            "v0.24.0",
            "westus3",
            "a43c7",
        )
        assert config.declaration_locator == LOCATOR
        assert seen["image_branch"] == "master"
        assert seen["declared"].declaration.forks[0].service == "partition"

    def test_a_declared_release_needs_its_own_wheel(self, up, monkeypatch):
        monkeypatch.setattr(cli, "__version__", "0.23.1")

        result, seen = up()

        assert result.exit_code == 2
        assert "requires spi 0.24.0, but this process is spi 0.23.1" in _flat(result.output)
        assert seen == {}

    def test_a_dry_run_does_not_hand_the_environment_to_the_declaration(self, up):
        result, seen = up("--dry-run")

        assert result.exit_code == 0, result.output
        assert seen["config"].declaration_locator == ""

    def test_an_option_repeating_the_declared_value_is_accepted(self, up):
        result, _ = up("--tag", "v0.24.0", "--profile", "core", "--location", "westus3")

        assert result.exit_code == 0, result.output

    @pytest.mark.parametrize(
        ("option", "value", "declared"),
        [
            ("--location", "eastus2", "westus3"),
            ("--profile", "minimal", "core"),
            ("--image-branch", "release", "master"),
            ("--name-suffix", "zzzzz", "a43c7"),
            ("--ingress-mode", "dns", "azure"),
            ("--branch", "main", "v0.24.0"),
        ],
    )
    def test_an_option_that_disagrees_with_the_declaration_is_refused(
        self, up, option, value, declared
    ):
        result, seen = up(option, value)

        assert result.exit_code == 2
        assert (
            f"conflicts with the declaration, which {'pins' if option == '--branch' else 'sets'} {declared}"
            in _flat(result.output)
        )
        assert seen == {}


def test_up_checks_an_explicit_tag_before_its_first_azure_read(_undeclared_up, monkeypatch):
    from spi import onboard

    monkeypatch.setattr(
        onboard, "read_group_tags", lambda *a, **k: pytest.fail("read Azure before the arguments")
    )
    monkeypatch.setattr(cli, "_up_declaration", _undeclared_up)

    result = CliRunner().invoke(cli.app, ["up", "--env", "shared", "--tag", "nonsense"])

    assert result.exit_code == 2
    assert "must match vX.Y.Z" in result.output


class TestUpDeclarationLookup:
    @pytest.fixture
    def lookup(self, _undeclared_up, monkeypatch):
        from spi import environment, onboard

        tags = {}
        monkeypatch.setattr(onboard, "read_group_tags", lambda rg, **_: dict(tags))
        monkeypatch.setattr(
            environment, "github_file", lambda repo, path, ref: DECLARATION.encode()
        )
        return _undeclared_up, tags

    def test_no_option_and_no_retained_locator_is_an_undeclared_stack(self, lookup):
        find, _ = lookup

        assert find("shared", None) is None

    def test_a_retained_locator_is_reused_when_the_option_is_omitted(self, lookup):
        find, tags = lookup
        tags["spi-environment-declaration"] = LOCATOR

        assert str(find("shared", None).locator) == LOCATOR
        assert str(find("shared", "acme/OPS:environments/shared.yaml").locator) == (
            "acme/OPS:environments/shared.yaml"
        )

    def test_an_option_naming_another_file_than_the_retained_one_is_refused(self, lookup):
        find, tags = lookup
        tags["spi-environment-declaration"] = "Acme/ops:environments/other/shared.yaml"

        with pytest.raises(
            typer.BadParameter, match="already declared by Acme/ops:environments/other"
        ):
            find("shared", LOCATOR)

    def test_a_declaration_for_another_environment_is_refused(self, lookup):
        find, _ = lookup

        with pytest.raises(typer.BadParameter, match="declares env 'shared'"):
            find("dev1", "Acme/ops:environments/shared.yaml")

    def test_an_unloadable_retained_declaration_stops_the_run(self, lookup, monkeypatch):
        from spi import environment
        from spi.images import ImageResolutionError

        find, tags = lookup
        tags["spi-environment-declaration"] = LOCATOR

        def gone(*args):
            raise ImageResolutionError("HTTP 404")

        monkeypatch.setattr(environment, "github_file", gone)

        with pytest.raises(typer.Exit) as exc:
            find("shared", None)
        assert exc.value.exit_code == 1


class TestDeclaredDeploy:
    @pytest.fixture
    def flow(self, monkeypatch):
        """deploy_azure up to provisioning, with the reconciliation recorded."""
        from spi import declared as declared_module

        events = []

        class Provisioned(Exception):
            pass

        def build(target, owner, *, lock, missing_ok):
            events.append(("build", target.identity_name, lock, missing_ok))
            return SimpleNamespace(
                blocked=list(self.blocked),
                sources={"partition": "Acme/osdu-spi-partition"},
                actions=[],
            )

        def provision(*args, **kwargs):
            events.append("provision")
            raise Provisioned

        self.blocked = []
        monkeypatch.setattr(declared_module, "build", build)
        monkeypatch.setattr(
            deploy, "_source_policy", lambda cfg: pytest.fail("retained tags must not be read")
        )
        monkeypatch.setattr(
            deploy,
            "_resolve_image_lock",
            lambda branch, sources=None: events.append(("resolve", sources)) or {},
        )
        monkeypatch.setattr(deploy, "provision_azure_infra", provision)
        return events, Provisioned

    def test_images_resolve_from_the_declared_sources_before_provisioning(self, flow):
        events, provisioned = flow
        config = Config.from_env("shared", profile=Profile.CORE)

        with pytest.raises(provisioned):
            deploy.deploy_azure(config, refresh_images=True, declared=_declared())

        assert events == [
            ("build", "spi-stack-shared-deployer", False, True),
            ("resolve", {"partition": "Acme/osdu-spi-partition"}),
            "provision",
        ]

    def test_a_fork_that_cannot_be_trusted_stops_the_run_before_provisioning(self, flow):
        events, _ = flow
        self.blocked = ["partition from Acme/osdu-spi-partition: no spi-stack environment"]
        config = Config.from_env("shared", profile=Profile.CORE)

        with pytest.raises(RuntimeError, match="cannot be reconciled: partition from"):
            deploy.deploy_azure(config, refresh_images=True, declared=_declared())

        assert "provision" not in events


def test_bootstrap_reconciles_to_the_declaration_before_projecting_into_the_lock(monkeypatch):
    from spi import declared as declared_module
    from spi import onboard

    config = Config.from_env("shared", profile=Profile.CORE)
    events = []
    result = SimpleNamespace(blocked=[], sources={}, actions=[])
    monkeypatch.setattr(
        declared_module,
        "build",
        lambda target, owner, *, lock, missing_ok: (
            events.append(("build", lock, missing_ok)) or result
        ),
    )
    monkeypatch.setattr(declared_module, "apply", lambda found: events.append("apply"))
    monkeypatch.setattr(
        onboard, "sync_projection_from_identity", lambda identity, rg: events.append("roster") or {}
    )
    monkeypatch.setattr(
        onboard, "sync_sources_from_tags", lambda rg: events.append("sources") or {}
    )

    deploy._project_trusted_repos(config, _declared())

    assert events == [("build", False, False), "apply", "roster", "sources"]


class TestDeclarationTag:
    @pytest.fixture
    def az(self, monkeypatch):
        calls = []
        state = {"exists": "false", "tag": "None"}

        def run(argv, **_):
            calls.append(argv)
            if argv[:3] == ["az", "group", "exists"]:
                return SimpleNamespace(returncode=0, stdout=state["exists"], stderr="")
            if argv[:3] == ["az", "group", "show"]:
                value = "abc12" if "spi-name-suffix" in argv[6] else state["tag"]
                return SimpleNamespace(returncode=0, stdout=value, stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(azure_infra, "run_command", run)
        return calls, state

    def _config(self):
        config = Config.from_env("shared", name_suffix="abc12")
        config.declaration_locator = LOCATOR
        return config

    def test_a_new_group_is_created_carrying_the_locator(self, az):
        calls, _ = az

        azure_infra.create_resource_group(self._config())

        create = next(argv for argv in calls if argv[:3] == ["az", "group", "create"])
        tags = create[create.index("--tags") + 1 : create.index("--output")]
        assert tags == ["spi-name-suffix=abc12", f"spi-environment-declaration={LOCATOR}"]

    @pytest.mark.parametrize(("retained", "written"), [("None", True), (LOCATOR, False)])
    def test_an_existing_group_gains_the_locator_once(self, az, retained, written):
        calls, state = az
        state.update(exists="true", tag=retained)

        azure_infra.create_resource_group(self._config())

        updates = [
            argv[argv.index("--set") + 1] for argv in calls if argv[:3] == ["az", "group", "update"]
        ]
        assert updates == ([f"tags.spi-environment-declaration={LOCATOR}"] if written else [])
