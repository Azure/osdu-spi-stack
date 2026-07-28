---
status: "accepted"
contact: "Yuchen Wang"
date: "2026-07-27"
deciders: "Yuchen Wang"
---

# Resolve System Pool Availability Zones per Subscription

## Context and Problem Statement

The AKS system pool is created with an explicit availability-zone list, exposed
as the `availabilityZones` parameter on the cluster template. The parameter
defaults to all three zones and is documented as something to override where a
region has zonal capacity or quota gaps.

That override is the problem. Zone availability for a VM size is scoped to the
*subscription*, not just the region: the same size in the same region can be
offered in three zones to one subscription and two to another, and the set
changes as Azure capacity changes. So the correct value is not a property of the
template, and it is not stable enough to record per region.

The two ways to get it wrong pull in opposite directions. Naming a zone the
subscription cannot use fails provisioning with `AvailabilityZoneNotSupported`.
Naming fewer zones to be safe is also rejected, because the Automatic SKU
requires the full usable set for the size it schedules on. A deployment that
worked in one subscription therefore fails in another, after the resource group
already exists.

## Decision Drivers

- The same template must deploy in any subscription without hand-editing.
- A restricted zone must never reach ARM.
- The Automatic SKU's zone expectations must still be satisfied.
- Failure should be an actionable pre-flight error, not a mid-deployment
  rejection.

## Considered Options

- Resolve the usable zones from the subscription at deploy time
- Keep the parameter and require operators to override it per environment
- Maintain a per-region zone table in the repository

## Decision Outcome

Chosen option: "Resolve the usable zones from the subscription at deploy time".

The template keeps its `availabilityZones` parameter, so the contract is
unchanged and an operator can still pin the value. What changes is that the CLI
now supplies it: before creating the cluster it reads the compute SKU catalogue
for the system pool size in the target region, takes the published zones,
subtracts the ones this subscription restricts, and passes the remainder. If no
zone survives, the deployment stops with an error naming the size and the
region.

Rejected: leaving it to operators, because the value is discoverable and the
consequence of getting it wrong is a failed deployment against a subscription
they may be using for the first time. Rejected: a per-region table, because it
encodes one subscription's entitlements into a shared template and goes stale
silently.

### Consequences

- Good, because the same template deploys unmodified across subscriptions.
- Good, because a restricted zone is discovered before any resource is created.
- Good, because the error names the size and region, so an operator can choose a
  different region without reading ARM traces.
- Good, because the parameter still exists, so an explicit pin remains possible.
- Bad, because cluster creation now depends on one more read that can be
  throttled or blocked by policy.
