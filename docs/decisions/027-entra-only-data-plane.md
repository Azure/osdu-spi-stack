---
status: "accepted"
contact: "danielscholl"
date: "2026-07-28"
deciders: "danielscholl"
---

# Entra-Only Data Plane: Disable Local Auth on Cosmos and Service Bus

## Context and Problem Statement

The data services (Cosmos DB SQL per partition, the Cosmos Gremlin graph
account, and the per-partition Service Bus namespaces) were provisioned with
local (key/SAS) authentication left enabled, relying on an external tenant
`modify` policy to disable it after the fact (ADR-021, the "dual-path" model).
That leaves key material in the deployment and makes compliance dependent on a
policy that may be absent, in audit-only mode, or applied inconsistently.
Tenants whose policy denies creating data services with local auth enabled
reject the deployment outright. We want a single deployment posture that is
compliant by construction in every tenant.

## Decision Drivers

- Compliance must be a property of the infrastructure code, not of an external
  policy that may or may not be present.
- Workload Identity is the strategic access path for every service (ADR-005);
  key material should not be part of the deployment at all.
- One code path must deploy cleanly whether or not the tenant enforces a
  local-auth policy.
- `listKeys()` on a Cosmos account is rejected once `disableLocalAuth` is true,
  so removing key writes and disabling local auth must happen together.

## Considered Options

- Entra-only everywhere: set `disableLocalAuth: true` on every Cosmos and
  Service Bus account in Bicep and stop writing key material.
- Dual-path (ADR-021): grant data-plane RBAC everywhere but keep the key
  secrets and parameterize Service Bus local auth.
- Keys-only: rely on key material and require policy exemptions in constrained
  tenants.

## Decision Outcome

Chosen option: "Entra-only everywhere". `disableLocalAuth: true` is hardcoded on
the Cosmos Gremlin account, every per-partition Cosmos SQL account, and every
per-partition Service Bus namespace. Because `listKeys()` is not permitted on a
Cosmos account with local auth disabled, all Cosmos key writes are removed:
`graph-db-primary-key` is no longer written (nothing in the deployment
references it by name), and the per-partition secrets
(`{p}-cosmos-primary-key`, `system-cosmos-primary-key`,
`{p}-cosmos-connection`, `{p}-sb-connection`) carry the literal `DISABLED`
because the partition record references them by name. The
`serviceBusDisableLocalAuth` parameter is removed; there is no longer a
per-tenant knob. The data-plane role assignments introduced in ADR-021 (Cosmos
SQL Built-in Data Contributor, Gremlin Data Contributor, Service Bus Data
Sender/Receiver) remain and become the only access path.

This supersedes ADR-021. The dual-path model kept keys as a compatibility path
for community images that authenticate with keys or SAS. Those images still read
the key secrets and will fail against a `DISABLED` value until they are replaced
with Workload-Identity-capable images. Wiring every OSDU service to authenticate
through Workload Identity depends on a custom-image supply chain and is tracked
separately; this ADR covers the infrastructure posture only.

### Consequences

- Good, because the deployment is compliant by construction in every tenant,
  with no key material and no dependency on an external policy.
- Good, because there is a single deployment posture and one access path
  (Workload Identity), removing the `serviceBusDisableLocalAuth` branch and the
  "does it deploy here" ambiguity.
- Bad, because community OSDU images that still authenticate with keys or SAS
  break until custom Workload-Identity images land; the per-partition key and
  connection secrets are retained as `DISABLED` placeholders only to satisfy the
  partition-record schema.
- Neutral, because Cosmos data-plane roles remain Cosmos-native
  (`sqlRoleAssignments` / `gremlinRoleAssignments`, invisible to
  `az role assignment`) with 5-15 minute propagation, and services cache clients
  at startup, so a fresh grant may require a pod restart.
