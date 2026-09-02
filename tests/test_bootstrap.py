# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Istio revision detection and the Flux substitution ConfigMap it feeds.

`spi-namespaces` substitutes `${ISTIO_REVISION}` from `spi-cluster-config`
with `optional: false`, so a wrong revision silently disables sidecar
injection and a missing ConfigMap stalls every layer above namespaces.
Both the detection and the paths that write the ConfigMap are covered here.
"""

from types import SimpleNamespace
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from spi import cli
from spi.bootstrap import (
    ISTIO_REVISION_CONFIGMAP,
    ISTIO_REVISION_KEY,
    ISTIO_REVISION_NAMESPACE,
    _detect_istio_revision,
    create_istio_revision_configmap,
    ensure_namespaces,
    render_istio_revision_configmap,
)
from spi.images import ResolvedImage
from spi.status import StatusError


def _deploy_list(*names: str) -> dict:
    return {"items": [{"metadata": {"name": name}} for name in names]}


def test_render_istio_revision_configmap():
    yaml = render_istio_revision_configmap("asm-1-30")

    assert f"name: {ISTIO_REVISION_CONFIGMAP}" in yaml
    assert f"namespace: {ISTIO_REVISION_NAMESPACE}" in yaml
    assert f'{ISTIO_REVISION_KEY}: "asm-1-30"' in yaml


class TestDetectIstioRevision:
    def test_reads_revision_from_istiod_deployment_name(self):
        with patch(
            "spi.bootstrap.kubectl_json",
            return_value=_deploy_list("istio-ingressgateway-asm-1-31", "istiod-asm-1-31"),
        ):
            assert _detect_istio_revision() == "asm-1-31"

    def test_returns_none_when_no_istiod_deployment(self):
        with patch("spi.bootstrap.kubectl_json", return_value=_deploy_list("aks-istio-egress")):
            assert _detect_istio_revision() is None

    def test_returns_none_when_cluster_query_fails(self):
        with patch("spi.bootstrap.kubectl_json", return_value=None):
            assert _detect_istio_revision() is None


class TestEnsureNamespaces:
    def test_labels_osdu_with_detected_revision_and_returns_it(self):
        with (
            patch("spi.bootstrap.kubectl_json", return_value=_deploy_list("istiod-asm-1-31")),
            patch("spi.bootstrap.run_process"),
            patch("spi.bootstrap.kubectl_apply_yaml") as apply_yaml,
        ):
            revision = ensure_namespaces()

        assert revision == "asm-1-31"
        assert "istio.io/rev: asm-1-31" in apply_yaml.call_args.args[0]

    def test_explicit_revision_skips_detection(self):
        with (
            patch("spi.bootstrap.kubectl_json") as kubectl_json,
            patch("spi.bootstrap.run_process"),
            patch("spi.bootstrap.kubectl_apply_yaml") as apply_yaml,
        ):
            revision = ensure_namespaces("asm-1-29")

        kubectl_json.assert_not_called()
        assert revision == "asm-1-29"
        assert "istio.io/rev: asm-1-29" in apply_yaml.call_args.args[0]

    def test_falls_back_when_detection_fails(self):
        with (
            patch("spi.bootstrap.kubectl_json", return_value=None),
            patch("spi.bootstrap.run_process"),
            patch("spi.bootstrap.kubectl_apply_yaml") as apply_yaml,
        ):
            revision = ensure_namespaces()

        assert revision == "asm-1-30"
        assert "istio.io/rev: asm-1-30" in apply_yaml.call_args.args[0]


class TestCreateIstioRevisionConfigmap:
    def test_applies_detected_revision_when_called_without_argument(self):
        with (
            patch("spi.bootstrap.kubectl_json", return_value=_deploy_list("istiod-asm-1-31")),
            patch("spi.bootstrap.kubectl_apply_yaml") as apply_yaml,
        ):
            create_istio_revision_configmap()

        applied = apply_yaml.call_args.args[0]
        assert f"name: {ISTIO_REVISION_CONFIGMAP}" in applied
        assert f'{ISTIO_REVISION_KEY}: "asm-1-31"' in applied

    def test_applies_supplied_revision(self):
        with (
            patch("spi.bootstrap.kubectl_json") as kubectl_json,
            patch("spi.bootstrap.kubectl_apply_yaml") as apply_yaml,
        ):
            create_istio_revision_configmap("asm-1-29")

        kubectl_json.assert_not_called()
        assert f'{ISTIO_REVISION_KEY}: "asm-1-29"' in apply_yaml.call_args.args[0]

    def test_aborts_without_applying_when_detection_fails(self):
        with (
            patch("spi.bootstrap.kubectl_json", return_value=None),
            patch("spi.bootstrap.kubectl_apply_yaml") as apply_yaml,
        ):
            create_istio_revision_configmap()

        apply_yaml.assert_not_called()


class TestReconcileRefreshesClusterConfig:
    """`spi up` is not the only way a cluster reaches a new commit.

    Clusters bootstrapped by an older CLI, and clusters whose managed Istio
    revision moved since the last deploy, pull new commits through
    `spi reconcile`, so that command has to write the substitution source
    before Flux applies anything.
    """

    def _invoke(self, *args: str):
        runner = CliRunner()
        with (
            patch("spi.cli.verify_spi_cluster", return_value="spi-test"),
            patch("spi.cli.get_suspend_status", return_value=False),
            patch(
                "spi.cli.run_command",
                side_effect=lambda cmd_list, **kwargs: SimpleNamespace(
                    returncode=0, stdout="", stderr=""
                ),
            ),
            patch("spi.cli.create_istio_revision_configmap") as configmap,
            # Reads the live lock through pins.run_process, which the
            # spi.cli.run_command patch above does not intercept; without
            # this the default reconcile path shells out to real kubectl.
            patch("spi.cli.apply_schema_load_backfill", return_value=False),
            patch("spi.status.resettable_helmreleases", return_value=[]),
        ):
            result = runner.invoke(cli.app, ["reconcile", *args])
        assert result.exit_code == 0, result.output
        return configmap

    def test_default_reconcile_writes_configmap(self):
        self._invoke().assert_called_once_with()

    def test_resume_writes_configmap(self):
        self._invoke("--resume").assert_called_once_with()

    def test_suspend_leaves_configmap_alone(self):
        self._invoke("--suspend").assert_not_called()

    def test_refresh_images_exits_on_resolution_error(self):
        """A registry lookup failure has to abort before annotating anything,
        surfacing the error instead of reconciling against a stale lock."""
        runner = CliRunner()
        with (
            patch("spi.cli.verify_spi_cluster", return_value="spi-test"),
            patch("spi.cli.get_suspend_status", return_value=False),
            patch("spi.cli.create_istio_revision_configmap"),
            patch(
                "spi.cli.resolve_image_lock",
                side_effect=cli.ImageResolutionError("schema: registry repository not found"),
            ),
            patch("spi.cli.apply_image_lock") as apply_lock,
            patch("spi.cli.run_command") as run_command,
        ):
            result = runner.invoke(cli.app, ["reconcile", "--refresh-images"])

        assert result.exit_code == 1
        assert "Unable to resolve OSDU service images" in result.output
        apply_lock.assert_not_called()
        run_command.assert_not_called()

    def test_refresh_images_aborts_when_pin_state_unreadable(self):
        """An unreadable pin state must abort the refresh before the lock is
        overwritten: treating it as "no pins" could revert an active pin."""
        from spi.pins import PinError

        runner = CliRunner()
        resolved = {
            "schema": ResolvedImage(
                name="schema",
                repository="community.opengroup.org:5555/osdu/schema-service-master",
                tag="1" * 40,
                created_at="2026-05-22T00:00:00+00:00",
                digest="sha256:schema",
            )
        }
        with (
            patch("spi.cli.verify_spi_cluster", return_value="spi-test"),
            patch("spi.cli.get_suspend_status", return_value=False),
            patch("spi.cli.create_istio_revision_configmap"),
            patch("spi.cli.resolve_image_lock", return_value=resolved),
            patch("spi.cli.apply_image_lock", side_effect=PinError("could not read lock")),
            patch("spi.cli.run_command") as run_command,
        ):
            result = runner.invoke(cli.app, ["reconcile", "--refresh-images"])

        assert result.exit_code == 1
        assert "Refusing to refresh" in result.output
        run_command.assert_not_called()

    def test_refresh_images_reconciles_schema_load_before_reference(self):
        runner = CliRunner()
        resolved = {
            "schema": ResolvedImage(
                name="schema",
                repository="community.opengroup.org:5555/osdu/schema-service-master",
                tag="1" * 40,
                created_at="2026-05-22T00:00:00+00:00",
                digest="sha256:schema",
            )
        }

        def _run_command(cmd_list, **kwargs):
            if cmd_list[:3] == ["kubectl", "get", "kustomization"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"kustomization.kustomize.toolkit.fluxcd.io/{cmd_list[3]}\n",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("spi.cli.verify_spi_cluster", return_value="spi-test"),
            patch("spi.cli.get_suspend_status", return_value=False),
            patch("spi.cli.create_istio_revision_configmap"),
            patch("spi.cli.resolve_image_lock", return_value=resolved),
            patch("spi.cli.apply_image_lock", return_value={}),
            patch("spi.cli.run_command", side_effect=_run_command) as run_command,
            patch("spi.status.resettable_helmreleases", return_value=[]),
        ):
            result = runner.invoke(cli.app, ["reconcile", "--refresh-images"])

        assert result.exit_code == 0, result.output
        reconciled = []
        for call in run_command.call_args_list:
            args = call.args[0]
            if args[:3] == ["flux", "reconcile", "kustomization"]:
                reconciled.append(args[3])
        assert reconciled == [
            "spi-osdu-services",
            "spi-osdu-schema-load",
            "spi-osdu-reference",
        ]

    def test_refresh_images_skips_missing_kustomizations(self):
        """A minimal/bare profile has none of the core Layer 5 Kustomizations;
        --refresh-images has to skip them instead of failing the whole
        reconcile when `flux reconcile` can't find one.
        """
        runner = CliRunner()
        resolved = {
            "schema": ResolvedImage(
                name="schema",
                repository="community.opengroup.org:5555/osdu/schema-service-master",
                tag="1" * 40,
                created_at="2026-05-22T00:00:00+00:00",
                digest="sha256:schema",
            )
        }

        def _run_command(cmd_list, **kwargs):
            if cmd_list[:3] == ["kubectl", "get", "kustomization"]:
                # --ignore-not-found: absence is an empty result, not a failure.
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("spi.cli.verify_spi_cluster", return_value="spi-test"),
            patch("spi.cli.get_suspend_status", return_value=False),
            patch("spi.cli.create_istio_revision_configmap"),
            patch("spi.cli.resolve_image_lock", return_value=resolved),
            patch("spi.cli.apply_image_lock", return_value={}),
            patch("spi.cli.run_command", side_effect=_run_command) as run_command,
            patch("spi.status.resettable_helmreleases", return_value=[]),
        ):
            result = runner.invoke(cli.app, ["reconcile", "--refresh-images"])

        assert result.exit_code == 0, result.output
        reconciled = [
            call.args[0][3]
            for call in run_command.call_args_list
            if call.args[0][:3] == ["flux", "reconcile", "kustomization"]
        ]
        assert reconciled == []

    def test_kustomization_probe_only_tolerates_genuine_absence(self):
        """An authorization error or API timeout must not read as "absent" and
        silently skip the dependent reconciliations, so the probe runs checked
        and relies on --ignore-not-found for the absence case."""
        with patch(
            "spi.cli.run_command",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ) as run_command:
            assert cli._kustomization_exists("spi-osdu-services") is False

        cmd_list = run_command.call_args.args[0]
        assert "--ignore-not-found" in cmd_list
        assert cmd_list[-2:] == ["-o", "name"]
        assert run_command.call_args.kwargs.get("check", True) is True

    def test_default_reconcile_does_not_block_on_missing_kustomizations(self):
        """A plain `spi reconcile` (no --refresh-images) has to keep tolerating
        absent core Kustomizations on minimal/bare clusters, same as before
        the ordered-wait behavior was introduced for image refreshes.
        """
        runner = CliRunner()

        def _run_command(cmd_list, **kwargs):
            if cmd_list[:3] == ["flux", "reconcile", "kustomization"]:
                raise AssertionError("default reconcile must not block on flux reconcile")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("spi.cli.verify_spi_cluster", return_value="spi-test"),
            patch("spi.cli.get_suspend_status", return_value=False),
            patch("spi.cli.create_istio_revision_configmap"),
            patch("spi.cli.run_command", side_effect=_run_command),
            patch("spi.cli.apply_schema_load_backfill", return_value=False),
            patch("spi.status.resettable_helmreleases", return_value=[]),
        ):
            result = runner.invoke(cli.app, ["reconcile"])

        assert result.exit_code == 0, result.output


class TestReconcileResetsStalledHelmReleases:
    """helm-controller marks a release Stalled once install retries are
    exhausted and never retries it, even after the cause is fixed. A
    re-applied, unchanged HelmRelease manifest does not reset it either, so
    reconcile has to annotate the release itself.
    """

    def _invoke(self, stalled):
        runner = CliRunner()
        events = []

        def _run_command(cmd_list, **kwargs):
            events.append(cmd_list)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("spi.cli.verify_spi_cluster", return_value="spi-test"),
            patch("spi.cli.get_suspend_status", return_value=False),
            patch("spi.cli.create_istio_revision_configmap"),
            patch("spi.cli.run_command", side_effect=_run_command),
            patch("spi.cli.apply_schema_load_backfill", return_value=False),
            patch("spi.status.resettable_helmreleases", return_value=stalled),
        ):
            result = runner.invoke(cli.app, ["reconcile"])

        assert result.exit_code == 0, result.output
        resets = [
            cmd
            for cmd in events
            if cmd[:2] == ["kubectl", "annotate"] and cmd[3].startswith("helmrelease/")
        ]
        return result, resets

    def test_stalled_release_gets_reset_and_force_annotations(self):
        result, resets = self._invoke([("osdu-flux", "partition")])

        assert [cmd[3] for cmd in resets] == ["helmrelease/partition"]
        assert resets[0][4:6] == ["-n", "osdu-flux"]
        pairs = dict(arg.split("=", 1) for arg in resets[0] if "=" in arg)
        assert set(pairs) == {
            "reconcile.fluxcd.io/requestedAt",
            "reconcile.fluxcd.io/resetAt",
            "reconcile.fluxcd.io/forceAt",
        }
        # helm-controller ignores resetAt and forceAt unless they match requestedAt.
        assert len(set(pairs.values())) == 1
        assert "Resetting HelmReleases that exhausted retries: partition" in result.output

    def test_healthy_releases_are_left_alone(self):
        result, resets = self._invoke([])

        assert resets == []
        assert "Resetting HelmReleases" not in result.output

    def test_unreadable_helmreleases_abort_the_reconcile(self):
        """A read that fails for any reason other than an absent type must not
        read as "nothing stalled": reconcile would then report success while
        leaving the releases it exists to recover still stalled.
        """
        runner = CliRunner()
        with (
            patch("spi.cli.verify_spi_cluster", return_value="spi-test"),
            patch("spi.cli.get_suspend_status", return_value=False),
            patch("spi.cli.create_istio_revision_configmap"),
            patch("spi.cli.run_command", return_value=SimpleNamespace(returncode=0, stdout="")),
            patch("spi.cli.apply_schema_load_backfill", return_value=False),
            patch(
                "spi.status.resettable_helmreleases",
                side_effect=StatusError("Could not read Flux HelmReleases: connection refused"),
            ),
        ):
            result = runner.invoke(cli.app, ["reconcile"])

        assert result.exit_code == 1
        assert "Could not check for stalled HelmReleases" in result.output
        assert "connection refused" in result.output

    def test_failed_reset_annotation_aborts(self):
        """The annotation is the recovery itself, so an RBAC denial or API
        error has to stop the run rather than be reported as a reset.
        """
        runner = CliRunner()

        def _run_command(cmd_list, **kwargs):
            if cmd_list[:2] == ["kubectl", "annotate"] and cmd_list[3].startswith("helmrelease/"):
                raise typer.Exit(code=1)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("spi.cli.verify_spi_cluster", return_value="spi-test"),
            patch("spi.cli.get_suspend_status", return_value=False),
            patch("spi.cli.create_istio_revision_configmap"),
            patch("spi.cli.run_command", side_effect=_run_command),
            patch("spi.cli.apply_schema_load_backfill", return_value=False),
            patch("spi.status.resettable_helmreleases", return_value=[("osdu-flux", "partition")]),
        ):
            result = runner.invoke(cli.app, ["reconcile"])

        assert result.exit_code == 1


class TestSchemaLoadImageLockBackfill:
    """The schema-load Job substitutes its image from `osdu-image-lock` with no
    static fallback. A cluster whose lock predates schema-load's
    inclusion would render an unresolved image, so `spi reconcile` backfills
    the loader entries before Flux applies the manifest.
    """

    def _invoke(self, changed: bool, error: Exception | None = None, *args: str):
        runner = CliRunner()
        events = []

        def _run_command(cmd_list, **kwargs):
            events.append(cmd_list)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def _backfill(*, branch):
            events.append(("backfill", branch))
            if error:
                raise error
            return changed

        with (
            patch("spi.cli.verify_spi_cluster", return_value="spi-test"),
            patch("spi.cli.get_suspend_status", return_value=False),
            patch("spi.cli.create_istio_revision_configmap"),
            patch("spi.cli.apply_schema_load_backfill", side_effect=_backfill) as backfill,
            patch("spi.cli.run_command", side_effect=_run_command) as run_command,
            patch("spi.status.resettable_helmreleases", return_value=[]),
        ):
            result = runner.invoke(cli.app, ["reconcile", *args])

        return result, backfill, run_command, events

    def test_legacy_lock_is_backfilled(self):
        result, backfill, _, _ = self._invoke(True)

        assert result.exit_code == 0, result.output
        backfill.assert_called_once_with(branch="master")
        assert "updated with schema-load" in result.output

    def test_current_lock_is_left_alone(self):
        result, backfill, _, _ = self._invoke(False)

        assert result.exit_code == 0, result.output
        backfill.assert_called_once_with(branch="master")
        assert "updated with schema-load" not in result.output

    def test_absent_lock_is_skipped(self):
        """minimal/bare profiles never create the lock."""
        result, backfill, _, _ = self._invoke(False)

        assert result.exit_code == 0, result.output
        backfill.assert_called_once_with(branch="master")

    def test_resolution_failure_aborts_before_reconcile(self):
        result, backfill, run_command, _ = self._invoke(
            False,
            cli.ImageResolutionError("schema-load: tag not found"),
        )

        assert result.exit_code == 1
        assert "Unable to backfill" in result.output
        backfill.assert_called_once_with(branch="master")
        run_command.assert_not_called()

    def test_resume_backfills_before_unsuspending_source(self):
        result, backfill, _, events = self._invoke(True, None, "--resume")

        assert result.exit_code == 0, result.output
        backfill.assert_called_once_with(branch="master")
        backfill_call = events.index(("backfill", "master"))
        gitrepository_patch = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, list) and event[:3] == ["kubectl", "patch", "gitrepository"]
        )
        assert backfill_call < gitrepository_patch
