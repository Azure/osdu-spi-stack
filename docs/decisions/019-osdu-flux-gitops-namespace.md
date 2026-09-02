# ADR-019: SPI-Owned GitOps Objects in a Dedicated osdu-flux Namespace

## Context

`flux-system` is owned by the AKS Flux extension, and AKS Automatic protects extension-managed system namespaces with the `aks-managed-protect-system-namespace-objects` admission policy: the deploying principal can neither create the namespace ahead of the extension nor write objects into it afterwards. The stack's GitOps objects (GitRepository, Kustomizations, HelmReleases, seed Secrets, and bootstrap ConfigMaps) therefore cannot live there; a CLI bootstrap that seeds secrets, the image lock, and the ingress ConfigMap into `flux-system` fails at the first write.

The deployer must be able to seed ConfigMaps and Secrets before Flux reconciles the HelmReleases that consume them via `valuesFrom` and `postBuild` substitution. Flux controllers themselves remain in `flux-system`; only the objects the stack owns need a writable home. Multi-tenancy enforcement in the Flux extension injects a `flux-applier` service-account impersonation that the same admission policy rejects.

## Decision

The CLI creates a dedicated `osdu-flux` namespace during bootstrap, seeds all SPI-owned ConfigMaps and Secrets there, and `infra/flux.bicep` sets the `fluxConfigurations` namespace accordingly. `multiTenancy.enforce` is disabled on the extension so the controllers apply manifests as their own (exempt) `flux-system` identities instead of impersonating a service account the platform blocks.

Rejected: policy exemptions for deployer writes into `flux-system`. Exemptions are not portable across subscriptions.

Rejected: seeding through the ARM `fluxConfigurations` surface only. The ARM surface cannot carry deployment-derived secret material.

## Consequences

- Bootstrap works on AKS Automatic without policy exceptions and behaves identically on clusters without such policies.
- SPI-owned objects and extension-owned controllers live in separate namespaces, so RBAC and troubleshooting scope to one of them.
- Disabling Flux multi-tenancy removes tenant isolation between Kustomizations; acceptable for a single-tenant stack.
- Operational commands address `osdu-flux`, not `flux-system` (`kubectl get kustomizations -n osdu-flux`).
