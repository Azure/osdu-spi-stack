# Architectural Decision Records (ADRs)

An Architectural Decision (AD) is a justified software design choice that addresses a functional or non-functional requirement that is architecturally significant. An Architectural Decision Record (ADR) captures a single AD and its rationale.

For more information [see](https://adr.github.io/).

## How to Create an ADR

1. Copy `adr-template.md` to `NNNN-title-with-dashes.md`, where NNNN is the next number in sequence.
   - Check for existing PRs so you do not collide on a number.
   - There is also a short-form template `adr-short-template.md` for smaller decisions.
2. Edit `NNNN-title-with-dashes.md`.
   - Status starts as `proposed`.
3. For each considered option, write one line on why it was rejected. The decision is what was chosen; alternatives get just enough space to show the trade-off.
4. Open a PR. The status moves to `accepted` once the decision is agreed.
5. Decisions can be changed later. A new ADR supersedes an old one; do not edit accepted ADRs in place.

## ADR Style

ADRs record decisions, not engineering logs. Keep them short and forward-facing so a reader can grok the decision in a single pass.

- **No `## Validation` sections.** Phase-by-phase acceptance logs belong in the PR description, not in the ADR.
- **No post-acceptance `## Amendment` sections.** If a decision needs revising, write a new ADR that supersedes it. Inline amendments re-open a record that should be closed.
- **No incident narrative in Context.** State the structural problem the decision addresses. Specific clusters, timeout values, and triggering incidents age poorly inside the ADR.
- **One-line option rejections.** Write "Rejected: <one clause>" rather than paragraphs re-litigating prior attempts.
- **Forward-looking, not retrospective.** "Supersedes X because..." is fine; "The previous version of this ADR proposed..." is a sign the ADR should be superseded rather than edited.

## When to Create an ADR

Create an ADR for any decision that could plausibly have gone a different way and where the alternative would be defensible:

- Architecture patterns (deployment strategies, dependency ordering, GitOps boundaries).
- Technology choices (middleware selection, operators, provisioning tools).
- Design patterns (namespace model, credential handling, ingress strategy).
- Security posture (identity model, certificate distribution, admission policy).

## Templates

- **Full template**: [`adr-template.md`](./adr-template.md)
- **Short template**: [`adr-short-template.md`](./adr-short-template.md)

## ADR Index

| ADR | Title | Status |
|-----|-------|--------|
| [001](001-azure-paas-for-data.md) | Azure PaaS for OSDU Data Services | Accepted |
| [002](002-aks-automatic.md) | AKS Automatic as Compute Substrate | Accepted |
| [003](003-in-cluster-middleware-scope.md) | In-Cluster Middleware Scope | Accepted |
| [004](004-local-helm-chart-safeguards.md) | Local Helm Chart for Safeguards Compliance | Accepted |
| [005](005-workload-identity.md) | Workload Identity for Azure PaaS Access | Accepted; data-plane boundary revised by [035](035-entra-first-data-plane.md) |
| [006](006-three-namespace-model.md) | Three-Namespace Model | Accepted |
| [007](007-layered-kustomization-ordering.md) | Layered Flux Kustomization Ordering | Accepted; Gateway ownership revised by [036](036-single-gateway-ownership.md) |
| [008](008-bicep-for-azure-provisioning.md) | Bicep for Azure Provisioning (AVM for AKS) | Accepted |
| [009](009-flux-cd-for-gitops.md) | Flux CD + AKS GitOps Extension | Accepted |
| [010](010-keyvault-secret-management.md) | Key Vault + ConfigMap Secret Model | Accepted |
| [011](011-trust-manager-ca-distribution.md) | Cross-Namespace CA Distribution via trust-manager | Accepted |
| [012](012-ingress-profiles.md) | Three Ingress Profiles (azure, dns, ip) | Accepted; Gateway ownership revised by [036](036-single-gateway-ownership.md) |
| [013](013-schema-load-flux-job.md) | Schema Load via a Flux-Managed Job | Accepted |
| [014](014-suspend-gitops-after-deploy.md) | Suspend GitOps Reconciliation After Deploy | Accepted |
| [015](015-partition-entitlements-bootstrap.md) | Partition + Entitlements Bootstrap via a Flux Helm Chart | Accepted; membership seeding extended by [037](037-per-identity-authorization.md) |
| [016](016-istio-jwt-projection.md) | Istio JWT Projection for Azure-Provider OSDU Services | Accepted; projection revised by [037](037-per-identity-authorization.md) |
| [017](017-osdu-image-lock.md) | Per-Deploy Image Lock via ConfigMap + Flux Substitution | Accepted |
| [018](018-karpenter-nodepool-authoring.md) | Karpenter NodePool Authoring as Workload Manifests | Accepted |
| [019](019-kubernetes-136-minimum.md) | Kubernetes 1.36 Minimum for AKS Automatic | Accepted |
| [020](020-osdu-flux-gitops-namespace.md) | SPI-Owned GitOps Objects in a Dedicated osdu-flux Namespace | Accepted |
| [021](021-data-plane-rbac-dual-path.md) | Dual-Path Data-Plane Access: Cosmos/Service Bus RBAC Alongside Keys | Superseded by [035](035-entra-first-data-plane.md) |
| [022](022-spi-pool-node-label.md) | spi-pool as the Workload Node-Placement Label | Accepted |
| [023](023-optional-application-insights.md) | Opt-In Application Insights Provisioning | Accepted; CLI surface completed by [030](030-application-insights-telemetry.md) |
| [024](024-middleware-only-minimal-profile.md) | Middleware-Only `minimal` Profile, Replacing the Unbacked `full` | Accepted |
| [025](025-tls-certificates-in-platform.md) | TLS Certificates in platform with Gateway ReferenceGrants | Accepted |
| [026](026-adme-aligned-integration-tests.md) | ADME-Aligned, Secret-Less Integration Tests on the Deploy Lane | Accepted |
| [027](027-deploy-lane-invariants.md) | Deploy-Lane CI-Mode and Digest-Pin Invariants | Accepted |
| [028](028-aks-base-node-autoprovisioning.md) | AKS Base SKU with Node Autoprovisioning | Superseded by [033](033-selectable-aks-deployment-modes.md) |
| [029](029-disable-local-auth-data-services.md) | Disable Local Auth on Azure Data Services | Accepted; scope narrowed by [035](035-entra-first-data-plane.md) |
| [030](030-application-insights-telemetry.md) | Application Insights for Service Telemetry | Accepted |
| [031](031-record-ingestion-data-plane.md) | Record-Ingestion Data-Plane Enablement | Accepted |
| [032](032-spi-ghcr-service-images.md) | SPI-Built GHCR Images as the Service Baseline | Accepted |
| [033](033-selectable-aks-deployment-modes.md) | Selectable AKS Deployment Modes | Accepted |
| [034](034-runtime-resolved-istio-revision.md) | Runtime-Resolved Managed Istio Revision | Accepted |
| [035](035-entra-first-data-plane.md) | Entra-First Data-Plane Authentication Boundary | Accepted |
| [036](036-single-gateway-ownership.md) | Single Gateway Ownership by the Ingress Profile | Accepted |
| [037](037-per-identity-authorization.md) | Per-Identity Authorization with Explicit Membership Seeding | Accepted |
