# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

from types import SimpleNamespace

import pytest
import typer

from spi import secrets


def test_osdu_secrets_include_rafs_redis_password(monkeypatch):
    applied = []
    monkeypatch.setattr(
        secrets,
        "_kubectl_apply_secret",
        lambda namespace, name, literals: applied.append((namespace, name, literals)),
    )

    secrets._create_osdu_secrets(
        {
            "redis_password": "redis-secret",
            "elastic_password": "elastic-secret",
        }
    )

    assert (
        "osdu",
        "rafs-redis-secret",
        {"REDIS_PASSWORD": "redis-secret"},
    ) in applied


def test_reconcile_rafs_secret_reuses_environment_seed(monkeypatch):
    applied = []
    monkeypatch.setattr(
        secrets,
        "_get_seed",
        lambda: {"redis_password": "stable-redis-secret"},
    )
    monkeypatch.setattr(
        secrets,
        "_kubectl_apply_secret",
        lambda namespace, name, literals: applied.append((namespace, name, literals)),
    )

    secrets.ensure_rafs_redis_secret()

    assert applied == [
        (
            "osdu",
            "rafs-redis-secret",
            {"REDIS_PASSWORD": "stable-redis-secret"},
        )
    ]


def test_reconcile_rafs_secret_aborts_when_seed_read_fails(monkeypatch):
    applied = []
    monkeypatch.setattr(
        secrets,
        "run_process",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="forbidden"),
    )
    monkeypatch.setattr(
        secrets,
        "_kubectl_apply_secret",
        lambda *args: applied.append(args),
    )

    with pytest.raises(typer.Exit) as raised:
        secrets.ensure_rafs_redis_secret()

    assert raised.value.exit_code == 1
    assert applied == []
