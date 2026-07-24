---
status: "accepted"
contact: "Yuchen Wang"
date: "2026-07-24"
deciders: "Yuchen Wang"
---

# Selectable AKS Deployment Modes

## Context and Problem Statement

Two validated cluster topologies now exist for the same stack. AKS Automatic is
the upstream substrate (ADR-002) and is deployable again on Kubernetes 1.36
(ADR-019). AKS Base with Node Autoprovisioning (ADR-026) was adopted while
Automatic blocked the mutating webhooks cert-manager and CloudNativePG require,
and it remains a fully validated deployment shape with explicit Azure RBAC,
Cilium overlay networking, and a region-aware system pool.

A cluster cannot move between the Automatic and Base SKUs in place, so the
topology is a per-environment property rather than a runtime setting. Carrying
only one topology would either abandon the upstream direction or discard a
proven fallback.

## Decision Drivers

- Upstream parity: the default deployment should be the upstream topology.
- A fallback must exist when Automatic platform behavior blocks the stack again.
- An immutable, verifiable per-environment record is required because reruns
  must never mutate an existing cluster into an unsupported shape.
- Only the cluster differs; the software stack, images, and PaaS surface do not.

## Considered Options

- Two selectable modes behind a persistent flag
- Automatic only, deleting the Base implementation
- Base only, deferring Automatic indefinitely
- One conditional AKS template covering both SKUs

## Decision Outcome

Chosen option: "Two selectable modes behind a persistent flag".

`spi up` defaults to `--aks-mode automatic`, which deploys `infra/aks.bicep`.
`--aks-mode base` deploys `infra/aks-base.bicep`. Both templates share the VNet
module, NAT gateway egress, user-assigned control-plane identity, OIDC issuer,
CSI drivers, managed Istio, and output contract, so every downstream phase is
mode-independent.

The resolved mode is persisted as the `spi-aks-mode` resource-group tag next to
the existing suffix and telemetry tags. On rerun the CLI reads the live cluster
SKU and node-provisioning profile, compares it with the tag, and refuses to
continue on disagreement. Environments created before this decision are
classified from the live cluster and tagged on first rerun; a Base cluster
without Node Autoprovisioning is rejected rather than adopted. A conflicting
`--aks-mode` request fails before any infrastructure call.

Rejected: Automatic only, because it discards a proven escape hatch.
Rejected: Base only, because it permanently diverges from upstream.
Rejected: one conditional template, because mode-specific properties are
mutually exclusive and a single resource obscures which shape is deployed.

### Consequences

- Good, because the zero-flag path matches upstream AKS Automatic.
- Good, because the Base topology stays validated without a second software
  stack, image set, or PaaS definition.
- Good, because an environment can never silently change cluster topology.
- Bad, because two AKS templates must be kept in sync as the shared surface
  evolves.
- Bad, because both modes need independent deployment validation before release.
