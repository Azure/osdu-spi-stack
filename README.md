# OSDU SPI Stack

[![GitHub Release](https://img.shields.io/github/v/release/Azure/osdu-spi-stack)](https://github.com/Azure/osdu-spi-stack/releases)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

**Deploy [OSDU](https://osduforum.org/) to AKS Automatic using Azure-native services.**

SPI Stack provisions Azure infrastructure, bootstraps AKS, and hands application
lifecycle management to Flux GitOps. It gives developers and platform engineers a
reproducible environment for evaluating OSDU on Azure.

> [!IMPORTANT]
> SPI Stack is designed for development, testing, and platform evaluation. It is
> not intended for production deployments.

## Quick Start

### 1. Install

Install [`uv`](https://docs.astral.sh/uv/), then install the latest SPI Stack release.

**macOS and Linux**

```bash
uv tool install "$(curl -fsSL https://api.github.com/repos/Azure/osdu-spi-stack/releases/latest \
  | grep -o 'https://github.com/Azure/osdu-spi-stack/releases/download/[^"]*-py3-none-any.whl')"
```

**Windows PowerShell**

```powershell
uv tool install (irm https://api.github.com/repos/Azure/osdu-spi-stack/releases/latest).assets.where({ $_.name -like '*-py3-none-any.whl' }).browser_download_url
```

Verify the installation:

```bash
spi --version
```

See [Installation](docs/install.md) for pinned versions, upgrades, and troubleshooting.

### 2. Check prerequisites

Deployment requires `az`, Bicep, `kubectl`, `kubelogin`, Flux, and an Azure subscription
where your identity can create resource groups, deploy the listed Azure services, and
create role assignments.

```bash
spi check
```

### 3. Deploy

```bash
spi up --env dev1
```

> [!NOTE]
> A full deployment typically takes 45–50 minutes, dominated by AKS Automatic
> provisioning.

> [!WARNING]
> `spi up` creates billable Azure resources. Cost varies by region, profile,
> partition count, and runtime. Remove the environment when it is no longer needed.

### 4. Inspect and remove

```bash
spi status          # Monitor deployment health
spi info            # View endpoints

# Delete the environment when finished
spi down --env dev1
```

## Why SPI Stack

- **Transparent:** Shows each `az` and `kubectl` command before running it.
- **Azure-native services:** Uses Cosmos DB, Service Bus, Storage, Key Vault, and Entra ID.
- **AKS Automatic:** Includes managed Istio, Karpenter, and Deployment Safeguards.
- **GitOps-driven:** Flux owns in-cluster reconciliation after bootstrap.
- **Secretless authentication:** Workloads access Azure through federated
  Workload Identity.
- **Multi-partition:** Creates isolated Cosmos DB, Service Bus, and Storage resources
  for each OSDU partition.

## Architecture

![SPI Stack architecture](docs/diagrams/architecture.png)

Bicep declares the Azure resources, the `spi` CLI orchestrates provisioning and
cluster bootstrap, and Flux reconciles the Kubernetes workloads from this repository.
OSDU services reach Azure PaaS through Workload Identity; no Azure access keys or
SAS tokens are stored.

See [Architecture](docs/architecture.md) for the control planes, deployment pipeline,
namespace model, and service topology.

## What It Deploys

SPI Stack creates AKS Automatic plus the Azure services required by OSDU: Cosmos DB,
Service Bus, Storage Accounts, Key Vault, Azure Container Registry, and Managed
Identity.

Flux deploys three application namespaces:

| Namespace | Contents |
|-----------|----------|
| `foundation` | ECK, CNPG, cert-manager, and trust-manager |
| `platform` | Elasticsearch, Redis, PostgreSQL, Airflow, and the Istio Gateway |
| `osdu` | Core OSDU APIs, bootstrap jobs, schema load, and reference services |

### Profiles

| Profile | Deploys | Use when |
|---------|---------|----------|
| `core` (default) | Middleware and OSDU services | Evaluating the full stack |
| `minimal` | Operators and middleware only | Developing or validating the platform layer |
| `bare` | Azure infrastructure and activated GitOps | Iterating on infrastructure, identity, or custom workloads |

See [Stack profiles](docs/architecture.md#stack-profiles) for the exact layer boundaries.

## Deployment Options

The default command creates the `opendes` partition and uses an Azure-assigned
hostname. Common variations include:

```bash
# Multiple OSDU partitions
spi up --env dev1 --partition opendes --partition tenant1

# Hostnames in an existing Azure DNS zone
spi up --env dev1 --ingress-mode dns --dns-zone example.com

# Middleware without OSDU services
spi up --env dev1 --profile minimal
```

See [Ingress modes](docs/architecture.md#ingress-profiles) and
[Deployment lifecycle](docs/design/deployment-lifecycle.md) for the complete behavior.

## Common Commands

| Command | Purpose |
|---------|---------|
| `spi check` | Validate deployment prerequisites |
| `spi up` | Provision Azure resources and activate GitOps |
| `spi status` | Show deployment health and reconciliation progress |
| `spi info` | Show cluster endpoints and optional credentials |
| `spi reconcile` | Suspend, resume, or refresh Flux reconciliation |
| `spi service` | Pin services to merge-request pipeline images |
| `spi update` | Check for and install a newer CLI release |
| `spi down` | Delete the environment's Azure resources |

Run `spi --help` or `spi <command> --help` for the complete command reference.

## Documentation

- [Installation](docs/install.md): release installation, version pinning, and upgrades
- [Architecture](docs/architecture.md): system overview and deployed components
- [Deployment lifecycle](docs/design/deployment-lifecycle.md): provisioning and
  reconciliation phases
- [Flux reconciliation](docs/design/flux-reconciliation.md): image locks, refreshes,
  and service pins
- [Gateway and ingress](docs/design/gateway-ingress.md): hostname, TLS, and routing modes
- [Workload Identity](docs/design/workload-identity.md): identity and Azure RBAC flow
- [Architecture decisions](docs/decisions/): governing decisions and trade-offs

## Development and Support

To work on the CLI from a checkout:

```bash
git clone https://github.com/Azure/osdu-spi-stack.git
cd osdu-spi-stack
uv sync
uv run spi --help
```

Development workflow and contribution requirements are in
[CONTRIBUTING.md](CONTRIBUTING.md). For support boundaries and reporting guidance, see
[SUPPORT.md](SUPPORT.md). Bugs and feature requests are tracked in [GitHub
Issues](https://github.com/Azure/osdu-spi-stack/issues).

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
or contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional
questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use
of Microsoft trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion
or imply Microsoft sponsorship. Any use of third-party trademarks or logos are subject to those
third-party's policies.
