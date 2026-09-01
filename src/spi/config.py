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

# Hyphens and underscores are stripped from Azure resource names, so allowing
# them here would silently collide two partitions.
_PARTITION_NAME_RE = re.compile(r"^[a-z0-9]+$")

# Storage account names (`osdu{env}{partition}{suffix}`, 24 chars) are the
# binding limit; Cosmos (44) and Service Bus (50) fit whenever storage fits.
_STORAGE_NAME_PREFIX = "osdu"
_STORAGE_NAME_MAX_LEN = 24
_NAME_SUFFIX_LEN = 5

# Resource group tag carrying the per-deployment suffix. An empty value marks
# a legacy deployment whose names stay unsuffixed.
RG_SUFFIX_TAG = "spi-name-suffix"


def generate_name_suffix() -> str:
    """Mint a fresh random suffix for a new deployment."""
    return secrets.token_hex(3)[:_NAME_SUFFIX_LEN]


class Profile(str, Enum):
    # Infra and the CLI bootstrap only; Flux reconciles empty trees.
    BARE = "bare"
    # Operators, Gateway, and middleware; no OSDU services.
    MINIMAL = "minimal"
    # Middleware plus OSDU services, bootstrap Jobs, schema load, and reference services.
    CORE = "core"


class IngressMode(str, Enum):
    # <label>.<region>.cloudapp.azure.com with Let's Encrypt TLS; no prerequisites.
    AZURE = "azure"
    # An Azure DNS zone in the subscription, ExternalDNS, Let's Encrypt TLS.
    DNS = "dns"
    # Bare IP over HTTP; hidden debug fallback.
    IP = "ip"


BASE_NAME = "spi-stack"


class Config(BaseModel):
    profile: Profile = Profile.CORE
    env: str = ""
    repo_url: str = "https://github.com/Azure/osdu-spi-stack.git"
    repo_branch: str = "main"
    repo_tag: str = ""
    cluster_name: str = BASE_NAME
    resource_group: str = BASE_NAME
    location: str = "eastus2"
    # Suffix on globally unique resource names, read back from RG_SUFFIX_TAG.
    name_suffix: str = ""
    data_partitions: List[str] = ["opendes"]
    identity_name: str = ""
    external_dns_identity_name: str = ""
    keyvault_name: str = ""
    acr_name: str = ""
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

        # Key Vault allows 24 alphanumeric characters, ACR 50.
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
        # Reserve the suffix budget so validation matches deploy-time names.
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

        Env plus the per-deployment name suffix: cloudapp labels are a
        region-global namespace, and an unsuffixed label can sit reserved by
        an unreachable resource (DnsRecordIsReserved), which no deploy-side
        retry can clear. A legacy deployment without a suffix keeps the old
        cluster-name label so its FQDN does not change.
        """
        if self.name_suffix:
            return f"{self.env or BASE_NAME}-ingress-{self.name_suffix}"
        return f"{self.cluster_name}-ingress"
