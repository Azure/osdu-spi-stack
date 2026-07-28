---
status: "accepted"
contact: "Yuchen Wang"
date: "2026-07-24"
deciders: "Yuchen Wang"
---

# Runtime-Resolved Managed Istio Revision

## Context and Problem Statement

Sidecar injection on the `osdu` namespace is selected by an `istio.io/rev`
label that must match the managed Istio revision AKS actually installed. That
revision is not constant: it is pinned per cluster template and must track the
Kubernetes minimum (ADR-019), so Automatic and Base clusters can legitimately
run different revisions.

The revision was previously hardcoded in two places, a Flux-managed Namespace
manifest and a CLI fallback constant. Because Flux owns that Namespace, a stale
literal is not merely wrong once: it is continuously reapplied, so it can
overwrite a correct label written during bootstrap and leave OSDU pods without
the intended sidecar.

## Decision Drivers

- The label must always describe the revision the cluster really runs.
- Flux reconciliation must not be able to drift the value back.
- One repository must serve clusters with different pinned revisions.
- A wrong revision should fail loudly rather than degrade silently.

## Considered Options

- Publish the live revision as a substituted variable
- Keep a hardcoded literal and update it with each revision change
- Remove the label and rely on namespace-wide default injection

## Decision Outcome

Chosen option: "Publish the live revision as a substituted variable".

Each AKS template outputs the revision it pins. The CLI carries that value into
bootstrap and writes it into the `spi-ingress-config` ConfigMap as
`ISTIO_REVISION`. The Namespace manifest references `${ISTIO_REVISION}` and its
Kustomization substitutes from that ConfigMap, so Flux and the CLI converge on
the same value instead of competing. When the CLI must infer the revision from
a live cluster, it reads the `istiod-<revision>` deployment name and raises an
error if no revision can be determined.

Rejected: a hardcoded literal, because it silently breaks injection whenever a
cluster pins a different revision. Rejected: default injection, because the
managed mesh expects explicit revision selection.

### Consequences

- Good, because the label cannot disagree with the installed mesh.
- Good, because Automatic and Base clusters share one manifest.
- Good, because an undetectable revision fails the deployment instead of
  producing unlabeled workloads.
- Bad, because the Namespace manifest is no longer standalone; it requires the
  substitution ConfigMap to render.
