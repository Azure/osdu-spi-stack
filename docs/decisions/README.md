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
5. Decisions can be changed later. Reversing or materially changing an accepted decision needs a new ADR that supersedes the old one; do not silently rewrite a closed decision in place. A small additive adjustment that extends a decision without contradicting it (a later profile, flag, or value) may instead be recorded as a short dated amendment note in the existing ADR, since a whole new ADR for a one-line change is disproportionate.

## ADR Style

ADRs record decisions, not engineering logs. Keep them short and forward-facing so a reader can grok the decision in a single pass. The prose style (voice, word list, review checklist) is fixed in [docs/STYLE.md](../STYLE.md); the structural rules below complement it.

- **No `## Validation` sections.** Phase-by-phase acceptance logs belong in the PR description, not in the ADR.
- **Amend only for small additive notes; supersede for real changes.** A decision that is reversed or materially changed gets a new ADR that supersedes it, not a rewrite of the closed record. A minor additive extension that does not contradict the decision (for example a later profile that reuses the same pattern) may be captured as a brief dated amendment note, a line or two, in place rather than a full new ADR.
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
| [005](005-workload-identity.md) | Workload Identity for Azure PaaS Access | Accepted |
| [006](006-three-namespace-model.md) | Three-Namespace Model | Accepted |
| [007](007-layered-kustomization-ordering.md) | Layered Flux Kustomization Ordering | Accepted |
| [008](008-bicep-for-azure-provisioning.md) | Bicep for Azure Provisioning (AVM for AKS) | Accepted |
| [009](009-flux-cd-for-gitops.md) | Flux CD + AKS GitOps Extension | Accepted |
| [010](010-keyvault-secret-management.md) | Key Vault + ConfigMap Secret Model | Accepted |
| [011](011-trust-manager-ca-distribution.md) | Cross-Namespace CA Distribution via trust-manager | Accepted |
| [012](012-ingress-profiles.md) | Three Ingress Profiles (azure, dns, ip) | Accepted |
| [013](013-schema-load-flux-job.md) | Schema Load via a Flux-Managed Job | Accepted |
| [014](014-suspend-gitops-after-deploy.md) | Suspend GitOps Reconciliation After Deploy | Accepted |
| [015](015-partition-entitlements-bootstrap.md) | Partition + Entitlements Bootstrap via a Flux Helm Chart | Accepted |
| [016](016-istio-jwt-projection.md) | Istio JWT Projection for Azure-Provider OSDU Services | Accepted |
| [017](017-osdu-image-lock.md) | Per-Deploy Image Lock via ConfigMap + Flux Substitution | Accepted |
| [018](018-karpenter-nodepool-authoring.md) | Karpenter NodePool Authoring as Workload Manifests | Accepted |
| [019](019-kubernetes-136-minimum.md) | Kubernetes 1.36 Minimum for AKS Automatic | Accepted |
| [020](020-osdu-flux-gitops-namespace.md) | SPI-Owned GitOps Objects in a Dedicated osdu-flux Namespace | Accepted |
| [021](021-data-plane-rbac-dual-path.md) | Dual-Path Data-Plane Access: Cosmos/Service Bus RBAC Alongside Keys | Superseded by [027](027-entra-only-data-plane.md) |
| [022](022-spi-pool-node-label.md) | spi-pool as the Workload Node-Placement Label | Accepted |
| [023](023-optional-application-insights.md) | Opt-In Application Insights Provisioning | Accepted |
| [024](024-middleware-only-minimal-profile.md) | Middleware-Only `minimal` Profile, Replacing the Unbacked `full` | Accepted |
| [025](025-tls-certificates-in-platform.md) | TLS Certificates in platform with Gateway ReferenceGrants | Accepted |
| [026](026-airflow-3.md) | Airflow 3, Single-Engine | Accepted |
| [027](027-entra-only-data-plane.md) | Entra-Only Data Plane: Disable Local Auth on Cosmos and Service Bus | Accepted |
| [028](028-windows-batch-shim-launcher.md) | Windows Batch Shim Launcher via an Escaped cmd.exe Command Line | Accepted |
