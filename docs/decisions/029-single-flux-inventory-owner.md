---
status: "accepted"
contact: "danielscholl"
date: "2026-08-24"
deciders: "danielscholl"
---

# Give Each Kubernetes Object One Flux Inventory Owner

## Context and Problem Statement

The stack profile's `spi-gateway` Kustomization and the TLS ingress modes'
`spi-gateway-tls` Kustomization both rendered the `spi-gateway` Gateway. Their
desired states differed, so each reconcile replaced the other's listeners and
the TLS owner never became Ready. Even byte-identical objects are unsafe to
share: pruning either inventory can delete an object that another inventory
still claims.

Moving an object between inventories also requires an explicit handoff.
Deleting an old Kustomization with Flux's default `MirrorPrune` policy can
remove its objects after the new owner applies them.

## Decision Drivers

- Every rendered Kubernetes object must have exactly one Flux Kustomization
  inventory owner.
- Existing clusters must retain their Gateway, certificates, grants, and
  Helm sources while ownership moves.
- Ingress-mode-specific listeners must remain reviewable as one complete
  Gateway rendering.

## Considered Options

- **Selected.** Give the selected ingress tree sole ownership and orphan each
  old inventory before removing it.
- Keep the base Gateway in the stack profile and patch it from ingress.
  Rejected: two reconcilers still write and prune one object.
- Remove and rename the old Kustomizations in one rollout.
  Rejected: `MirrorPrune` can delete resources after their new owner applies
  them.

## Decision Outcome

The selected ingress tree is the Gateway's sole renderer. Every non-bare mode
declares that owner under one name, `spi-gateway-tls`, which the TLS modes
already carry on live clusters: the TLS modes point it at their overlay and the
`ip` modes point it at the base component. A single child identity means
switching `--ingress-mode` rewrites `spec.path` on an existing inventory rather
than pruning one child and creating another, whose `MirrorPrune` deletion would
take the Gateway with it. Route Kustomizations depend on that name. The name can
be shortened to `spi-gateway` in a later rollout, once the profile-level handoff
below is gone and the rename can carry its own handoff.

The old profile-level `spi-gateway` Kustomization remains for one migration
rollout, points at an empty kustomization, sets `prune: false`, and uses
`deletionPolicy: Orphan`. It can be removed only after that state has
reconciled on existing clusters.

The shared `bitnami` HelmRepository moves out of `software/components/redis`
into `software/components/helm-sources`, owned by a `spi-helm-sources`
Kustomization. Redis and `spi-external-dns-release` both depend on it, so
ExternalDNS gets an ordering edge to the source it needs without gating on
Redis's runtime health. `spi-redis` depends on `spi-helm-sources` too, so the
new owner adopts the HelmRepository before the old owner reconciles and drops
it from its inventory. The old `spi-external-dns` inventory uses the same empty,
non-pruning orphan handoff and can be removed after reconciliation.

This supersedes the Gateway placement in ADR-007 and the shared Gateway
ownership implied by ADR-012.

### Consequences

- Good, because one reconciler applies and prunes each object.
- Good, because existing inventories cannot delete resources during handoff.
- Good, because each ingress mode still renders the complete desired Gateway.
- Bad, because temporary handoff Kustomizations remain visible for one
  migration rollout and require a follow-up removal.
