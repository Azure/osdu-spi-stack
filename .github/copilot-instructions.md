# OSDU SPI Stack -- Agent Context

Azure-native OSDU deployment using AKS Automatic + Azure PaaS + Flux CD GitOps.
Repository: `Azure/osdu-spi-stack`

## Project Layout

```
src/spi/                  Python CLI (Typer + Rich + Pydantic)
  cli.py                  Commands: check, up, down, status, info, reconcile, update
  config.py               Config model (Azure-only, Profile enum)
  checks.py               Tool prerequisites (az, bicep, kubectl, kubelogin, flux, helm)
  deploy.py               Orchestrates: infra -> bootstrap -> GitOps (deploy_azure)
  azure_infra.py          RG + AKS imperative, PaaS via Bicep (provision_azure_infra)
  bicep.py                az deployment group create wrapper
  bootstrap.py            K8s bootstrap (namespaces, StorageClasses, Gateway API CRDs)
  shell.py                Command execution + kubectl helpers
  secrets.py              In-cluster secret generation (ES, Redis, PG)
  templates.py            YAML templates (GitRepository, Kustomization, ConfigMap)
  images.py               OSDU image-lock resolution
  ingress.py              Ingress mode logic (azure/dns/ip)
  guard.py                Cluster-context safety checks
  status.py               Deployment health dashboard
  info.py                 Endpoint discovery and credential display
  update.py               Self-update command

infra/                    Bicep templates for Azure PaaS provisioning
  main.bicep              RG-scoped entrypoint; wires module deployments
  modules/                Per-concern modules (identity, kv, acr, cosmos-gremlin,
                          partition, storage-common, rbac)
  params/default.bicepparam  Default parameter values for manual deployment

software/
  charts/osdu-spi-service/ Local Helm chart (AKS Safeguards-compliant)
  charts/osdu-spi-init/    Partition + entitlements bootstrap chart
  components/              In-cluster middleware (Flux manifests)
    cert-manager/          TLS certificate management
    trust-manager/         Cross-namespace CA bundle distribution
    operators/eck/         Elasticsearch operator
    operators/cnpg/        PostgreSQL operator
    elasticsearch/         3-node ES cluster
    redis/                 Bitnami Redis with TLS
    postgres/              CNPG cluster (Airflow metadata)
    airflow/               Workflow orchestration
    gateway/               Istio Gateway API
  stacks/osdu/
    profiles/core/         7-layer Kustomization stack
    services/              10 core OSDU service HelmReleases
    services-reference/    3 reference service HelmReleases
    secrets/               ConfigMap placeholder docs

docs/
  architecture.md          System architecture document
  decisions/               18 ADRs
  diagrams/                Excalidraw architecture diagram
```

## CLI Reference

```bash
uv run spi check                            # Validate prerequisites
uv run spi up --env dev1                     # Deploy everything
uv run spi up --env dev1 --profile full      # Deploy with all services
uv run spi up --env dev1 --partition p1 --partition p2  # Multi-partition
uv run spi up --env dev1 --dry-run           # Preview Bicep changes (what-if)
uv run spi down --env dev1                   # Delete all Azure resources
uv run spi status                            # Health dashboard
uv run spi status --watch                    # Continuous refresh
uv run spi info                              # Show endpoints
uv run spi info --show-secrets               # Include credentials
uv run spi reconcile                         # Force Flux reconcile
uv run spi reconcile --suspend               # Freeze GitOps
uv run spi reconcile --resume                # Unfreeze GitOps
```

## Writing Conventions

- No em dashes; use commas, periods, or semicolons.
- Every az/kubectl command displayed transparently via Rich panels.
- Azure resource names derived from --env flag for isolation.

## Key Design Decisions

- Azure-only (no KinD/AWS/GCP); SPI services depend on Azure PaaS (ADR-001)
- AKS Automatic with managed Istio and Deployment Safeguards (ADR-002)
- Imperative CLI bootstrap, then Flux CD + AKS GitOps Extension for K8s workloads (ADR-009)
- Local Helm chart bakes Safeguards compliance into templates (ADR-004)
- Workload Identity for all Azure PaaS access; no stored credentials (ADR-005)
- Three namespaces: foundation, platform, osdu (ADR-006)
- 7-layer Kustomization ordering with explicit dependsOn (ADR-007)
- In-cluster only for ES, Redis, PG (Airflow); everything else is Azure PaaS (ADR-003)
- Azure PaaS provisioning declared in Bicep (`infra/`); RG + AKS + soft-delete
  recovery + post-deploy Key Vault writes remain imperative (ADR-008)

## OSDU Service Provider Context

SPI Stack deploys the **Azure** provider of OSDU services. When exploring
cloned OSDU service repositories (e.g., `workspace/partition`, `workspace/indexer-service`),
each service contains multiple provider implementations under `provider/`:

```
provider/
  partition-aws/        # AWS-specific -- IGNORE
  partition-azure/      # Azure-specific -- THIS IS THE ONE SPI USES
  partition-gc/         # Google-specific -- IGNORE
  partition-ibm/        # IBM-specific -- IGNORE
```

**Only `*-azure/` provider code is relevant to this project.** Other providers
(`*-aws/`, `*-gc/`, `*-ibm/`, `*-core-plus/`) use different cloud services or
in-cluster middleware that is not part of the SPI Stack deployment model.

When investigating service behavior, configuration, or bugs:
1. Start with `*-azure/` provider directories
2. Fall back to `*-core/` (shared base logic) if the azure provider extends it
3. **Skip** `*-aws/`, `*-gc/`, `*-ibm/`, `*-core-plus/` directories entirely
4. The `<service>-core/` module contains shared interfaces and utilities
   that all providers use -- this is relevant when tracing shared behavior

This avoids wasting tokens reading non-Azure implementations that will never run
in an SPI Stack deployment.

## OSDU Service Images

Services use Azure SPI images from the OSDU community registry:
- Pattern: `community.opengroup.org:5555/osdu/platform/.../*-master:tag`
- `spi up` resolves current master SHA tags and writes them to
  `flux-system/osdu-image-lock`; service manifests use Flux post-build
  substitution from that ConfigMap.
- Refresh a live cluster with `uv run spi reconcile --refresh-images`.
- To refresh static checked-in image references, run
  `python scripts/resolve-image-tags.py --update`.

## Deployment Workflow

1. `spi check` -- verify az, bicep, kubectl, kubelogin, flux, helm installed
2. `spi up --env dev1` -- provisions Azure infra (~45-50 min, mostly AKS Automatic), bootstraps K8s, activates GitOps
   - RG + AKS via `az` CLI
   - Identity + KV + ACR + CosmosDB + Service Bus + Storage + RBAC via
     a single `az deployment group create` against `infra/main.bicep`
3. `spi status --watch` -- monitor Flux reconciliation progress
4. Wait for all Kustomizations and HelmReleases to become Ready
5. `spi info` -- get gateway IP and API endpoints
6. `spi down --env dev1` -- cleanup (deletes resource group)
