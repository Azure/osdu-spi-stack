# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

from spi.bootstrap import (
    ISTIO_REVISION_CONFIGMAP,
    ISTIO_REVISION_KEY,
    ISTIO_REVISION_NAMESPACE,
    render_istio_revision_configmap,
)


def test_render_istio_revision_configmap():
    yaml = render_istio_revision_configmap("asm-1-30")

    assert f"name: {ISTIO_REVISION_CONFIGMAP}" in yaml
    assert f"namespace: {ISTIO_REVISION_NAMESPACE}" in yaml
    assert f'{ISTIO_REVISION_KEY}: "asm-1-30"' in yaml
