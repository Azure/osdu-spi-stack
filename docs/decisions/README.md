# Architectural Decision Records (ADRs)

An Architectural Decision (AD) is a justified software design choice that addresses a functional or non-functional requirement that is architecturally significant. An Architectural Decision Record (ADR) captures a single AD and its rationale.

## The register model

This directory is a decision register, not a time-boxed log. Each record states the ruling as it stands; git history carries the chronology, authorship, and every prior form. Records therefore have no status, no dates, no deciders, and no amendment trail; a record exists exactly as long as its decision stands.

- **A decision that changes is rewritten in place** through a PR, with the old choice moved to a one-line `Rejected:` entry when the contrast still teaches something.
- **A decision that stops standing alone is folded** into the record that owns its subject, and the corpus is renumbered to stay contiguous, with references updated across the repository.
- **A record must earn its file.** The test is operational: without it, would a competent contributor plausibly re-propose the rejected alternative or walk into the trap it documents? If not, the content belongs in a design doc or in the code.

## How to add an ADR

1. Copy `adr-template.md` to `NNN-title-with-dashes.md`, where NNN is the next number in sequence. Check open PRs so you do not collide on a number.
2. Shape: `# ADR-NNN: Title`, `## Context`, `## Decision` with inline `Rejected:` one-liners, `## Consequences`. No frontmatter.
3. For each rejected option, write one line preserving its real advantage. The decision is what was chosen; alternatives get enough space to show the trade-off, no more.
4. Prose follows [docs/STYLE.md](../STYLE.md): impersonal active voice, claims backed by a named artifact or exact number, no moment-in-time status.

## ADR style

- **No `## Validation` sections.** Phase-by-phase acceptance logs belong in the PR description.
- **No incident narrative in Context.** State the structural problem the decision addresses; triggering incidents and specific clusters age poorly.
- **One-line option rejections.** Write "Rejected: <one clause>" rather than paragraphs re-litigating prior attempts.
- **Consequences mix good and bad unsorted**, and the honest limitation is worth leading with.

## When to create an ADR

Create an ADR for a decision that could plausibly have gone a different way and where the alternative would be defensible:

- Architecture patterns (deployment strategies, dependency ordering, GitOps boundaries).
- Technology choices (middleware selection, operators, provisioning tools).
- Design patterns (namespace model, credential handling, ingress strategy).
- Security posture (identity model, certificate distribution, admission policy).

## ADR Index

Each row states the ruling so the index can answer "what was decided" on its own; open the ADR for the drivers, rejected options, and consequences.

| ADR | Title | Decision |
|-----|-------|----------|
| [001](001-azure-paas-for-data.md) | Azure PaaS for OSDU Data Services | Every data service with a managed equivalent runs as Azure PaaS; the stack is Azure-only by design. |
| [002](002-aks-automatic.md) | AKS Automatic as Compute Substrate | The cluster is AKS Automatic with a Kubernetes 1.36 floor; Karpenter, managed Istio, and Deployment Safeguards come from the platform. |
| [003](003-in-cluster-middleware-scope.md) | In-Cluster Middleware Scope | Exactly three stateful systems run in-cluster: Elasticsearch (ECK), Redis (Bitnami chart), and PostgreSQL for Airflow (CNPG). |
| [004](004-local-helm-chart-safeguards.md) | Local Helm Chart for Safeguards Compliance | One local chart (`osdu-spi-service`) bakes Safeguards compliance into its templates; HelmReleases supply only image, env, and resource overrides. |
| [005](005-workload-identity.md) | Workload Identity for Azure PaaS Access | A UAMI federated with `workload-identity-sa` carries OSDU service PaaS access; `dns` ingress mode adds a separate UAMI for ExternalDNS. No stored credentials. |
| [006](006-three-namespace-model.md) | Three-Namespace Model | Workloads split across `foundation` (operators), `platform` (middleware), and `osdu` (services). |
| [007](007-layered-kustomization-ordering.md) | Layered Flux Kustomization Ordering | The stack reconciles as ordered layers (0a through 6) wired by explicit `dependsOn`; same-layer Kustomizations run in parallel. |
| [008](008-bicep-for-azure-provisioning.md) | Bicep for Azure Provisioning (AVM for AKS) | Azure resources are declared in Bicep, AVM for AKS and hand-written modules elsewhere; the CLI orchestrates only the seams Bicep cannot cover. |
| [009](009-flux-cd-for-gitops.md) | Flux CD + AKS GitOps Extension | The AKS native Flux extension reconciles all in-cluster state from one `fluxConfigurations` with two top-level Kustomizations (stack, ingress). |
| [010](010-keyvault-secret-management.md) | Key Vault + ConfigMap Secret Model | Secrets split by class: Entra tokens for PaaS access, Key Vault for stored values, CLI-generated Kubernetes Secrets for in-cluster middleware. |
| [011](011-trust-manager-ca-distribution.md) | Cross-Namespace CA Distribution via trust-manager | trust-manager Bundles mirror the Redis and Elasticsearch CAs into `osdu`; a DestinationRule disables Istio mTLS toward Redis. |
| [012](012-ingress-profiles.md) | Three Ingress Profiles (azure, dns, ip) | Ingress is one of three self-contained Flux trees selected by `--ingress-mode`; `azure` (Azure-assigned hostname) is the default. |
| [013](013-schema-load-flux-job.md) | Schema Load via a Flux-Managed Job | A Flux-managed Job runs the community schema-load image under `workload-identity-sa` against the in-cluster schema service. |
| [014](014-suspend-gitops-after-deploy.md) | Suspend GitOps Reconciliation After Deploy | `spi up` ends by suspending the Flux GitRepository; the environment stays pinned to its deploy commit until `spi reconcile`. |
| [015](015-partition-entitlements-bootstrap.md) | Partition, Entitlements, and Legal Bootstrap via a Flux Helm Chart | A Flux Helm chart renders per-partition one-shot Jobs that create the partition record, provision entitlements root groups, and seed the default legal tag. |
| [016](016-istio-jwt-projection.md) | Istio JWT Projection for Azure-Provider OSDU Services | RequestAuthentication plus a Lua EnvoyFilter validate AAD JWTs and project the `x-app-id` and `x-user-id` headers the Azure provider expects. |
| [017](017-osdu-image-lock.md) | Per-Deploy Image Lock via ConfigMap + Flux Substitution | Images resolve per origin into the `osdu-image-lock` ConfigMap and Flux substitutes them, by digest when one is recorded; pins overlay the lock, refreshes preserve them, and only `--refresh-images` moves an existing lock. |
| [018](018-karpenter-nodepool-authoring.md) | Karpenter NodePool Authoring as Workload Manifests | NodePools and AKSNodeClasses are Flux-managed manifests: tainted `platform` and `osdu` pools selected by the stack-owned `spi-pool` label. |
| [019](019-osdu-flux-gitops-namespace.md) | SPI-Owned GitOps Objects in a Dedicated osdu-flux Namespace | The CLI creates `osdu-flux` at bootstrap and seeds all SPI-owned GitOps ConfigMaps and Secrets there. |
| [020](020-optional-application-insights.md) | Opt-In Application Insights Provisioning | Application Insights and Log Analytics deploy only when `enableApplicationInsights` is set; the default is off. |
| [021](021-middleware-only-minimal-profile.md) | Middleware-Only `minimal` Profile, Replacing the Unbacked `full` | Profile values are `bare`, `minimal`, and `core`; the unbacked `full` value is removed. |
| [022](022-tls-certificates-in-platform.md) | TLS Certificates in platform with Gateway ReferenceGrants | Certificates issue into `platform`; ReferenceGrants let the Gateway in the managed namespace read the secrets. |
| [023](023-entra-only-data-plane.md) | Entra-Only Data Plane: Disable Local Auth on Cosmos and Service Bus | `disableLocalAuth: true` on every Cosmos account and Service Bus namespace; Workload Identity is the only data-plane path. |
| [024](024-windows-batch-shim-launcher.md) | Windows Batch Shim Launcher via an Escaped cmd.exe Command Line | Batch shims launch through one escaped `cmd.exe` command line at the single `spi.shell.run_process` chokepoint. |
| [025](025-single-flux-inventory-owner.md) | One Flux Inventory Owner per Kubernetes Object | Every object has exactly one Flux owner; all ingress modes render the Gateway under the same `spi-gateway-tls` name. |
| [026](026-bind-managed-istio-ingress.md) | Bind to the AKS Managed Istio Ingress | Ingress binds to the AKS-provisioned external ingress Service; its DNS label is applied via Flux, not the CLI. |
| [027](027-subscription-resolved-availability-zones.md) | Subscription-Resolved System Pool Availability Zones | The CLI resolves the system pool's usable zones from the subscription's SKU catalogue before deploying; a restricted or missing zone is a preflight CLI error, not an ARM failure. |
| [028](028-version-pinned-shared-environment.md) | Version-Pinned Shared Backing Environment | The shared environment for fork CI deploys a release tag, never a branch; its declaration is one reviewed file (`ops/environments/shared.yaml`), and a release opens the bump PR automatically. |
| [029](029-environment-lifecycle-and-reset-boundary.md) | Environment Lifecycle Verbs and the Reset Boundary | Refresh changes runtime state, upgrade is `spi up` re-run at the new tag, and only the weekly full reset clears both cluster and PaaS state; lifecycle workflows own the fail-closed `maintenance` flag. |
| [030](030-machine-readable-status-contract.md) | Machine-Readable Status and the Deploy Record | `spi status --json` emits a versioned envelope whose exit code gates on `deployable`, with `ready` for convergence as a field; `spi up` records the deployed version in RG tags and a `spi-deploy-record` ConfigMap. |
| [031](031-fork-image-deploys-as-ephemeral-pins.md) | Fork-Built Images Deploy as Ephemeral Lock Pins | A fork deploy is a GHCR digest pin on the image lock, marked ephemeral and owned by its workflow run; restore and the weekday sweep act only on pins proven theirs or stale. |
| [032](032-per-fork-deploy-identity.md) | Per-Fork Deploy Identity and Namespace RBAC | Each fork gets a UAMI whose federated credential trusts the fork's protected deploy environment; RoleBindings name each identity explicitly, with reads split from a `resourceNames`-scoped lock write and no create, delete, or secrets verbs. |
| [033](033-canonical-image-source-follows-onboarding.md) | Canonical Image Source Follows Onboarding | Setting `github_repo` on a service's registry entry flips its canonical image from community GitLab to the fork's GHCR `main` line, one service at a time as its deploy gates activate. |
