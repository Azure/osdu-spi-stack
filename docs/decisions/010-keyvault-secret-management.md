# ADR-010: Key Vault + ConfigMap Secret Model

**Status**: Accepted

## Context

OSDU services need three classes of configuration at runtime:

1. **Credentials for Azure PaaS.** Cosmos DB, Service Bus, Storage, Key Vault itself. These must never live as Kubernetes Secrets.
2. **Metadata and secret values.** Cosmos endpoints, primary keys, Service Bus namespace names, Storage account names. Sensitive, machine-readable, long-lived.
3. **Passwords for in-cluster middleware.** Redis, Elasticsearch, PostgreSQL. Cluster-scoped, regenerated per environment, not reachable from Azure at all.

Mixing all three into Kubernetes Secrets is the simple path and the wrong one: PaaS credentials end up in Git-visible manifests or kubectl-visible secrets.

## Decision

Three stores, each with a single job:

| Class | Store | Access path |
|---|---|---|
| Azure PaaS credentials | Entra ID tokens | Workload Identity (ADR-005); no stored material |
| PaaS metadata and secret values | Azure Key Vault | SDK reads under Workload Identity (or CSI) |
| In-cluster middleware passwords | Kubernetes Secrets in `platform`/`osdu` | CLI generates once per environment; CA material mirrored via trust-manager (ADR-011) |

Non-sensitive endpoint configuration (partition name, tenant ID, cluster ingress hostname, Redis and Elasticsearch FQDNs) lives in the `osdu-config` ConfigMap in the `osdu` namespace and is mounted into services via `envFrom`.

Key Vault secret values are declared **in Bicep** (`infra/main.bicep`) where the source is Azure: endpoints, `listKeys()` on Cosmos accounts, identity IDs, tenant and subscription. The CLI writes only the handful of **runtime** secrets whose values originate in-cluster and are not available at infra-deploy time:

- Per-partition Elasticsearch endpoint, username, password (ECK-issued credentials).
- Redis hostname and password (Bitnami-chart-issued).
- `tbl-storage-endpoint` (derived from the common Storage account).

These runtime writes happen after Flux reconciles the middleware layer, using the CLI's Azure session.

Rejected:
- **Kubernetes Secrets for PaaS credentials.** Visible to anyone with cluster read access; defeats the point of Workload Identity.
- **CSI mount for every secret.** CSI is available (AKS Automatic provides the driver) and is used for a few values, but SDK reads under Workload Identity keep the secret path in code, which matches what OSDU's upstream Azure provider modules already do.
- **Write every Key Vault secret post-deploy from Python.** Loses Bicep's deploy-time guarantees (correct keys, correct access policies) for values that are knowable at infra time.

## Consequences

- Azure PaaS credentials never land in Kubernetes. A compromised cluster leaks in-cluster secrets but not Azure data.
- Key Vault access is audited; every secret read is a log entry.
- The in-cluster secret surface is small (three middleware passwords) and is regenerated deterministically per environment.
- The CLI's post-handoff responsibilities are narrow and bounded: wait for middleware Ready, write a small set of runtime secrets, exit. No long polling tail.
