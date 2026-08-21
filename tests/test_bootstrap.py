# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Istio revision detection and the Flux substitution ConfigMap it feeds.

`spi-namespaces` substitutes `${ISTIO_REVISION}` from `spi-cluster-config`
with `optional: false`, so a wrong revision silently disables sidecar
injection and a missing ConfigMap stalls every layer above namespaces.
Both the detection and the paths that write the ConfigMap are covered here.
"""

from unittest.mock import patch

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
            patch("spi.cli.run_command"),
            patch("spi.cli.create_istio_revision_configmap") as configmap,
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

        with (
            patch("spi.cli.verify_spi_cluster", return_value="spi-test"),
            patch("spi.cli.get_suspend_status", return_value=False),
            patch("spi.cli.create_istio_revision_configmap"),
            patch("spi.cli.resolve_image_lock", return_value=resolved),
            patch("spi.cli.render_image_lock_configmap", return_value="kind: ConfigMap\n"),
            patch("spi.cli.kubectl_apply_yaml"),
            patch("spi.cli.run_command") as run_command,
        ):
            result = runner.invoke(cli.app, ["reconcile", "--refresh-images"])

        assert result.exit_code == 0, result.output
        reconciled = []
        for call in run_command.call_args_list:
            args = call.args[0]
            if args[3].startswith("kustomization/"):
                reconciled.append(args[3].removeprefix("kustomization/"))
        assert "spi-osdu-schema-load" in reconciled
        assert reconciled.index("spi-osdu-schema-load") < reconciled.index("spi-osdu-reference")
