# ADR-018: Karpenter NodePool Authoring as Workload Manifests

**Status**: Accepted

## Context

AKS Automatic ships Karpenter (Node Auto-Provisioning) and disables the classic AKS agent-pool model. Compute capacity is declared via `karpenter.sh/v1.NodePool` and `karpenter.azure.com/v1beta1.AKSNodeClass` Custom Resources. Authoring those CRs is a placement decision: they could live in Bicep alongside the cluster (`infra/aks.bicep`), be applied imperatively by the CLI during bootstrap, or live as workload manifests under `software/` and reconcile through Flux like everything else in the cluster.

The placement choice matters because the same CRs need to evolve with workload shape: SPI Stack runs platform middleware (ECK, CNPG, Redis, Airflow) on one taint domain and OSDU services on another, so the NodePools change when the workload mix changes, not when the cluster shape changes.

## Decision

Author Karpenter `NodePool` and `AKSNodeClass` resources as Flux-managed workload manifests in `software/components/nodepools/`, reconciled by the `spi-nodepools` Kustomization at Layer 0b of the core profile (after `spi-namespaces`, before any layer that schedules workloads).

Two NodePools:

- `platform`: taint `workload=platform:NoSchedule`, requirements `Dsv5` family, 8 vCPU, >30 GiB RAM, premium-capable, on-demand. Hosts stateful middleware.
- `osdu`: same shape, taint `workload=osdu:NoSchedule`. Hosts OSDU services.

Both pin `AKSNodeClass.imageFamily: AzureLinux` with a 128 GiB OS disk. Disruption uses `WhenEmptyOrUnderutilized` with a 5-minute consolidation delay.

Rejected:

- **Declare NodePools in Bicep alongside the AKS cluster.** Bicep would have to either embed the CR as a `Microsoft.Resources/deployments` JSON blob (loses CR-level review) or call a `kubernetesClusterExtension`-style escape hatch. Either way the NodePool evolution is gated on a Bicep deploy when it should track workload evolution.
- **Apply NodePools imperatively from the CLI at bootstrap.** Re-opens the problem ADR-009 closed for everything else: cluster state stops being reconstructable from Git, and a NodePool tweak requires the CLI to run.
- **One shared NodePool.** Removes the workload-isolation guarantee. A platform middleware burst (PostgreSQL replica rebuild, ES JVM heap pressure) would compete with OSDU service scaling on the same nodes; the taints are how we keep those domains separate.

## Consequences

- NodePool changes flow through the same GitOps loop as every other workload manifest: PR, review, Flux reconcile. No CLI or Bicep redeploy.
- Workload isolation is enforced at the scheduler. Platform pods declare `tolerations` and `nodeSelector: spi-pool=platform` (label renamed from `agentpool`, see [ADR-022](022-spi-pool-node-label.md)) in their charts; OSDU services declare the matching osdu pair. Mis-tolerated pods stay `Pending` rather than landing on the wrong pool.
- The Layer 0b position means NodePools are present before Layer 1 operators reconcile, so the first ECK or CNPG pod schedules on the correct pool without a Karpenter cold-start delay against unlabeled nodes.
- Adding a new workload domain (e.g., a future ingest pool) is a new NodePool + AKSNodeClass pair under `software/components/nodepools/` and a chart-level toleration. No infra-side change.
- The disruption settings (`WhenEmptyOrUnderutilized`, 5 min) are tuned for dev/test churn. Production tuning is unvalidated here; the expected direction is longer windows and `WhenEmpty` only. Either way it is a tuning concern, not a structural change.
