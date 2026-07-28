# Bicep Architecture

**What this explains.** How `infra/` is organised, why the deployment has
three Bicep stages with two selectable AKS entrypoints, what `infra/modules/`
owns, and which seams the CLI handles imperatively.

**Why it matters.** Most of `spi up` is Bicep, and most failures look like `az` errors that are actually Bicep errors. Knowing which template owns which resource makes the difference between "where do I edit the schema" and "where does the error point me."

> **Companion docs.** [Deployment lifecycle](deployment-lifecycle.md) covers the timing and ordering of these deploys. [Workload Identity](workload-identity.md) explains how identity and RBAC modules wire together at runtime.

## The three deployment stages

![Bicep architecture](../diagrams/bicep-architecture.png)

The AKS stage selects one of two entrypoints; the PaaS and Flux stages are
shared:

| Template | Style | What it lands |
|---|---|---|
| `infra/aks.bicep` | Raw `managedClusters` Bicep | AKS Automatic cluster, BYO VNet + NAT gateway, managed system node subnet, managed Istio, OIDC issuer |
| `infra/aks-base.bicep` | Raw `managedClusters` Bicep | Optional Base SKU + Node Autoprovisioning cluster using the same network and output contract |
| `infra/main.bicep` | Raw modules under `infra/modules/` | Every other PaaS resource: identity, RBAC, Key Vault (with secrets), ACR, Cosmos DB Gremlin, per-partition (Cosmos SQL + Service Bus + Storage), common Storage, optional `external-dns-*` for `dns` ingress |
| `infra/flux.bicep` | Raw (small) | AKS Flux extension + `fluxConfigurations` with two Kustomizations (stack profile, ingress mode) |

Each deploys via `az deployment group create` against the same resource group. They run sequentially because each depends on values from the previous one (kubeconfig from AKS, identity client IDs from main, Kustomization paths from CLI inputs).

### Why split into three

A single template would work, but the three boundaries match three different lifecycles:

- **The selected AKS entrypoint** is the slowest piece (~30 min) and almost
  never changes after first deploy.
- **`main.bicep`** changes when you add a partition, swap a PaaS sku, or wire a new identity. It deploys in ~2-3 min and re-runs idempotently.
- **`flux.bicep`** changes whenever you change profile or ingress mode (`--profile`, `--ingress-mode`). It deploys in seconds.

Splitting them also keeps `--dry-run` useful: it runs what-if against the
selected AKS entrypoint and `main.bicep`, then skips Kubernetes and Flux work.

## Why raw Bicep for AKS and PaaS

AKS Automatic has a non-trivial parameter shape: system-pool VM size,
Ephemeral OS disk, NAT gateway for egress, `hostedSystemProfile` for managed
system nodes, `serviceMeshProfile` for Istio, and Workload Identity flags. Base
+ NAP needs a different set of Azure RBAC, Cilium overlay, and workload
identity properties. Raw separate entrypoints keep those shapes explicit.

The PaaS modules (Key Vault, ACR, Storage, Cosmos, Service Bus, Managed Identity) also use raw Bicep. They are stable resources with well-known shape, so the raw modules under `infra/modules/` are small, readable, and reviewable.

The full rationale is in [ADR-008](../decisions/008-bicep-for-azure-provisioning.md).

## Module inventory (`infra/modules/`)

| Module | Resources | Notes |
|---|---|---|
| `identity.bicep` | UAMI `<cluster>-osdu-identity`, federated credentials | Federated to `workload-identity-sa` across the fixed OSDU namespace set (8, incl. `osdu` and `platform`) |
| `keyvault.bicep` | Key Vault resource only | RBAC lives in `rbac.bicep`; secret values are declared in `main.bicep` (runtime secrets land later via CLI) |
| `acr.bicep` | Container Registry (Basic SKU) | UAMI gets `AcrPull` |
| `cosmos-gremlin.bicep` | Cosmos DB Gremlin account + graph DB | Entitlements graph; shared across partitions |
| `partition.bicep` | Per-partition: Cosmos SQL account + 24 containers, local-auth-disabled Service Bus namespace + 14 topics + 14 subscriptions, Storage account + 5 containers, per-partition KV secrets (`{p}-storage-account-blob-endpoint`, `{p}-cosmos-primary-key`, `{p}-sb-connection` placeholder, etc.) | Looped from `main.bicep` over `dataPartitions` |
| `storage-common.bicep` | Common Storage account (legal tags, cross-partition data) | One per environment, not per partition |
| `rbac.bicep` | RBAC role assignments scoped per resource | Key Vault Secrets User, Storage Blob/Table Data Contributor, Service Bus Data Sender + Receiver, AcrPull |
| `vnet.bicep` | VNet + user-node, managed-system-node and API-server subnets + NAT gateway | Shared by both AKS entrypoints; Base leaves the managed-system subnet unused |
| `external-dns-identity.bicep` | Second UAMI (`<cluster>-external-dns`) + federated credential to ExternalDNS SA | Conditional on `--ingress-mode dns` |
| `external-dns-role.bicep` | `DNS Zone Contributor` role on the DNS zone | Deploys into the zone's RG; the role binds to the zone. Conditional on `--ingress-mode dns` |

`partition.bicep` is the only looped module today. Adding a partition is `spi up --env <env> --partition p1 --partition p2`; `main.bicep` renders one partition module per name.

## Imperative seams in the CLI

Five things Bicep cannot or will not do; the CLI handles them with `az`:

1. **`az group create`.** Bicep cannot create the resource group it deploys into.
2. **Soft-delete Key Vault recovery.** `az keyvault list-deleted | jq` + `az keyvault recover`. ARM cannot branch on a live query, so the CLI checks before submitting `main.bicep` and runs `recover` if needed.
3. **`az aks get-credentials`.** Kubeconfig merge is a client-side operation, not a resource.
4. **`az aks mesh enable-istio-cni`.** The AKS resource provider rejects `proxyRedirectionMechanism` at create time. The CLI patches it imperatively after `aks.bicep` lands.
5. **Runtime Key Vault secrets.** The in-cluster middleware secrets (`redis-*`, `{p}-elastic-*`) are not declarable in Bicep. The CLI writes them with `az keyvault secret set` from the generated seed passwords — no wait for middleware Ready, since the values are known once infra is up. See [ADR-010](../decisions/010-keyvault-secret-management.md) and the [secret lifecycle](secret-lifecycle.md) doc for the full handoff.

These seams are explicit and small. Adding to them is a smell: most "Bicep cannot do this" answers turn out to be missing schema or API details.

## Deploy order (per `spi up`)

```
spi up --env <env>
   │
   ├── az group create
   │
   ├── az deployment group create  --template-file infra/aks[ -base].bicep
   ├── az aks get-credentials
   ├── az aks mesh enable-istio-cni
   │
   ├── (KV recovery if needed)
   ├── az deployment group create  --template-file infra/main.bicep
   │       --parameters dataPartitions=[...] ingressMode=<mode>
   │
   ├── K8s bootstrap (kubectl, see deployment-lifecycle.md)
   │
   ├── az deployment group create  --template-file infra/flux.bicep
   │       --parameters profile=<profile> ingressMode=<mode>
   │
   └── az keyvault secret set  (runtime secrets, from seed)
```

`spi up --dry-run` stops after the `what-if` on `aks.bicep` and `main.bicep`. Everything below that line only runs in a real deploy.

## Worked example: adding a new PaaS resource

Suppose you want to add an Azure Cache for Redis to support a new service.

1. **Write the module.** Create `infra/modules/redis-cache.bicep` declaring the `Microsoft.Cache/redis` resource and any KV secrets the cluster needs (`redis-cache-host`, `redis-cache-key`).
2. **Wire it into `main.bicep`.** Add the `module redisCache 'modules/redis-cache.bicep' = {...}` block with the parameters you need.
3. **Wire RBAC if relevant.** If services need data-plane access via Workload Identity, extend `rbac.bicep` with the new role assignments scoped to the resource.
4. **Plumb the config.** If consumers read it from `osdu-config`, extend `templates.py`'s `osdu_config_configmap()` function. If they read it from Key Vault, the secret you declared in step 1 is enough.
5. **Preview.** `spi up --env dev1 --dry-run` shows the ARM-level diff. If it looks right, run without `--dry-run`.

The CLI does not need to learn the resource. That is the point of declarative provisioning.

## Worked example: what `spi up --dry-run` actually does

```bash
$ uv run spi up --env dev1 --dry-run
```

Output snippet:

```
[ aks.bicep ] az deployment group what-if -g spi-stack-dev1 ...
   ~ Modify: Microsoft.ContainerService/managedClusters/spi-stack-dev1
       agentPoolProfiles[0].nodeCount: 1 → 1   (no change)
       (rest unchanged)

[ main.bicep ] az deployment group what-if -g spi-stack-dev1 ...
   + Create: Microsoft.KeyVault/vaults/secrets/p1-cosmos-endpoint
   + Create: Microsoft.DocumentDB/databaseAccounts/spi-stack-dev1-p1
   ~ Modify: Microsoft.Storage/storageAccounts/spistackdev1common
```

The dry-run does not touch the cluster. It does land the resource group (Bicep needs an RG target), but it skips soft-deleted Key Vault recovery — that runs only on a real deploy (`if not dry_run` in `azure_infra.py`).

## Related ADRs

- [ADR-002](../decisions/002-aks-automatic.md) -- AKS Automatic as Compute Substrate
- [ADR-005](../decisions/005-workload-identity.md) -- Workload Identity for Azure PaaS Access
- [ADR-008](../decisions/008-bicep-for-azure-provisioning.md) -- Bicep for Azure Provisioning
- [ADR-010](../decisions/010-keyvault-secret-management.md) -- Key Vault + ConfigMap Secret Model

## Source files

- `infra/aks.bicep` -- AKS via raw managedClusters Bicep
- `infra/main.bicep` -- the PaaS modules
- `infra/flux.bicep` -- AKS Flux extension + `fluxConfigurations`
- `infra/modules/*.bicep` -- per-resource modules
- `infra/params/default.bicepparam`, `infra/params/multi.bicepparam` -- parameter examples
- `src/spi/bicep.py` -- `az deployment group create` wrapper used by the CLI
- `src/spi/azure_infra.py` -- the imperative seams (RG, KV recovery, mesh CNI)
- `src/spi/deploy.py` -- runtime KV secret writes (`_write_keyvault_bootstrap_secrets`)
