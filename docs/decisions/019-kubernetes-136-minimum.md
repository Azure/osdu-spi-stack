---
status: "accepted"
contact: "danielscholl"
date: "2026-07-24"
deciders: "danielscholl"
---

# Kubernetes 1.36 Minimum for AKS Automatic

## Context and Problem Statement

AKS Automatic clusters running Kubernetes versions prior to 1.36 block the
creation of every `MutatingWebhookConfiguration` at the authorization layer,
for all identities regardless of RBAC. cert-manager and CloudNativePG both
require mutating admission webhooks to operate: cert-manager pairs its
mutating and validating webhooks (the validating webhook rejects
CertificateRequests whose identity fields the mutating webhook did not
stamp), and the CloudNativePG operator fails at startup when its
MutatingWebhookConfiguration does not exist. The stack therefore could not
reconcile on AKS Automatic below 1.36.

Starting with Kubernetes 1.36, AKS Automatic replaces the blanket block with
a scoped `ValidatingAdmissionPolicy` that only rejects webhook
configurations targeting sensitive resources (`nodes`, `persistentvolumes`,
`certificatesigningrequests`, `tokenreviews`). The cert-manager, CNPG, ECK,
and trust-manager webhooks all target their own API groups and pass this
policy unmodified.

## Decision Drivers

- The operator-based middleware model (ADR-011, CNPG, ECK) is a core
  architectural choice worth preserving.
- AKS Automatic's `stable` upgrade channel holds clusters at N-1, which can
  leave a cluster below 1.36 even when 1.36 is generally available.
- The managed Istio revision must be compatible with the cluster version;
  `asm-1-28` supports at most Kubernetes 1.35.

## Considered Options

- Require Kubernetes >= 1.36 and keep the operator model unchanged
- Remove or replace every component that ships a mutating webhook
- Move from AKS Automatic to the Base SKU where the restriction does not apply

## Decision Outcome

Chosen option: "Require Kubernetes >= 1.36", because it preserves the
operator model with zero changes to the middleware manifests and keeps the
managed-platform benefits of AKS Automatic. `infra/aks.bicep` pins
`kubernetesVersion` to `1.36` and the Istio revision to `asm-1-30` (the
newest revision compatible with 1.36 per `az aks mesh get-revisions`).

Validated end to end on a live AKS Automatic 1.36 cluster: unmodified
cert-manager, CloudNativePG, ECK, and trust-manager charts reconcile
through Flux, create their webhook configurations, and serve traffic.

### Consequences

- Good, because no middleware component needs replacement or modification.
- Good, because the scoped 1.36 policy still blocks webhook configurations
  that target node, volume, CSR, or token-review resources.
- Bad, because deployments must track a recent Kubernetes minor; regions
  where 1.36 is unavailable cannot host the stack until it rolls out.
- Bad, because the managed mesh revision must be bumped in lockstep with
  future Kubernetes minimums.
