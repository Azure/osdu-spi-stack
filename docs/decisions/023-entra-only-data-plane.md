# ADR-023: Entra-Only Data Plane: Disable Local Auth on Cosmos and Service Bus

## Context

The data services (Cosmos DB SQL per partition, the Cosmos Gremlin graph account, and the per-partition Service Bus namespaces) were first provisioned dual-path: data-plane RBAC granted everywhere, local (key/SAS) authentication left enabled as a compatibility path for community images, and compliance delegated to an external tenant `modify` policy expected to disable local auth after the fact. That leaves key material in the deployment and makes compliance dependent on a policy that may be absent, in audit-only mode, or applied inconsistently. Tenants whose policy denies creating data services with local auth enabled reject the deployment outright. The stack needs a single deployment posture that is compliant by construction in every tenant.

Compliance must be a property of the infrastructure code, not of an external policy that may or may not be present. Workload Identity is the strategic access path for every service (ADR-005); key material should not be part of the deployment at all. One code path must deploy cleanly whether or not the tenant enforces a local-auth policy. `listKeys()` on a Cosmos account is rejected once `disableLocalAuth` is true, so removing key writes and disabling local auth must happen together.

## Decision

Entra-only everywhere. `disableLocalAuth: true` is hardcoded on the Cosmos Gremlin account, every per-partition Cosmos SQL account, and every per-partition Service Bus namespace. Because `listKeys()` is not permitted on a Cosmos account with local auth disabled, all Cosmos key writes are removed: `graph-db-primary-key` is no longer written (nothing in the deployment references it by name), and the per-partition secrets (`{p}-cosmos-primary-key`, `system-cosmos-primary-key`, `{p}-cosmos-connection`, `{p}-sb-connection`) carry the literal `DISABLED` because the partition record references them by name. There is no per-tenant `serviceBusDisableLocalAuth` knob. The data-plane role assignments granted under the dual-path model (Cosmos SQL Built-in Data Contributor, Gremlin Data Contributor, Service Bus Data Sender/Receiver) remain and become the only access path.

The dual-path model kept keys as a compatibility path for community images that authenticate with keys or SAS. Those images still read the key secrets and fail against a `DISABLED` value until they are replaced with Workload-Identity-capable images. Wiring every OSDU service to authenticate through Workload Identity depends on a custom-image supply chain and is tracked separately; this decision covers the infrastructure posture only.

Rejected: dual-path (data-plane RBAC everywhere plus retained key secrets, with Service Bus local auth as a parameter). Deploys in constrained and unconstrained subscriptions alike, but leaves key material present and makes the effective access path environment-dependent.

Rejected: keys-only with policy exemptions in constrained tenants. Exemptions are not portable, and the posture fails outright in tenants that deny local auth at creation.

## Consequences

- The deployment is compliant by construction in every tenant, with no key material and no dependency on an external policy.
- There is a single deployment posture and one access path (Workload Identity), removing the "does it deploy here" ambiguity.
- Community OSDU images that still authenticate with keys or SAS break until custom Workload-Identity images land; the per-partition key and connection secrets are retained as `DISABLED` placeholders only to satisfy the partition-record schema.
- Cosmos data-plane roles remain Cosmos-native (`sqlRoleAssignments` / `gremlinRoleAssignments`, invisible to `az role assignment`) with 5-15 minute propagation, and services cache clients at startup, so a fresh grant may require a pod restart.
