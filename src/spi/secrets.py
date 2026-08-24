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

"""Secret management for SPI Stack.

SPI Stack only needs secrets for the in-cluster middleware:
  - Elasticsearch (elastic user)
  - Redis (default user)
  - PostgreSQL (Airflow metadata database)
  - Airflow (admin password plus api-secret/JWT/fernet signing keys;
    the fernet key is format-sensitive: urlsafe-base64, 32 bytes)

Azure PaaS services use Workload Identity (no stored secrets).
"""

import base64
import json
import secrets
import string

import typer

from .console import console
from .shell import kubectl_apply_yaml, run_process

SEED_NAME = "spi-secrets"
SEED_NAMESPACE = "osdu-flux"

SEED_KEYS = [
    "elastic_password",
    "redis_password",
    "pg_admin_password",
    "pg_airflow_password",
    "airflow_admin_password",
    "airflow_api_secret_key",
    "airflow_jwt_secret",
    "airflow_fernet_key",
]


def _generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _generate_fernet_key() -> str:
    # Fernet requires a urlsafe-base64-encoded 32-byte key, not a password.
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def _kubectl_apply_secret(namespace: str, name: str, literals: dict):
    cmd = [
        "kubectl",
        "create",
        "secret",
        "generic",
        name,
        "-n",
        namespace,
        "--dry-run=client",
        "-o",
        "yaml",
    ]
    for k, v in literals.items():
        cmd.append(f"--from-literal={k}={v}")

    create = run_process(cmd, capture_output=True, text=True)
    if create.returncode != 0:
        console.print(f"  [error]Failed to generate secret {namespace}/{name}[/error]")
        raise typer.Exit(code=1)

    kubectl_apply_yaml(create.stdout, f"apply secret {namespace}/{name}")


def _get_seed() -> dict | None:
    result = run_process(
        ["kubectl", "get", "secret", SEED_NAME, "-n", SEED_NAMESPACE, "-o", "jsonpath={.data}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None

    try:
        raw = json.loads(result.stdout)
        return {k: base64.b64decode(v).decode() for k, v in raw.items()}
    except Exception:
        return None


def _create_seed(seed: dict):
    _kubectl_apply_secret(SEED_NAMESPACE, SEED_NAME, seed)


def get_or_create_seed() -> dict:
    existing = _get_seed()
    if existing and all(k in existing for k in SEED_KEYS):
        console.print(f"  [info]Seed secret '{SEED_NAME}' exists, reusing seed values[/info]")
        return existing

    seed = dict(existing) if existing else {}
    for k in SEED_KEYS:
        if k not in seed:
            if k == "airflow_fernet_key":
                seed[k] = _generate_fernet_key()
            else:
                seed[k] = _generate_password()

    if existing:
        console.print("  [info]Seed secret updated with new keys[/info]")
    else:
        console.print("  [info]Generating new seed secrets...[/info]")
    _create_seed(seed)
    return seed


def _create_platform_secrets(s: dict):
    """Create infrastructure secrets in the platform namespace."""
    # PostgreSQL (Airflow metadata)
    _kubectl_apply_secret(
        "platform",
        "postgresql-superuser-credentials",
        {
            "username": "postgres",
            "password": s["pg_admin_password"],
        },
    )
    _kubectl_apply_secret(
        "platform",
        "postgresql-airflow-credentials",
        {
            "username": "airflow",
            "password": s["pg_airflow_password"],
        },
    )

    # Elasticsearch
    _kubectl_apply_secret(
        "platform",
        "elasticsearch-es-elastic-user",
        {
            "elastic": s["elastic_password"],
        },
    )

    # Redis
    _kubectl_apply_secret(
        "platform",
        "redis-credentials",
        {
            "password": s["redis_password"],
        },
    )

    # Airflow metadata connection
    pg_host = "postgresql-rw.platform.svc.cluster.local"
    _kubectl_apply_secret(
        "platform",
        "airflow-metadata-secret",
        {
            # Bare postgresql:// on purpose: SQLAlchemy already defaults to
            # psycopg2, and the chart feeds this same string to the built-in
            # airflow_db Connection, where a +driver suffix is an unknown
            # conn_type.
            "connection": f"postgresql://airflow:{s['pg_airflow_password']}@{pg_host}:5432/airflow",
        },
    )
    # Airflow signing material. The key names are fixed by the chart:
    # api-secret-key (apiSecretKeySecretName), jwt-secret (jwtSecretName),
    # fernet-key (fernetKeySecretName). 'password' is the admin UI/API user.
    _kubectl_apply_secret(
        "platform",
        "airflow-api-credentials",
        {
            "password": s["airflow_admin_password"],
            "api-secret-key": s["airflow_api_secret_key"],
            "jwt-secret": s["airflow_jwt_secret"],
            "fernet-key": s["airflow_fernet_key"],
        },
    )


def _create_osdu_secrets(s: dict):
    """Create secrets needed by OSDU services in the osdu namespace.

    Most OSDU services use Workload Identity for Azure PaaS access.
    Only Elasticsearch and Redis credentials need to be in K8s secrets.
    """
    # Elasticsearch CA cert will be copied cross-namespace by the
    # middleware manifests. Just ensure the credential secrets exist.
    for svc in ["indexer", "search"]:
        _kubectl_apply_secret(
            "osdu",
            f"{svc}-elastic-secret",
            {
                "ELASTIC_USER_SYSTEM": "elastic",
                "ELASTIC_PASS_SYSTEM": s["elastic_password"],
            },
        )


def ensure_secrets():
    """Generate and apply all secrets. Idempotent."""
    console.print("\n[bold]Configuring secrets...[/bold]")
    seed = get_or_create_seed()
    _create_platform_secrets(seed)
    _create_osdu_secrets(seed)
    console.print("  [ready]All secrets configured[/ready]")
