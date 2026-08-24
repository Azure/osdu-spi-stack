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

"""Configuration models for SPI Stack."""

import re
import secrets
from enum import Enum
from typing import List

from pydantic import BaseModel, model_validator

# Partition names must be lowercase alphanumeric. Hyphens and underscores are
# stripped at Azure-resource-name time (`_storage_name` in azure_infra.py),
# so allowing them here would silently collide two configured partitions.
_PARTITION_NAME_RE = re.compile(r"^[a-z0-9]+$")

# Storage account naming is the binding constraint: `osdu{env}{partition}{suffix}`
# with hyphens stripped, max 24 chars. The 5-char suffix is randomly generated
# on the first `spi up` for a given environment and persisted as the
# `spi-name-suffix` tag on the resource group; subsequent runs read it back.
# The validator reserves the suffix budget unconditionally so partition
# choices that would overflow a future deployment fail at config-build time.
# Cosmos (44) and Service Bus (50) always fit when storage fits.
_STORAGE_NAME_PREFIX = "osdu"
_STORAGE_NAME_MAX_LEN = 24
_NAME_SUFFIX_LEN = 5

# Tag key on the resource group that carries the per-deployment suffix.
# An empty value marks a pre-suffix (legacy) deployment whose names must
# stay unsuffixed to keep matching the resources already in Azure.
RG_SUFFIX_TAG = "spi-name-suffix"


def generate_name_suffix() -> str:
    """Mint a fresh random suffix for a new deployment."""
    # token_hex returns 2 chars per byte; 3 bytes -> 6 hex chars, take 5.
    return secrets.token_hex(3)[:_NAME_SUFFIX_LEN]


class Profile(str, Enum):
    # Infra plus activated GitOps only. Flux reconciles empty stack and ingress
    # trees. Namespaces, secrets, ConfigMap, and ServiceAccount come from the CLI
    # bootstrap; no operators, cert/trust-manager, Gateway, middleware, or services.
    BARE = "bare"
    # Middleware only: operators, cert/trust-manager, Gateway, Elasticsearch,
    # Redis, PostgreSQL, Airflow. Stops before layer 5 (no OSDU services).
    MINIMAL = "minimal"
    # Middleware plus the OSDU services, bootstrap Jobs, schema load, and
    # reference services. Default.
    CORE = "core"


class IngressMode(str, Enum):
    # Auto-FQDN (<label>.<region>.cloudapp.azure.com) + Let's Encrypt TLS.
    # Default. Zero prerequisites.
    AZURE = "azure"
    # Real DNS zone + ExternalDNS + Let's Encrypt TLS. Zone auto-discovered
    # from the current subscription.
    DNS = "dns"
    # Bare IP, HTTP only, no TLS. Hidden fallback for air-gapped debug.
    IP = "ip"


BASE_NAME = "spi-stack"


class Config(BaseModel):
    profile: Profile = Profile.CORE
    env: str = ""
    repo_url: str = "https://github.com/Azure/osdu-spi-stack.git"
    repo_branch: str = "main"
    cluster_name: str = BASE_NAME
    # Azure
    resource_group: str = BASE_NAME
    location: str = "eastus2"
    # Random 5-char suffix used by globally unique resource names (storage,
    # KV, ACR, Cosmos, Service Bus). Persisted as the `spi-name-suffix` tag
    # on the resource group; an empty value marks a legacy (pre-suffix)
    # deployment whose names must stay unsuffixed.
    name_suffix: str = ""
    # Data partitions
    data_partitions: List[str] = ["opendes"]
    # Derived names (set in from_env)
    identity_name: str = ""
    external_dns_identity_name: str = ""
    keyvault_name: str = ""
    acr_name: str = ""
    # Ingress / DNS
    ingress_mode: IngressMode = IngressMode.AZURE
    dns_zone: str = ""  # dns mode: auto-discovered if empty
    dns_zone_rg: str = ""  # dns mode: derived from zone lookup
    ingress_prefix: str = ""  # defaults to env
    acme_email: str = ""  # defaults to admin@<fqdn>|<zone>
    ingress_fqdn: str = ""  # azure mode: resolved LB FQDN

    @staticmethod
    def from_env(env: str, name_suffix: str = "", **kwargs) -> "Config":
        """Create config with names derived from --env and a deployment suffix.

        name_suffix is the random 5-char value resolved from (or minted for)
        the resource group's `spi-name-suffix` tag. Pass "" to render legacy
        unsuffixed names — used both for legacy deployments and for tests
        that don't exercise the Azure plumbing.
        """
        cluster_name = f"{BASE_NAME}-{env}" if env else BASE_NAME
        resource_group = f"{BASE_NAME}-{env}" if env else BASE_NAME

        # Azure naming: alphanumeric only, 3-24 chars for KV, 5-50 for ACR
        safe_env = env.replace("-", "").replace("_", "")
        keyvault_name = f"osdu{safe_env}{name_suffix}"[:24] if env else "osduspistack"
        acr_name = f"osdu{safe_env}{name_suffix}"[:50] if env else "osduspistack"
        identity_name = f"{cluster_name}-osdu-identity"
        external_dns_identity_name = f"{cluster_name}-external-dns"

        return Config(
            env=env,
            name_suffix=name_suffix,
            cluster_name=cluster_name,
            resource_group=resource_group,
            identity_name=identity_name,
            external_dns_identity_name=external_dns_identity_name,
            keyvault_name=keyvault_name,
            acr_name=acr_name,
            **kwargs,
        )

    @property
    def env_flag(self) -> str:
        """Return the --env flag string for display in next-steps."""
        return f" --env {self.env}" if self.env else ""

    @property
    def primary_partition(self) -> str:
        """First data partition hosts the system database."""
        return self.data_partitions[0]

    @model_validator(mode="after")
    def _validate_data_partitions(self) -> "Config":
        partitions = self.data_partitions
        if not partitions:
            raise ValueError("data_partitions must contain at least one partition")

        duplicates = sorted({p for p in partitions if partitions.count(p) > 1})
        if duplicates:
            raise ValueError(f"data_partitions contains duplicate names: {duplicates}")

        sanitized_env = self.env.replace("-", "").replace("_", "")
        # Reserve the suffix budget even when subscription_id is unknown so
        # validation matches the names produced at deploy time.
        suffix_placeholder = "x" * _NAME_SUFFIX_LEN
        for p in partitions:
            if not _PARTITION_NAME_RE.fullmatch(p):
                raise ValueError(
                    f"partition name {p!r} must be lowercase alphanumeric "
                    f"(matches [a-z0-9]+); hyphens and underscores are stripped "
                    f"during Azure resource naming and would silently collide"
                )
            storage_name = f"{_STORAGE_NAME_PREFIX}{sanitized_env}{p}{suffix_placeholder}"
            if len(storage_name) > _STORAGE_NAME_MAX_LEN:
                raise ValueError(
                    f"partition name {p!r} produces storage account name "
                    f"{storage_name!r} (length {len(storage_name)}, includes a "
                    f"{_NAME_SUFFIX_LEN}-char per-subscription uniqueness "
                    f"suffix), exceeding the {_STORAGE_NAME_MAX_LEN}-char Azure "
                    f"limit. Shorten the env (currently {self.env!r}) or the "
                    f"partition name."
                )
        return self

    @model_validator(mode="after")
    def _validate_bare_profile(self) -> "Config":
        if self.profile is Profile.BARE:
            if self.ingress_mode is not IngressMode.AZURE:
                raise ValueError(
                    "profile 'bare' deploys no ingress substrate; "
                    f"ingress_mode '{self.ingress_mode.value}' is not supported"
                )
            if self.dns_zone:
                raise ValueError(
                    "profile 'bare' deploys no ingress substrate; "
                    "dns_zone is not supported with profile 'bare'"
                )
        return self

    @property
    def resolved_ingress_prefix(self) -> str:
        """DNS-mode hostname prefix. Falls back to env name, then 'spi'."""
        return self.ingress_prefix or self.env or "spi"

    @property
    def dns_label(self) -> str:
        """Azure-mode DNS label for the Istio ingress PIP.

        The '-ingress' suffix identifies the endpoint and reduces the chance
        of colliding with another globally unique Azure DNS label.
        """
        return f"{self.cluster_name}-ingress"
