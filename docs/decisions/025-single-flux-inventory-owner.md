# ADR-025: Give Each Kubernetes Object One Flux Inventory Owner

## Context

The stack profile's `spi-gateway` Kustomization and the TLS ingress modes' `spi-gateway-tls` Kustomization both rendered the `spi-gateway` Gateway. Their desired states differed, so each reconcile replaced the other's listeners and the TLS owner never became Ready. Even byte-identical objects are unsafe to share: pruning either inventory can delete an object that another inventory still claims.

Moving an object between inventories also requires an explicit handoff. Deleting an old Kustomization with Flux's default `MirrorPrune` policy can remove its objects after the new owner applies them.

Every rendered Kubernetes object must have exactly one Flux Kustomization inventory owner. Existing clusters must retain their Gateway, certificates, grants, and Helm sources while ownership moves, and ingress-mode-specific listeners must remain reviewable as one complete Gateway rendering.

## Decision

The selected ingress tree is the Gateway's sole renderer. Every non-bare mode declares that owner under one name, `spi-gateway-tls`, which the TLS modes already carry on live clusters: the TLS modes point it at their overlay and the `ip` modes point it at the base component. A single child identity means switching `--ingress-mode` rewrites `spec.path` on an existing inventory rather than pruning one child and creating another, whose `MirrorPrune` deletion would take the Gateway with it. Route Kustomizations depend on that name. The name can be shortened to `spi-gateway` in a later rollout, once the profile-level handoff below is gone and the rename can carry its own handoff.

Any old owner that stops rendering an object first becomes an empty Kustomization with `prune: false` and `deletionPolicy: Orphan`. It remains in that orphaning state until the empty inventory has reconciled on existing clusters. Only a later rollout may remove or rename the old owner.

The shared `bitnami` HelmRepository moves out of `software/components/redis` into `software/components/helm-sources`, owned by a `spi-helm-sources` Kustomization. Redis and `spi-external-dns-release` both depend on it, so ExternalDNS gets an ordering edge to the source it needs without gating on Redis's runtime health. When the source leaves an old inventory, that old owner follows the same empty, non-pruning, orphaning handoff before removal.

Rejected: keep the base Gateway in the stack profile and patch it from ingress. Two reconcilers still write and prune one object.

Rejected: remove and rename the old Kustomizations in one rollout. `MirrorPrune` can delete resources after their new owner applies them.

## Consequences

- One reconciler applies and prunes each object.
- Existing inventories cannot delete resources during handoff.
- Each ingress mode still renders the complete desired Gateway.
- Handoff Kustomizations remain visible until their empty, non-pruning, orphaning state has reconciled and a later rollout removes them.
