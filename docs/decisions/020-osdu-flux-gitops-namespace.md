---
status: "accepted"
contact: "danielscholl"
date: "2026-07-24"
deciders: "danielscholl"
---

# SPI-Owned GitOps Objects in a Dedicated osdu-flux Namespace

## Context and Problem Statement

The stack previously placed its GitOps objects (GitRepository,
Kustomizations, HelmReleases, seed Secrets, and bootstrap ConfigMaps) in
`flux-system`, the namespace owned by the AKS Flux extension. AKS Automatic
protects extension-managed system namespaces with the
`aks-managed-protect-system-namespace-objects` admission policy: the
deploying principal can neither create the `flux-system` namespace ahead of
the extension nor write objects into it afterwards. The CLI bootstrap
(seed secrets, image lock, ingress ConfigMap) therefore failed on AKS
Automatic.

## Decision Drivers

- The deployer must be able to seed ConfigMaps and Secrets before Flux
  reconciles the HelmReleases that consume them via `valuesFrom` and
  `postBuild` substitution.
- Flux controllers themselves remain in `flux-system`; only the objects the
  stack owns need a writable home.
- Multi-tenancy enforcement in the Flux extension injects a `flux-applier`
  service-account impersonation that the same admission policy rejects.

## Considered Options

- Move all SPI-owned GitOps objects to a dedicated `osdu-flux` namespace
- Request policy exemptions for deployer writes into `flux-system`
- Perform all seeding through the ARM `fluxConfigurations` surface only

## Decision Outcome

Chosen option: "Dedicated `osdu-flux` namespace". The CLI creates
`osdu-flux` during bootstrap, seeds all ConfigMaps/Secrets there, and
`infra/flux.bicep` sets the `fluxConfigurations` namespace accordingly.
`multiTenancy.enforce` is disabled on the extension so the controllers
apply manifests as their own (exempt) `flux-system` identities instead of
impersonating a service account the platform blocks. Policy exemptions are
not portable across subscriptions, and the ARM surface cannot carry
deployment-derived secret material.

### Consequences

- Good, because bootstrap works on AKS Automatic without policy exceptions
  and behaves identically on clusters without such policies.
- Good, because SPI-owned objects are cleanly separated from
  extension-owned controllers, simplifying RBAC and troubleshooting.
- Bad, because disabling Flux multi-tenancy removes tenant isolation
  between Kustomizations; acceptable for a single-tenant stack.
- Bad, because operational commands and docs that referenced `flux-system`
  had to change (`kubectl get kustomizations -n osdu-flux`).
