# ADR-034: Managed Identities Survive `spi down`

## Context

A trusted repository holds five values (ADR-032): `AZURE_CLIENT_ID`,
`AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `SPI_STACK_RESOURCE_GROUP`,
`SPI_STACK_CLUSTER`. Four are stable across a rebuild: the tenant and
subscription do not change, and the group and cluster names derive from
the environment name, so `spi up` recreates them. The client id is the
exception. Azure mints it when the identity is created, and a managed
identity has no soft delete, so `down` and `up` strand every repository
holding the old value. The weekly reset (ADR-029) is exactly that rebuild.

## Decision

`spi down` deletes the resources in the environment group individually and
leaves `Microsoft.ManagedIdentity/userAssignedIdentities` standing, together
with the group and its tags. `spi down --purge` removes external grants before
deleting the whole group and its identities.

- Purge inventories out-of-group Azure role assignments for the identities
  it will delete, using their retained principal IDs and subscription-wide
  assignment enumeration. It does not depend on the cluster still existing,
  or assume that the selected ingress mode describes earlier grants.
  The stack-owned ExternalDNS grant is `DNS Zone Contributor` at DNS-zone
  scope held by the environment's ExternalDNS identity, created by
  `infra/modules/external-dns-role.bicep`. Purge recognises a grant by that
  identity, role, and scope kind, removes it by resource ID, and confirms
  its absence before requesting group deletion; the external zone and its
  resource group are not deletion targets. Purge
  then waits for Azure to report the group gone within the same 45-minute
  deadline as teardown; an accepted delete that has not completed exits
  nonzero naming the group, since acceptance is not completion.
  Discovery failures, missing external-scope permissions, unrecognized
  external grants, or an unconfirmed removal stop purge with the affected
  IDs reported and the environment group and identities retained.
- Ordinary `spi down` keeps out-of-group grants because the identities
  survive. A later purge discovers those grants from the retained principal
  IDs, including grants at an earlier DNS zone, and applies the same cleanup
  precondition. Unrecognized grants require operator cleanup, not a blanket
  deletion of role assignments on the external scope.
- Teardown inventories the group before deletion and re-lists it after each
  pass. The deletion plan covers the resource types provisioned by the
  bundled Bicep, including optional resources; an unhandled non-identity
  resource blocks the plan rather than being ignored or deleted blindly.
  An unreadable inventory is a failure, not an empty group.
- Resources with no in-group dependency (AKS, Cosmos, Service Bus, storage,
  ACR, Key Vault, optional telemetry) are requested together and deleted
  concurrently, as `az group delete` would. The cluster and its managed
  nodes group must be gone before network teardown. Subnet associations are
  detached before deleting the NAT gateway, then its public IP, with the
  VNet last. Key Vault soft delete is unaffected, and `spi up` recovers the
  vault.
- The command waits for completion within a 45-minute deadline. Only
  transient or dependency failures are retried, with backoff; authorization,
  resource locks, and unhandled resource types fail with the affected IDs
  and reasons. Deadline expiry exits nonzero with the remaining inventory.
  Success requires a fresh inventory containing only managed identities and
  confirmation that the managed nodes group is gone; delete acceptance is
  not completion. Reset never provisions after a failed or timed-out delete.
- `spi up` on a group that still holds identities adopts them: the ARM
  deployment is incremental and role assignments are re-created on the new
  cluster and vault. Before image resolution it loads source intent from the
  reviewed declaration, or from the retained RG tags for an undeclared
  environment (ADR-033). Credentials and source projections are reconciled
  during bootstrap, not left for a post-provision repair.
- The `spi-name-suffix`, `spi-source-<service>`, and
  `spi-environment-declaration` tags survive with the group. A reset retains
  names, source choices, and the declared-environment boundary without
  operator input (ADR-028, ADR-033).
- The reset sequence in `docs/design/environment-lifecycle.md` waits until
  the identity-only inventory and managed nodes group deletion are confirmed
  instead of until the environment group is gone.

Rejected: a separate persistent identity resource group. Survives `az group
delete` without a custom deletion graph, but adds a second environment group
and separates the retained identity from the suffix and source-policy tags.
Keeping one group accepts ownership of dependency-aware teardown instead.

Rejected: re-stamp the new client id into every repository after a reset.
Keeps `down` simple, but pushes an environment value into N repositories on
a schedule, needs write access to repositories the environment does not own,
and is impossible for a customer environment whose forks the stack cannot
reach.

## Consequences

- A rebuild never rotates the deploy identity, so no repository is touched by
  a reset.
- `spi down` is slower and not atomic: a failure part way leaves a partial
  group. Both `spi up` and a second `spi down` are idempotent against that.
- Provisioning a new resource type adds a teardown obligation. The Bicep
  inventory and deletion plan must stay aligned; a bounded failure leaves
  resources and possible charges visible rather than claiming cleanup.
- The workload identity survives too, so its client id is stable across
  rebuilds. No component requires that.
- Purge needs permission to discover grants in the deployment subscription
  and delete stack-owned assignments on external scopes. Resource-group
  delete permission alone is insufficient for DNS ingress; failure leaves
  the identity available for cleanup instead of orphaning the assignment.
- Someone who wants the group gone must say `--purge`.
