# ADR-008: Bicep for Azure Provisioning (AVM for AKS)

**Status**: Accepted

## Context

Provisioning an SPI Stack deploys on the order of 50 Azure resources: UAMI and federated credentials, Key Vault and its secrets, ACR, Cosmos DB Gremlin and per-partition SQL with 24 containers, per-partition Service Bus with 14 topics and 14 subscriptions, common and per-partition Storage with containers and tables, and a scoped RBAC set. An imperative `az` CLI orchestrator for this resource graph grows past a thousand lines and ships ordering bugs that ARM would reject at submit time.

Bicep inherits ARM's idempotency and parallel orchestration without a state file. It gives us `what-if` preview and deployment history as first-class features. Azure Verified Modules (AVM) provide Microsoft-maintained, versioned Bicep modules with best-practice defaults for complex resource types.

## Decision

All Azure resources are declared in Bicep. The Python CLI is a thin orchestrator that calls `az deployment group create` twice and handles the seams Bicep cannot cover.

Layout:

- `infra/aks.bicep`. AKS Automatic cluster and managed Istio via the AVM `container-service/managed-cluster` module (pinned version). AVM is used here because Automatic's required configuration (system-pool VM size, Ephemeral OS disk, NAT gateway outbound, Istio `serviceMeshProfile`) is non-trivial to replicate correctly, and AVM bundles the right defaults.
- `infra/main.bicep`. Every other PaaS resource as hand-written Bicep under `infra/modules/` (identity, keyvault, acr, cosmos-gremlin, partition, storage-common, rbac, external-dns-*, vnet). Raw Bicep is simpler than AVM passthrough modules for resources where AVM adds no material defaults.
- `infra/flux.bicep`. AKS Flux extension and `fluxConfigurations` resource (ADR-009), deployed after K8s bootstrap.

Imperative in the CLI (via `az`), not Bicep:

- `az group create`. Bicep cannot create the resource group it deploys into.
- Soft-deleted Key Vault precheck and `az keyvault recover`. ARM cannot branch on a live query.
- `az aks get-credentials`. Kubeconfig merge, not a resource.
- `az aks mesh enable-istio-cni`. AVM v0.13.0 types `proxyRedirectionMechanism` out of `IstioComponents`.
- Key Vault runtime secrets that depend on in-cluster seed passwords (Redis, Elasticsearch per-partition credentials). Written post-handoff by the CLI after middleware is Ready (ADR-010).
- K8s bootstrap: namespaces, StorageClasses, ServiceAccount, `osdu-config` ConfigMap.

`spi up --dry-run` runs `az deployment group what-if` against `aks.bicep` and `main.bicep`, giving an ARM-level diff before any resource provisioning.

AVM module versions are pinned explicitly; upgrades are manual and reviewed.

Rejected:
- **Terraform.** Adds a state file and a plan/apply cycle the stack does not need. A sister repo (`../osdu-spi-infra`) uses Terraform at production scope; the SPI Stack targets dev/test.
- **Full AVM adoption for PaaS modules.** AVM's passthrough modules for Key Vault, ACR, Storage, Cosmos, Service Bus, and Managed Identity do not materially improve defaults over raw Bicep and add a module-version axis to maintain. AVM stays reserved for AKS.
- **Pure `az` CLI orchestrator.** The imperative codebase grew past a thousand lines and kept shipping ordering bugs that ARM rejects at submit time.

## Consequences

- The Python infra orchestrator is small: it resolves names, runs the Bicep deployments, and handles the imperative seams above.
- `spi up --dry-run` is a first-class preview; no equivalent exists in an imperative implementation.
- Debugging a failed deploy shifts from per-command stderr to ARM deployment operation logs. The CLI streams operations in verbose mode.
- Bicep ships with recent `az` CLI versions; `spi check` verifies `az bicep version`.
- Adding a new Azure resource is a Bicep module plus a `main.bicep` wiring change. The CLI does not have to learn the resource.
