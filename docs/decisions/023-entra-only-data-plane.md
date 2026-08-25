# ADR-023: Entra-Only Data Plane: Disable Local Auth on Cosmos and Service Bus

## Context

Cosmos DB and Service Bus offer two data-plane paths: Entra-backed RBAC and local (key/SAS) authentication. The accounts were first provisioned dual-path: RBAC granted everywhere, local auth left enabled as a compatibility path for community images, and compliance delegated to an external tenant policy expected to disable local auth after the fact. That leaves key material in the deployment, makes the effective posture depend on a policy that may be absent or audit-only, and fails outright in tenants whose policy denies creating data services with local auth enabled.

## Decision

`disableLocalAuth: true` is hardcoded on the Cosmos Gremlin account, every per-partition Cosmos SQL account, and every per-partition Service Bus namespace; the data-plane role assignments (Cosmos SQL and Gremlin Data Contributor, Service Bus Data Sender/Receiver) become the only access path. Compliance is a property of the infrastructure code, not of tenant policy, so one code path deploys cleanly in constrained and unconstrained subscriptions alike.

`listKeys()` is rejected on a Cosmos account with local auth disabled, so the Cosmos key writes go with it: `graph-db-primary-key` is no longer written, and the per-partition key and connection secrets carry the literal `DISABLED` because the partition record references them by name. Community images that authenticate with keys or SAS read those secrets and fail against `DISABLED` until Workload-Identity-capable images replace them (the SPI custom-image supply chain); this decision covers the infrastructure posture only.

Rejected: dual-path (RBAC everywhere plus retained key secrets). Deploys in any subscription, but leaves key material present and makes the effective access path environment-dependent.

Rejected: keys-only with policy exemptions in constrained tenants. Exemptions are not portable, and creation fails where policy denies local auth.

## Consequences

- The deployment is compliant by construction in every tenant, with no key material and no dependency on an external policy.
- Workload Identity (ADR-005) is the only data-plane path; community key/SAS images break until custom images land.
- The `DISABLED` placeholder secrets remain only to satisfy the partition-record schema.
- Cosmos data-plane roles are Cosmos-native (`sqlRoleAssignments` / `gremlinRoleAssignments`, invisible to `az role assignment`) with 5-15 minute propagation; services cache clients at startup, so a fresh grant may require a pod restart.
