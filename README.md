# OSDU SPI Stack

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

### Azure-Native Software for OSDU

SPI Stack deploys the OSDU platform onto Azure using AKS Automatic 1.36 and
Azure PaaS services with a bootstrap + [Flux CD](https://fluxcd.io/) GitOps
model. The validated Base SKU + Node Autoprovisioning topology remains
available through an immutable deployment flag.

This project is currently optimized for Azure dev/test environments and is still evolving.

**Who this is for:**

- Developers who want a reproducible Azure-based OSDU environment
- Platform engineers evaluating OSDU with Azure PaaS services


## Why SPI Stack

- **Azure-native**: leverages CosmosDB, Service Bus, Storage, Key Vault, and Entra ID
- **AKS Automatic by default**: managed Istio, hosted system pools, Karpenter,
  and Deployment Safeguards
- **Base + NAP fallback**: `--aks-mode base` preserves the validated explicit
  cluster topology
- **GitOps-driven**: Flux continuously reconciles desired state after bootstrap
- **Transparent**: every `az` and `kubectl` command is shown before execution
- **Workload Identity**: no stored credentials; all Azure access via federated identity


## Install

The only tool you need is [`uv`](https://docs.astral.sh/uv/). Each
[GitHub Release](https://github.com/Azure/osdu-spi-stack/releases) publishes a
versioned `spi` wheel (`spi-X.Y.Z-py3-none-any.whl`); you install it directly
from its GitHub Release URL.

### Install the latest release

These commands always resolve the newest release, so they never need updating
between versions. Pick the one for your shell.

**macOS / Linux (bash, zsh):**

```bash
uv tool install "$(curl -fsSL https://api.github.com/repos/Azure/osdu-spi-stack/releases/latest \
  | grep -o 'https://github.com/Azure/osdu-spi-stack/releases/download/[^"]*-py3-none-any.whl')"
```

**Windows (PowerShell):**

```powershell
uv tool install (irm https://api.github.com/repos/Azure/osdu-spi-stack/releases/latest).assets.where({ $_.name -like '*-py3-none-any.whl' }).browser_download_url
```

Then verify:

```bash
spi --version
```

After install the `spi` binary is on PATH; no `uv run` prefix.

### Install a specific version (reproducibility)

To pin an exact version — for CI, reproducible environments, or bug reports —
install its wheel URL directly (same command on every platform):

```bash
uv tool install https://github.com/Azure/osdu-spi-stack/releases/download/v0.1.0/spi-0.1.0-py3-none-any.whl
```

Copy the wheel URL for any version from that release's page under
[Releases](https://github.com/Azure/osdu-spi-stack/releases).

### Upgrade

After the first install, `spi` upgrades itself — no URL needed, same on every
platform:

```bash
spi update           # check for a newer version and install it
spi update --check   # check only; do not install
spi update --force   # reinstall even if already on the latest version
```

> **Note:** `uv tool install git+https://github.com/...@vX.Y.Z` also works,
> but the wheel-URL forms above are preferred because they preserve the
> tag-derived version in `spi --version` reliably.

## Quick Start

Once installed:

```bash
# Check prerequisites
spi check

# Deploy (provisions Azure resources + activates GitOps)
spi up --env dev1

# Optional fallback topology; the selected mode is preserved on reruns
spi up --env dev1 --aks-mode base

# Creator access is seeded into Entitlements by default.
# Disable it for workload-only automation:
spi up --env dev1 --no-seed-creator

# Deploy a coordinated release tag across SPI service repositories
spi up --env dev1 --image-tag v1.2.3

# Advanced: validate the same feature ref across multiple service repositories
spi up --env dev1 --image-ref fix/core-lib-azure-3.0.1

# Compatibility fallback to OSDU community images
spi up --env dev1 --image-source community --image-branch master

# Multi-partition deploy (one CosmosDB + Service Bus + storage per partition)
spi up --env dev1 --partition opendes --partition tenant1

# Pick an ingress mode (default: azure)
spi up --env dev1 --ingress-mode dns --dns-zone example.com
spi up --env dev1 --ingress-mode ip       # debug / smoke

# Middleware only, no OSDU services
spi up --env dev1 --profile minimal
```

### After Deploy

```bash
spi status              # Deployment health dashboard
spi status --watch      # Continuous refresh
spi info                # Endpoints and credentials

spi reconcile --suspend # Freeze: stop Flux auto-reconciliation
spi reconcile --resume  # Unfreeze: resume Flux auto-reconciliation

spi down --env dev1     # Tear down when done
```

## Development Setup

To work on `spi` itself rather than install it:

```bash
git clone https://github.com/Azure/osdu-spi-stack.git
cd osdu-spi-stack
uv sync
uv run spi --help
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full developer workflow,
including pre-commit setup, conventional commits, and the release process.


## Operating Model

SPI Stack is **GitOps + bootstrap**, not "pure GitOps from an empty cluster."

The CLI performs a bootstrap phase:

- Provision Azure PaaS resources (CosmosDB, Service Bus, Storage, Key Vault)
- Create AKS Automatic by default, or Base + NAP when explicitly selected
- Configure Workload Identity and RBAC role assignments
- Bootstrap the cluster with namespaces, secrets, ConfigMap, and ServiceAccount
- Activate the AKS native Flux extension pointing to this repo

After that handoff, **Flux owns steady-state reconciliation** and continuously converges the cluster to the desired state.

<details>
<summary>Deployment phases</summary>

1. **Core Infra**: Resource Group, selected AKS topology, Managed Identity, Key Vault, ACR
2. **Data Infra**: CosmosDB (Gremlin + SQL), Service Bus, Storage Accounts
3. **IAM**: Federated credentials, RBAC role assignments, Key Vault secrets
4. **K8s Bootstrap**: Namespaces, StorageClasses, secrets, ConfigMap, ServiceAccount
5. **GitOps**: AKS native Flux extension pointing to this repo

A full `spi up` typically takes ~45-50 minutes, dominated by AKS provisioning (~30 min). Exact times vary by region.

</details>

<details>
<summary>Environment isolation</summary>

Use `--env` to run multiple isolated deployments. Each environment gets its own resource group and cluster (e.g., `spi-stack-dev1`, `spi-stack-team`).

```bash
uv run spi up --env dev1
uv run spi up --env dev1 --aks-mode base
uv run spi up --env dev1 --application-insights
uv run spi up --env staging
```

Application Insights and its Log Analytics workspace are disabled by default.
Use `--application-insights` when the environment needs request, dependency,
and exception telemetry.

AKS Automatic is the default. Use `--aks-mode base` only when Base + Node
Autoprovisioning is required. Both choices are immutable for the lifetime of an
environment.

</details>


## What It Deploys

Three namespaces, deployed in dependency order via a 7-layer Kustomization stack:

| Namespace | Layer | Deploys |
|-----------|-------|---------|
| **foundation** | Operators | ECK (Elasticsearch), CNPG (PostgreSQL), cert-manager |
| **platform** | Middleware | Elasticsearch, Redis (TLS), PostgreSQL (Airflow), Airflow, Istio Gateway |
| **osdu** | Services | partition, entitlements, legal, schema, storage, search, indexer, file, workflow + 3 reference services |

### Profiles

`--profile` selects how much of that stack Flux reconciles:

| Profile | Deploys |
|---------|---------|
| `core` (default) | Everything above. |
| `minimal` | `foundation` and `platform` only — operators, cert-manager, trust-manager, Gateway, Elasticsearch, Redis, PostgreSQL, Airflow. No OSDU services. |

Use `minimal` when you are working on the middleware itself and the OSDU services would only add deploy time. The middleware layers are identical between profiles, so what you validate on `minimal` holds on `core`.

### Azure PaaS Resources

| Resource | Purpose |
|----------|---------|
| AKS Automatic / Base + NAP | Kubernetes with managed Istio and Karpenter Node Autoprovisioning |
| CosmosDB Gremlin | Entitlements graph |
| CosmosDB SQL | OSDU operational data (per partition) |
| Service Bus | Async messaging (per partition, 14 topics) |
| Storage Accounts | Blob/table storage (common + per partition) |
| Key Vault | Centralized secret management |
| Managed Identity | Workload Identity for all OSDU services |


## Prerequisites

Everything is discovered by the CLI:

```bash
uv run spi check
```

**Required tools**: az, bicep, kubectl, kubelogin, flux, helm

**System requirements**: Azure subscription with permissions to create resource groups and AKS clusters.


## CLI Reference

```
uv run spi <command> [OPTIONS]

Commands:
  check      Validate required tools are installed
  up         Provision Azure infra and deploy the stack   --env NAME [--profile] [--partition] [--ingress-mode] [--dns-zone] [--dry-run]
  status     Deployment health dashboard                  [--watch]
  down       Delete all Azure resources                   --env NAME
  info       Show endpoints and optional credentials      [--show-secrets]
  reconcile  Force Flux to re-sync from Git               [--suspend] [--resume] [--refresh-images]
```

Use `--dry-run` on `spi up` to preview the Bicep changes (`az deployment group what-if`) before any Azure resources are created beyond the resource group. `--aks-mode` defaults to `automatic`; `base` selects the Base SKU with Node Autoprovisioning and is preserved per environment. `--profile` defaults to `core`; `minimal` deploys middleware only. `--ingress-mode` defaults to `azure`; the other supported modes are `dns` (per-service hostnames on an owned Azure DNS zone) and `ip` (bare IP, debug only). Service images default to each public `yuchen-osdu` package's `main-snapshot`, which is immediately pinned to an immutable digest. Use `--image-tag` for a coordinated release tag, `--image-ref` for advanced multi-repository feature validation, or `--image-source community` for the OSDU GitLab fallback. `--refresh-images` re-resolves the configured selector and reconciles the service Kustomizations.


## Documentation

- [Architecture](docs/architecture.md)  -- 30,000-ft overview of the system
- [Design docs](docs/design/)  -- how subsystems actually work (deployment lifecycle, Bicep, Flux, Workload Identity, ingress, secrets)
- [ADRs](docs/decisions/)  -- decision records with alternatives considered

## License

Licensed under the [Apache License 2.0](LICENSE).

## Contributing

This project welcomes contributions and suggestions. Most contributions require you to
agree to a Contributor License Agreement (CLA) declaring that you have the right to, and
actually do, grant us the rights to use your contribution. For details, visit
[https://cla.opensource.microsoft.com](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to
provide a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow
the instructions provided by the bot. You will only need to do this once across all repos
using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/)
or contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the developer workflow, including pre-commit setup,
conventional commits, and the release process.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use
of Microsoft trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion
or imply Microsoft sponsorship. Any use of third-party trademarks or logos are subject to those
third-party's policies.
