---
status: "accepted"
contact: "Yuchen Wang"
date: "2026-07-24"
deciders: "Yuchen Wang"
---

# Entra-First Data-Plane Authentication Boundary

## Context and Problem Statement

Two accepted decisions describe the data plane differently. ADR-021 keeps key
and SAS material alongside data-plane RBAC so one codebase deploys in both
policy-constrained and unconstrained subscriptions. ADR-027 disables local
authentication outright because Microsoft-tenant policy denies Service Bus
namespaces with local auth enabled and forces `disableLocalAuth` on Cosmos
accounts.

Both are correct about their own constraint, and the difference is not
theoretical: the deployed service images decide which paths actually work. The
committed GHCR fleet (ADR-030) includes an Entra-capable Entitlements build and
a Workload-Identity-capable indexer-queue, while the Partition service still
reads its Cosmos SQL key from Key Vault.

## Decision Drivers

- Credentials should not exist where the deployed images do not need them.
- The stack must deploy where tenant policy rejects local authentication.
- Only capabilities proven by the deployed images may be assumed.
- Data-plane role grants are additive and harmless where keys still work.

## Considered Options

- Entra-first with a named, capability-driven exception
- Dual path everywhere, keeping all key material
- Entra-only everywhere, including Cosmos SQL

## Decision Outcome

Chosen option: "Entra-first with a named, capability-driven exception".

Cosmos Gremlin and Service Bus set `disableLocalAuth: true`, Service Bus
requires TLS 1.2, and their key secrets are removed or reduced to a `DISABLED`
placeholder. Cosmos SQL keeps its Key Vault key because the Partition service
has no Cosmos data-plane MSI path yet; tenant policy may still disable that key
independently.

Data-plane roles are granted unconditionally in every subscription: Gremlin Data
Contributor on the graph account and Cosmos SQL Built-in Data Contributor on each
partition account, alongside Service Bus Data Sender and Receiver from ADR-005.
Where keys work, the grants are inert; where policy removes keys, they are the
live path.

This supersedes the dual-path posture of ADR-021 for Gremlin and Service Bus and
narrows ADR-027 for Cosmos SQL. Removing the remaining SQL key is gated solely on
Partition gaining the data-plane MSI client.

Rejected: dual path everywhere, because it retains credentials the deployed
images no longer use. Rejected: Entra-only everywhere, because it breaks
Partition today.

### Consequences

- Good, because the same Bicep deploys in constrained and unconstrained
  subscriptions.
- Good, because the only remaining key has an explicit, testable removal
  condition.
- Bad, because readers must know that one service still depends on key
  material.
- Bad, because Cosmos data-plane roles are Cosmos-native, invisible to
  `az role assignment`, and take minutes to propagate.
