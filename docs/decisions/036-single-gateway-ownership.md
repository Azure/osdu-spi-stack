---
status: "accepted"
contact: "Yuchen Wang"
date: "2026-07-24"
deciders: "Yuchen Wang"
---

# Single Gateway Ownership by the Ingress Profile

## Context and Problem Statement

The `Gateway` in `aks-istio-ingress` is the single entrypoint for every OSDU
API. ADR-007 places a `spi-gateway` Kustomization in the core profile, and
ADR-012 gives each ingress profile its own listener and certificate surface.
Two independent Flux Kustomizations therefore reconcile the same object.

When one owner renders only the HTTP listener and the other renders HTTP plus
the mode's HTTPS listeners, each reconciliation interval rewrites the other's
result. The endpoint alternates between working and broken on a timer, and no
single manifest explains the observed state.

## Decision Drivers

- Exactly one reconciler must own any object Flux manages.
- The listener set is mode-specific, so ownership belongs with the mode.
- Existing clusters must be able to migrate without losing the live
  certificate.

## Considered Options

- The selected ingress profile is the sole owner
- The core profile owns a base Gateway that ingress patches
- Both owners keep applying and one is made to lose deterministically

## Decision Outcome

Chosen option: "The selected ingress profile is the sole owner".

The core profile no longer contains a `spi-gateway` Kustomization. Each ingress
profile owns exactly one `spi-gateway` child: `ip` renders HTTP only, while
`azure` and `dns` render HTTP plus their HTTPS listeners and certificates in the
same Kustomization. Ownership is therefore invariant across modes and the whole
listener set is visible in one place.

Existing clusters migrate once by disabling pruning on the live Gateway and
Certificate, reconciling the ingress parent so the profile adopts them,
reconciling the core parent to drop the obsolete child, then restoring pruning.

Rejected: base plus patch, because two reconcilers still write the same object.
Rejected: deterministic loser, because it depends on timing rather than
ownership.

### Consequences

- Good, because HTTPS cannot regress to HTTP between reconciliations.
- Good, because the Gateway's full shape is readable from the selected mode.
- Bad, because a one-time adoption procedure is required for clusters that
  predate this decision.
