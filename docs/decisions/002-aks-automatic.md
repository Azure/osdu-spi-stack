# ADR-002: AKS Automatic as Compute Substrate

## Context

Standard AKS requires the operator to configure and maintain node pools, service mesh, admission policies, monitoring, CSI drivers, and autoscaling. AKS Automatic bundles those as managed features with Microsoft-owned defaults.

The SPI Stack is an Azure-only dev/test platform (ADR-001); the flexibility lost to Automatic's opinionated defaults is a fair trade for not running that configuration ourselves.

## Decision

Deploy the cluster as AKS Automatic (`sku.name: Automatic`). The stack consumes the following managed features:

- **Karpenter** for node auto-provisioning; no manual node pools. Our workload NodePool CRs (ADR-007, Layer 0b) are still user-declared but Karpenter honors them.
- **Managed Istio** service mesh and ingress gateway. Istio installation, upgrades, and CNI chaining are Azure-managed.
- **Deployment Safeguards** enforced as a non-bypassable `ValidatingAdmissionPolicy` (non-root, seccomp `RuntimeDefault`, capability drop, resource requests and limits, probes). ADR-004 covers how our workloads comply.
- **Key Vault CSI** secret provider (available but largely unused; the services read from Key Vault via SDK + Workload Identity).
- **Cilium CNI** in overlay mode.
- **Managed Prometheus** and **Container Insights** for metrics and logs into Azure Monitor and Log Analytics.

The cluster pins `kubernetesVersion` to `1.36` and the Istio revision to `asm-1-30` in `infra/aks.bicep`. Below 1.36, AKS Automatic blocks creation of any `MutatingWebhookConfiguration` at the authorization layer, for every identity regardless of RBAC; cert-manager, CloudNativePG, and ECK require mutating webhooks, so the operator model cannot reconcile there. From 1.36 the blanket block narrows to a scoped `ValidatingAdmissionPolicy` covering `nodes`, `persistentvolumes`, `certificatesigningrequests`, and `tokenreviews`, which the stack's operator webhooks do not touch. The `stable` upgrade channel holds clusters at N-1, so the floor is pinned rather than assumed, and the managed Istio revision moves in lockstep with the Kubernetes minimum (`az aks mesh get-revisions`).

Rejected: standard AKS with self-managed Istio. The installation and upgrade cost outweighs the marginal control gained for a dev/test stack.

Rejected: Kubernetes below 1.36 with the webhook-dependent operators removed or replaced. Each in-cluster middleware system (ADR-003) rides an operator that ships a mutating webhook; replacing them costs more than requiring a recent minor.

## Consequences

- Safeguards are cluster-wide and non-bypassable. Every Pod (ours and any upstream chart we consume) must comply; ADR-004 is the authoring-time answer for OSDU services.
- Istio is Azure-managed. CNI chaining still requires one post-deploy imperative call (`az aks mesh enable-istio-cni`) because the corresponding AVM parameter is typed out of the managed-cluster schema at v0.13.0; ADR-008 tracks that seam.
- Workload Identity is first-class (AKS OIDC issuer on by default); ADR-005 depends on it.
- The Automatic SKU constrains some knobs (system-pool VM size, outbound network path); `infra/aks.bicep` sets the supported combinations.
- Deployments must track a recent Kubernetes minor; regions where 1.36 is unavailable cannot host the stack until it rolls out.
