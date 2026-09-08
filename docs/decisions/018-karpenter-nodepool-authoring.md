# ADR-018: Karpenter NodePool Authoring as Workload Manifests

## Context

AKS Automatic ships Karpenter (Node Auto-Provisioning) and disables the classic AKS agent-pool model. Compute capacity is declared via `karpenter.sh/v1.NodePool` and `karpenter.azure.com/v1beta1.AKSNodeClass` Custom Resources. Authoring those CRs is a placement decision: they could live in Bicep alongside the cluster (`infra/aks.bicep`), be applied imperatively by the CLI during bootstrap, or live as workload manifests under `software/` and reconcile through Flux like everything else in the cluster.

The placement choice matters because the same CRs need to evolve with workload shape: SPI Stack runs platform middleware (ECK, CNPG, Redis, Airflow) on one taint domain and OSDU services on another, so the NodePools change when the workload mix changes, not when the cluster shape changes.

## Decision

Author Karpenter `NodePool` and `AKSNodeClass` resources as Flux-managed workload manifests in `software/components/nodepools/`, reconciled by the `spi-nodepools` Kustomization at Layer 0b of the core profile (after `spi-namespaces`, before any layer that schedules workloads).

Two NodePools, shaped by the class of workload each hosts:

- `platform`: taint `workload=platform:NoSchedule`, requirements `D` family, 8 vCPU, >30 GiB RAM, premium-capable, on-demand. Hosts stateful middleware. Disruption is `WhenEmpty` with a 5-minute delay and a budget of one node at a time.
- `osdu`: taint `workload=osdu:NoSchedule`, requirements `D` family, 4, 8, or 16 vCPU, spot or on-demand. Hosts OSDU services. Disruption is `WhenEmptyOrUnderutilized` with a 5-minute delay.

Both pin `AKSNodeClass.imageFamily: AzureLinux` with a 128 GiB OS disk.

The shapes differ because the pools hold opposite workloads. Platform pods are bound to zonal PersistentVolumes and guarded by PodDisruptionBudgets, so relocating one means a permitted eviction, a same-zone replacement node, and a disk reattachment; `WhenEmpty` avoids that churn for a bin-packing gain that is small on a pool of a few hosts, and a fixed 8 vCPU shape keeps the per-node kubelet and DaemonSet reservation amortized over the few hosts the pool needs. OSDU services are stateless HTTP workloads behind Istio with no persistent volumes (the reference services mount only `emptyDir` scratch space), so a range of sizes lets Karpenter bin-pack them and replace an underutilized node with a smaller one, and spot capacity with on-demand fallback prices them accordingly.

Elasticsearch, PostgreSQL, and Redis each declare a required hostname anti-affinity across their own members (`software/components/{elasticsearch,postgres,redis}/`). Setting any `affinity` block on an ECK pod template replaces the operator's default preferred spread, and Karpenter relaxes preferred rules it cannot satisfy, so only a required rule keeps a quorum off a single host. The rule is hostname, not zone: a zone requirement would strand pods whose volumes were provisioned in one zone.

Both pools exclude the non-zonal offering, which Karpenter exposes as zone `0`. A pod that lands on such a node gets a non-zonal PersistentVolume, and Azure will not attach that disk to a zonal VM, so the pod can only ever reschedule onto another non-zonal node. Excluding the value keeps the manifest region-independent; listing the usable zones would not.

Rejected:

- **Identical shapes for both pools.** One review surface, but it pins a stateless pool to an 8 vCPU on-demand floor chosen for JVM-heavy middleware and disables the bin-packing Karpenter exists for.
- **Spot capacity on `platform`.** Same saving as on `osdu`, but a spot eviction ignores PodDisruptionBudgets and a zonal volume cannot follow its pod to whichever zone has spot capacity.
- **Preferred anti-affinity for the stateful sets.** Schedules even when hosts are scarce, but the scheduler and Karpenter both treat it as a score, and a co-located quorum makes its host permanently undrainable.

Placement rides the stack-owned `spi-pool` label, applied consistently to NodePool templates, workload `nodeSelector`s, and affinity rules. AKS reserves `agentpool` as a system label: NodePool manifests that set it are rejected at admission (`label "agentpool" is restricted`), and reserved labels can gain restrictions in any hardening wave. A stack-owned label cannot collide with platform reservations; the AKS-managed `kubernetes.azure.com/agentpool` is set by the platform, not by Karpenter templates, and taint-only placement loses the ability to require (rather than merely tolerate) a pool.

Rejected placements:

- **Declare NodePools in Bicep alongside the AKS cluster.** Bicep would have to either embed the CR as a `Microsoft.Resources/deployments` JSON blob (loses CR-level review) or call a `kubernetesClusterExtension`-style escape hatch. Either way the NodePool evolution is gated on a Bicep deploy when it should track workload evolution.
- **Apply NodePools imperatively from the CLI at bootstrap.** Re-opens the problem ADR-009 closed for everything else: cluster state stops being reconstructable from Git, and a NodePool tweak requires the CLI to run.
- **One shared NodePool.** Removes the workload-isolation guarantee. A platform middleware burst (PostgreSQL replica rebuild, ES JVM heap pressure) would compete with OSDU service scaling on the same nodes; the taints keep those domains separate.

## Consequences

- NodePool changes flow through the same GitOps loop as every other workload manifest: PR, review, Flux reconcile. No CLI or Bicep redeploy.
- Workload isolation is enforced at the scheduler. Platform pods declare `tolerations` and `nodeSelector: spi-pool=platform` in their charts; OSDU services declare the matching osdu pair. Mis-tolerated pods stay `Pending` rather than landing on the wrong pool.
- The Layer 0b position means NodePools are present before Layer 1 operators reconcile, so the first ECK or CNPG pod schedules on the correct pool without a Karpenter cold-start delay against unlabeled nodes.
- Adding a new workload domain (e.g., a future ingest pool) is a new NodePool + AKSNodeClass pair under `software/components/nodepools/` and a chart-level toleration. No infra-side change.
- Operators inspecting nodes must know `spi-pool` is the placement label (`kubectl get nodes -L spi-pool`).
- The required anti-affinity sets a floor of three `platform` hosts, one per Elasticsearch member. It gives host-level redundancy only: volumes stay in the zone they were first bound in, so members whose volumes share a zone stay in that zone until their claims are recreated.
- `WhenEmpty` never moves a running pod, so a pool that sprawled before the anti-affinity landed keeps its extra hosts until an operator drains them once.
- A spot eviction on `osdu` leaves a single-replica service unavailable until its replacement passes the chart's startup delay. Entitlements, partition, and legal run two replicas because every other service depends on them. Spot nodes carry the `kubernetes.azure.com/scalesetpriority=spot:NoSchedule` taint, which the service releases tolerate and the Jobs (`schema-load`, the `osdu-spi-init` chart) deliberately do not: an eviction counts as a failed attempt against their `backoffLimit`, and Karpenter provisions on-demand for a pod that lacks the toleration. A Job template cannot change on a running stack without recreating the Job, so the omission is the mechanism. A production overlay that cannot accept the remaining exposure pins `osdu` to on-demand.
- The 5-minute consolidation delays are tuned for dev/test churn. Production tuning is unvalidated here; the expected direction is longer windows.
