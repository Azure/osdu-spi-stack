# ADR-025: One Flux Inventory Owner per Kubernetes Object

## Context

The stack profile's `spi-gateway` Kustomization and the TLS ingress modes' `spi-gateway-tls` Kustomization both rendered the `spi-gateway` Gateway. Their desired states differed, so each reconcile replaced the other's listeners and the TLS owner never became Ready. Even byte-identical objects are unsafe to share: pruning either inventory can delete an object the other still claims. Moving an object between inventories needs an explicit handoff, because deleting a Kustomization under Flux's default `MirrorPrune` policy can remove its objects after the new owner has applied them.

## Decision

Every rendered Kubernetes object has exactly one Flux Kustomization inventory owner.

- The selected ingress tree is the Gateway's sole renderer. Every non-bare mode declares that owner under the one name `spi-gateway-tls`: TLS modes point it at their overlay, `ip` at the base component, and route Kustomizations depend on it. One child identity means switching `--ingress-mode` rewrites `spec.path` on an existing inventory rather than pruning one child and creating another, whose `MirrorPrune` deletion would take the Gateway with it.
- An owner that stops rendering an object first becomes an empty Kustomization with `prune: false` and `deletionPolicy: Orphan`. Only a later rollout, after the empty inventory has reconciled on existing clusters, may remove or rename it.
- The shared `bitnami` HelmRepository moves from `software/components/redis` to `software/components/helm-sources`, owned by a `spi-helm-sources` Kustomization; Redis and ExternalDNS both depend on the source without ExternalDNS gating on Redis's runtime health.

Rejected: keep the base Gateway in the stack profile and patch it from ingress. Two reconcilers still write and prune one object.

Rejected: remove and rename the old Kustomizations in one rollout. `MirrorPrune` can delete resources after their new owner applies them.

## Consequences

- One reconciler applies and prunes each object, and existing inventories cannot delete resources during handoff.
- Each ingress mode still renders the complete desired Gateway.
- Handoff Kustomizations stay visible until a later rollout removes them.
- The `spi-gateway-tls` name can shorten to `spi-gateway` once the handoffs are gone, in a rollout that carries its own handoff.
