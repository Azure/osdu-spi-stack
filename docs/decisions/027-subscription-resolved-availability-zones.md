# ADR-027: Subscription-Resolved System Pool Availability Zones

## Context

The AKS system pool is created with an explicit zone list, exposed as the
`availabilityZones` parameter on `infra/aks.bicep` with a default of all three
zones. Zone availability for a VM size is scoped to the subscription, not just
the region: the same size in the same region can be offered in three zones to
one subscription and two to another, and the set changes as Azure capacity
changes. So the correct value is not a property of the template and is not
stable enough to record per region.

The failure modes pull in opposite directions. Naming a zone the subscription
cannot use fails provisioning with `AvailabilityZoneNotSupported`. Naming fewer
zones to be safe is also rejected, because AKS Automatic requires the full
usable set for the size it schedules on. A deployment that works in one
subscription therefore fails in another, after the resource group already
exists, as an ARM error rather than an actionable message.

## Decision

The CLI resolves the usable zones from the target subscription before deploying
the cluster. `_resolve_system_pool_zones` in `src/spi/azure_infra.py` reads the
compute SKU catalogue (`az vm list-skus`) for `SYSTEM_POOL_VM_SIZE` in the
target region, takes the published zones, subtracts the ones this subscription
restricts, and passes the remainder as the `availabilityZones` parameter. The
size itself is passed from the same constant, so the CLI and the template
cannot disagree on which SKU the zones were resolved for. The env var
`SPI_SYSTEM_POOL_VM_SIZE` overrides the size for both the query and the
template, so an override receives the same preflight validation; the chosen
size must still support the ephemeral OS disk AKS Automatic requires on the
system pool.

Three states stop the deployment preflight, each naming the size and region:
the size is not offered in the region, no zone survives the restrictions, or a
restriction reduces the set below what is published (AKS Automatic refuses a
reduced list). When the catalogue read itself fails (throttling, policy), the
CLI warns and leaves the template default in place rather than blocking the
deployment on an unverifiable answer; ARM then adjudicates as it did before
this record.

The template keeps its parameter and default, so a direct `az deployment`
without the CLI still works and an explicit pin remains possible.

Rejected: requiring operators to override the parameter per environment; the
value is discoverable, and the cost of guessing wrong is a failed deployment in
a subscription they may be using for the first time. Rejected: a per-region
zone table in the repository; it encodes one subscription's entitlements into a
shared template and goes stale silently. Rejected: failing hard when the
catalogue read fails; that converts a throttled read into a blocked deployment
whose zones were, in every observed subscription, the default anyway.

## Consequences

- The same template deploys unmodified across subscriptions, and a restricted
  zone is discovered before any cluster resource is created.
- The preflight error names the size and region, so an operator can pick a
  different region without reading ARM traces.
- Cluster creation depends on one more read that can be throttled or blocked
  by policy; that path degrades to the template default with a warning instead
  of failing.
- A subscription whose zone offerings change between runs can present a zone
  set that differs from the existing pool's; ARM rejects the in-place zone
  change, which is the same outcome the hardcoded default produced.
