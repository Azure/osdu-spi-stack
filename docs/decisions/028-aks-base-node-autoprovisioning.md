# ADR-028: AKS Base SKU with Node Autoprovisioning

**Status**: Superseded by [ADR-033](033-selectable-aks-deployment-modes.md)

## Context

ADR-002 chose AKS Automatic so the cluster shipped managed node pools, managed Istio, Deployment Safeguards, and Azure Monitor wiring without us configuring them.

Around 2026-06-02 AKS Automatic made managed system node pools mandatory and began enforcing the "AKS-managed security control changes" guardrail non-bypassably. That guardrail blocks creating or modifying `MutatingWebhookConfiguration` objects, even for cluster-admin. cert-manager and CloudNativePG both install MWCs, so the full SPI Stack can no longer reconcile on Automatic. The behavior is a platform-side change, not a knob we can set, which is why a stack that converged on Automatic weeks earlier now fails.

## Decision

Run the cluster as AKS Standard (`sku.name: Base`) with the same opinionated features wired explicitly in `infra/aks.bicep`:

- **Node Autoprovisioning (NAP / Karpenter)** via `nodeProvisioningProfile`. Retained so the software layer's Karpenter `NodePool` CRs (ADR-007 Layer 0b, ADR-018) and per-service `nodeSelector`s keep working unchanged.
- **Managed Istio** service mesh and ingress gateway addon. Unchanged from Automatic; the `az aks mesh enable-istio-cni` post-deploy seam (ADR-008) remains.
- **Azure RBAC for Kubernetes authorization** and the **OIDC issuer + Workload Identity** add-on, both preconfigured on Automatic but set explicitly on Base.
- **Standard load balancer**, BYO VNet, and a user-assigned cluster identity.

Rejected: stay on Automatic and special-case cert-manager / CNPG. The guardrail is non-bypassable, so there is no special case. Rejected: pin Automatic to its pre-2026-06-02 behavior. Platform guardrail timing is not customer-controllable.

## Consequences

- **Deployment Safeguards are no longer cluster-enforced.** The non-bypassable `ValidatingAdmissionPolicy` was an Automatic-only feature. Compliance does not regress in practice: ADR-004's local Helm chart still bakes the same `securityContext` (non-root, seccomp `RuntimeDefault`, dropped capabilities, requests/limits, probes) into every workload at authoring time. Enforcement moves from admission-time to chart-time.
- **Kubelet AcrPull must be granted explicitly.** Automatic wired the kubelet identity to the attached ACR; Base does not. `infra/modules/rbac.bicep` assigns AcrPull to the kubelet identity so custom-image pulls (ADR-017 image-lock overrides) resolve from ACR without an image pull secret.
- NAP, NodePools, managed Istio, Workload Identity, and Azure Monitor for containers all still apply, so ADR-005, ADR-007, ADR-008, and ADR-018 are unaffected.
- The stack is now reproducible against current AKS behavior rather than a frozen Automatic snapshot.
