---
status: "accepted"
contact: "danielscholl"
date: "2026-07-24"
deciders: "danielscholl"
---

# spi-pool as the Workload Node-Placement Label

## Context and Problem Statement

Karpenter NodePools and workload node selectors previously used the
`agentpool` label to steer middleware onto the `platform` pool and OSDU
services onto the `osdu` pool. AKS restricts `agentpool` as a
reserved system label: NodePool manifests that set it in
`spec.template.metadata.labels` are rejected at admission
(`label "agentpool" is restricted`), which blocked the entire nodepool
layer from reconciling.

## Decision Drivers

- Node placement must keep working across AKS hardening waves; reserved
  system labels can gain restrictions at any time.
- The label is purely internal (nothing outside this repository consumes
  it), so a rename is mechanical.

## Considered Options

- Introduce a stack-owned label (`spi-pool`)
- Use the AKS-managed `kubernetes.azure.com/agentpool` label
- Select nodes via taints/tolerations only, without a label

## Decision Outcome

Chosen option: "`spi-pool`", applied consistently to NodePool templates,
workload `nodeSelector`s, and affinity rules. A stack-owned label can
never collide with platform reservations. The AKS-managed label is set by
the platform, not by Karpenter templates, and taint-only placement loses
the ability to require (rather than merely tolerate) a pool.

### Consequences

- Good, because node placement is decoupled from AKS reserved-label policy.
- Good, because the rename is invisible outside the repository.
- Bad, because operators inspecting nodes must know `spi-pool` is the
  placement label (`kubectl get nodes -L spi-pool`).
