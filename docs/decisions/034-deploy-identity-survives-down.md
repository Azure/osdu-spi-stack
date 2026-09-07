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
with the group and its tags. `spi down --purge` deletes the whole group.

- Deletion order in `src/spi/azure_infra.py`: AKS first, which removes the
  managed nodes group; then Cosmos, Service Bus, storage, ACR, Key Vault, NAT
  gateway, public IP; the VNet last, since the cluster holds its subnets
  until it is gone. The command retries until only identities remain. Key
  Vault soft delete is unaffected, and `spi up` recovers the vault.
- `spi up` on a group that still holds identities adopts them: the ARM
  deployment is incremental, federated credentials are re-declared from the
  roster, and role assignments are re-created on the new cluster and vault.
- The `spi-name-suffix` tag survives with the group, so a reset no longer
  feeds the declared suffix back by hand (ADR-028).
- The reset sequence in `docs/design/environment-lifecycle.md` waits until
  only identities remain instead of until the group is gone.

Rejected: a separate persistent identity resource group. Survives `az group
delete` with no deletion logic, but adds a second group per environment to
name, tag, sweep-exclude, and explain, for one object.

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
- The workload identity survives too, so its client id is stable across
  rebuilds. No component requires that.
- Someone who wants the group gone must say `--purge`.
