# ADR-008: Bicep for Azure Provisioning

## Context

Provisioning an SPI Stack deploys on the order of 50 Azure resources: UAMI and federated credentials, Key Vault and its secrets, ACR, Cosmos DB Gremlin and per-partition SQL with 24 containers, per-partition Service Bus with 14 topics and 14 subscriptions, common and per-partition Storage with containers and tables, and a scoped RBAC set. An imperative `az` CLI orchestrator for this resource graph grows past a thousand lines and ships ordering bugs that ARM would reject at submit time.

Bicep inherits ARM's idempotency and parallel orchestration without a state file, and carries `what-if` preview and deployment history as first-class features.

## Decision

All Azure resources are declared in Bicep. The Python CLI is a thin orchestrator that calls `az deployment group create` once per template and handles the seams Bicep cannot cover.

Layout:

- `infra/aks.bicep`. AKS Automatic cluster and managed Istio as a raw `Microsoft.ContainerService/managedClusters` resource on a VNet from `infra/modules/vnet.bicep` with NAT gateway outbound; the system-pool VM size, ephemeral OS disk, and Istio `serviceMeshProfile` are declared directly.
- `infra/main.bicep`. Every other PaaS resource as hand-written Bicep under `infra/modules/` (identity, keyvault, acr, cosmos-gremlin, partition, storage-common, rbac, external-dns-*).
- `infra/flux.bicep`. AKS Flux extension and `fluxConfigurations` resource (ADR-009), deployed after K8s bootstrap.

Imperative in the CLI (via `az`), not Bicep:

- `az group create`. Bicep cannot create the resource group it deploys into.
- Soft-deleted Key Vault precheck and `az keyvault recover`. ARM cannot branch on a live query.
- `az aks get-credentials`. Kubeconfig merge, not a resource.
- `az aks mesh enable-istio-cni`. The resource provider rejects `proxyRedirectionMechanism` at cluster creation; the CLI skips the call when the cluster already reports `CNIChaining`.
- Key Vault runtime secrets: Redis and per-partition Elasticsearch credentials from the generated seed passwords and fixed in-cluster hostnames, plus `tbl-storage-endpoint` derived from the common Storage account name. Written post-handoff by the CLI without waiting for middleware (ADR-010).
- K8s bootstrap: namespaces, StorageClasses, ServiceAccount, `osdu-config` ConfigMap.

`spi up --dry-run` runs `az deployment group what-if` against `aks.bicep` and `main.bicep`, giving an ARM-level diff before any resource provisioning.

Rejected:
- **Terraform.** Mature module ecosystem and a plan/apply cycle reviewers already know, at the cost of a state file to store and lock; `what-if` covers the preview need without one.
- **Azure Verified Modules.** Microsoft-maintained defaults and one versioned module per resource type, at the cost of a module-version axis to track and parameter surfaces that lag the resource provider schema (the Istio `proxyRedirectionMechanism` field was typed out of the managed-cluster module). Raw resources expose the provider's full schema at a pinned API version.
- **Pure `az` CLI orchestrator.** No template language to learn and every step visible in a shell transcript, but resource ordering is hand-maintained and has no preview.

## Consequences

- The Python infra orchestrator is small: it resolves names, runs the Bicep deployments, and handles the imperative seams above.
- `spi up --dry-run` is a first-class preview; no equivalent exists in an imperative implementation.
- Debugging a failed deploy shifts from per-command stderr to ARM deployment operation logs. The CLI streams operations in verbose mode.
- Bicep ships with recent `az` CLI versions; `spi check` verifies `az bicep version`.
- Adding a new Azure resource is a Bicep module plus a `main.bicep` wiring change. The CLI does not have to learn the resource.
