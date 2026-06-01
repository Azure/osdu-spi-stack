# ADR-005: Workload Identity for Azure PaaS Access

**Status**: Accepted

## Context

OSDU services authenticate to Cosmos DB, Service Bus, Azure Storage, and Key Vault. The alternative is to store connection strings or service-principal credentials as Kubernetes Secrets; those leak easily, require rotation, and multiply the secret inventory.

AKS Automatic enables the OIDC issuer by default, which is the precondition for Azure Workload Identity: a ServiceAccount token is federated with a user-assigned managed identity (UAMI), and the pod exchanges it for an Entra ID access token at runtime.

## Decision

Use a single UAMI (`<cluster>-osdu-identity`) federated with the `workload-identity-sa` ServiceAccount name across the fixed OSDU namespace set (`default`, `osdu-core`, `airflow`, `osdu-system`, `osdu-auth`, `osdu-reference`, `osdu`, `platform`). All OSDU services run under that ServiceAccount.

- The UAMI is declared in `infra/modules/identity.bicep` and receives RBAC role assignments via `infra/modules/rbac.bicep`: Key Vault Secrets User, Storage Blob Data Contributor, Storage Table Data Contributor, Service Bus Data Sender, Service Bus Data Receiver, AcrPull.
- The ServiceAccount carries `azure.workload.identity/client-id` and `tenant-id` annotations.
- Pods opt in with the `azure.workload.identity/use: "true"` label; the AKS webhook projects the federated token file and injects `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_FEDERATED_TOKEN_FILE`.

Ingress mode `dns` provisions a second UAMI (`<cluster>-external-dns`) scoped `DNS Zone Contributor` on the target DNS zone (ADR-012).

Rejected: per-service UAMIs with least-privilege scoping. The role surface (the same six roles across every service) does not differentiate enough to justify the federation and RBAC volume at the SPI Stack's current scope.

## Consequences

- Zero stored credentials for Azure PaaS access. Tokens are short-lived and refreshed automatically.
- One identity, one set of RBAC bindings. Provisioning is deterministic and re-runs are idempotent.
- All OSDU services share the same access envelope; there is no per-service blast-radius containment at the Azure layer. Containment is at the Kubernetes RBAC and mesh layer instead.
- The schema-load Job (ADR-013) and any future workloads in the `osdu` namespace reuse this ServiceAccount without any new Azure-side provisioning.
- One per-partition carve-out remains: indexer-queue's subscription client builds a Service Bus connection string regardless of the Workload Identity flag, so `${partition}-sb-connection` holds a real SAS key in Key Vault. The key is gated by the same UAMI's `Key Vault Secrets User` role and never lands in a pod env var. See [secret lifecycle](../design/secret-lifecycle.md) for the full mechanics.
