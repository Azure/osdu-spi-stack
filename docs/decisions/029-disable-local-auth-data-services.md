# ADR-029: Disable Local Auth on Azure Data Services

**Status**: Accepted

## Context

ADR-001 backs OSDU with Cosmos DB (Gremlin + SQL) and Service Bus; ADR-005 reaches all of them via Workload Identity. The original stack still left local (key / SAS) authentication enabled and wrote the account keys to Key Vault as a fallback that OSDU's Azure provider modules can consume.

The target Microsoft tenant enforces CloudGov Azure Policies that refuse local auth on these services:

- Service Bus creation is denied outright by `ServiceBusCreationDeniedWhenLocalAuthIsEnabledOrMinTlsIsLow` unless `disableLocalAuth: true` and `minimumTlsVersion: '1.2'`.
- A CloudGov `modify` policy forces `disableLocalAuth: true` on every Cosmos DB account at runtime, even when the Bicep does not request it.

Key-based access that works in an unconstrained tenant therefore does not work here. The keys we write to Key Vault are dead, and any service that authenticates with them fails.

## Decision

Disable local auth everywhere and authenticate every data-plane call with the OSDU managed identity plus the service-specific data-plane role:

| Service | Local auth | Data-plane role granted to the OSDU UAMI |
|---|---|---|
| Cosmos DB Gremlin (shared) | `disableLocalAuth: true` in `cosmos-gremlin.bicep` | Cosmos DB Built-in Data Contributor for Gremlin (`...0004`) |
| Cosmos DB SQL (per partition) | enforced by tenant policy | Cosmos DB Built-in Data Contributor (`...0002`), assigned in `partition.bicep` |
| Service Bus (per partition) | `disableLocalAuth: true` + `minimumTlsVersion: '1.2'` in `partition.bicep` | Service Bus Data Sender + Receiver (`rbac.bicep`, ADR-005) |

The graph and Service Bus key secrets are removed or set to the `"DISABLED"` placeholder; services select the MSI / Workload-Identity client by setting `AZURE_MSI_ISENABLED=true` on the Service Bus consumers.

Rejected: keep key auth and store keys in Key Vault. Denied by tenant policy and leaves credentials at rest. Rejected: per-account custom role definitions. The built-in data-contributor roles already scope to the account; custom roles add deployment surface without a least-privilege win at this scale.

## Consequences

- No Cosmos or Service Bus keys live in Key Vault. The access envelope is the same one ADR-005 already defined, extended with the Cosmos data-plane roles.
- Cosmos data-plane RBAC is a Cosmos-specific resource (`sqlRoleAssignments` / `gremlinRoleAssignments`), not Azure RBAC, so it is invisible to `az role assignment` and takes ~5-15 minutes to propagate. Services cache the Cosmos client at startup and may need a restart after a fresh grant.
- Two community images do not support this model and must be replaced (ADR-017 image-lock): **entitlements** (community image reads `graph-db-primary-key`, which no longer exists, so it needs the MSI-Gremlin build) and **indexer-queue** (its pinned `core-lib-azure` 2.0.6 cannot do Workload-Identity Service Bus, so it needs a `core-lib-azure` 2.5.x build).
- The per-partition Cosmos SQL data-plane role was the one path initially missed; without it legal, storage, schema, and workflow all returned `403 "does not have required RBAC permissions"`.
