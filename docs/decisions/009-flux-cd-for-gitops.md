# ADR-009: Flux CD + AKS GitOps Extension for In-Cluster Reconciliation

## Context

Azure provisioning lands declaratively via Bicep (ADR-008); the cluster still has to be populated with operators, middleware, ingress, and OSDU services. The CLI could apply those as imperative `kubectl` calls, but then cluster state is not reconstructable from Git, upgrades require the CLI to be in a specific version, and every change is a CLI change.

Flux CD is the canonical Kubernetes GitOps tool: `GitRepository` sources, `Kustomization` and `HelmRelease` controllers, `dependsOn` for ordering. AKS offers Flux as a native cluster extension managed by Azure.

## Decision

All in-cluster state is reconciled by Flux CD, installed as an AKS native extension. The extension and its `fluxConfigurations` resource are declared in `infra/flux.bicep` and deployed after K8s bootstrap.

Shape:

- **One `fluxConfigurations` resource** (`osdu-spi-stack-system`) pointing at the repo's `main` branch with a 10-minute sync interval.
- **Two top-level Kustomizations** under that configuration:
  - `stack` reconciling `./software/stacks/osdu/profiles/<profile>` (the layered core profile, ADR-007).
  - `ingress` reconciling `./software/stacks/osdu/ingress/<mode>` (one of `azure`, `dns`, `ip`, ADR-012).
- **Sync namespace `osdu-flux`.** Created by the CLI at bootstrap and referenced by `fluxConfigurations.namespace` in `infra/flux.bicep`; the Flux controllers run in `flux-system`, owned by the AKS extension (ADR-019).
- **Profile and ingress mode are Bicep parameters.** Switching either re-deploys `flux.bicep` and the extension drift-reconciles the new paths; no hand-edit of in-cluster resources.

The CLI's `spi reconcile` is a thin wrapper over `flux reconcile` plus `--suspend` / `--resume` for freezing and unfreezing the `GitRepository`.

Rejected:
- **`flux install` plus a manual `GitRepository` YAML.** Works, but the AKS extension handles Flux component upgrades as an Azure-managed concern, which is one less thing to patch.
- **One monolithic Kustomization.** Separating `stack` from `ingress` lets ingress-mode switches (ADR-012) avoid re-reconciling the entire service graph.
- **Continuous auto-reconciliation on every commit.** Safe for production, a liability for a dev/test stack. Users can `spi reconcile --suspend` to pin an environment after a deploy.

## Consequences

- Cluster state is reconstructable from Git. A fresh `spi up` against an existing repo reproduces the same end state.
- Ordering between layers, between profile and ingress, and between services and schema-load is declarative and visible (`flux get kustomizations`).
- Flux component upgrades (source-controller, kustomize-controller, helm-controller) move with the AKS extension; we do not pin Flux versions.
- Every in-cluster change is a Git commit. Urgent hotfixes are `kubectl apply` plus a follow-up PR, not persistent state.
- Ingress mode and profile switches are Bicep re-deploys that mutate one path each. The CLI does not have to delete and recreate Kustomizations to switch modes.
