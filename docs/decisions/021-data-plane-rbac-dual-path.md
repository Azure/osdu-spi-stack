---
status: "superseded by [ADR-027](027-entra-only-data-plane.md)"
contact: "danielscholl"
date: "2026-07-28"
deciders: "danielscholl"
---

# Dual-Path Data-Plane Access: Cosmos/Service Bus RBAC Alongside Keys

## Context and Problem Statement

Some Azure environments enforce policies that deny or disable local
(key/SAS) authentication on data services: Service Bus namespace creation
is rejected unless local auth is off and TLS >= 1.2, and Cosmos DB accounts
can have `disableLocalAuth` forced on by a `modify` policy at deploy time.
In such environments, key-based access silently stops working even though
the keys still exist in Key Vault. Other environments have no such
policies, and the community OSDU images there may still depend on key/SAS
paths (ADR-005 documents the Service Bus SAS carve-out).

## Decision Drivers

- One codebase must deploy cleanly in both policy-constrained and
  unconstrained subscriptions (no tenant-specific assumptions in code).
- Cosmos data-plane roles are additive: they coexist with key access and
  activate automatically wherever policy kills the keys.
- Workload Identity is the strategic access path for every service
  (ADR-005); keys are the compatibility path.

## Considered Options

- Dual-path: grant data-plane RBAC everywhere, keep key secrets, make the
  one hard-required setting (Service Bus local auth) a parameter
- Entra-only: disable local auth explicitly on every service and remove
  key secrets
- Keys-only: rely on key material and require policy exemptions in
  constrained tenants

## Decision Outcome

Chosen option: "Dual-path". The stack now grants the OSDU managed identity
the Cosmos SQL Built-in Data Contributor role on every partition account,
the Gremlin Data Contributor role on the graph account, and retains the
existing Key Vault key secrets. Service Bus namespaces set
`disableLocalAuth` via the `serviceBusDisableLocalAuth` parameter
(default `true`, with `minimumTlsVersion: '1.2'`); deployments that need
the SAS path can set it to `false`. Two operational notes: Cosmos
data-plane roles are Cosmos-native (`sqlRoleAssignments` /
`gremlinRoleAssignments`, invisible to `az role assignment`) and take
5–15 minutes to propagate, and services cache Cosmos clients at startup,
so a fresh grant may require a pod restart.

The same change set also adds the supporting grants and secrets the
data plane needs regardless of auth mode: `system-cosmos-*` Key Vault
secrets for system services (schema, workflow), a per-partition blob
container named after the partition id (the storage provider does not
auto-create it), kubelet-identity AcrPull for custom image pulls, and a
`deployerPrincipalType` parameter so human and service-principal
deployers both get valid role assignments.

### Consequences

- Good, because the same Bicep deploys in constrained and unconstrained
  subscriptions without edits.
- Good, because RBAC grants are inert where keys work and load-bearing
  where they do not.
- Bad, because key secrets remain present in Key Vault until an
  Entra-only end state is adopted; readers must know which path a given
  environment actually uses.
- Bad, because the parameter default (`true`) requires SAS-dependent
  deployments to opt out explicitly.
