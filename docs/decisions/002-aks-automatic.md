# ADR-002: AKS Automatic as Compute Substrate

**Status**: Accepted

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

Rejected: standard AKS with self-managed Istio. The installation and upgrade cost outweighs the marginal control gained for a dev/test stack.

## Consequences

- Safeguards are cluster-wide and non-bypassable. Every Pod (ours and any upstream chart we consume) must comply; ADR-004 is the authoring-time answer for OSDU services.
- Istio is Azure-managed. CNI chaining still requires one post-deploy imperative call (`az aks mesh enable-istio-cni`) because the corresponding AVM parameter is typed out of the managed-cluster schema at v0.13.0; ADR-008 tracks that seam.
- Workload Identity is first-class (AKS OIDC issuer on by default); ADR-005 depends on it.
- The Automatic SKU constrains some knobs (system-pool VM size, outbound network path); `infra/aks.bicep` sets the supported combinations.
