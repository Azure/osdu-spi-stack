# ADR-026: Automated Gateway Deployment in platform

## Context

AKS managed Istio runs the Gateway API deployment controller: a `Gateway` of class `istio` in a stack-owned namespace gets its own workload and LoadBalancer Service, `<gateway>-istio`, rendered from the add-on's template, and the `aks-managed-restrict-istio-gateway-deployments` admission policy holds that workload to the add-on's images, a baseline security context, and a ban on protected namespaces. The add-on's own external ingress Service in `aks-istio-ingress` sits behind the `aks-managed-protect-system-namespaces` policy, which denies every write from identities outside an AKS-maintained exemption list; the Flux controllers are not on it, and `azure-dns-label-name` is not among the annotations AKS supports on that Service. Azure mode needs that annotation on whichever LoadBalancer Service fronts the Gateway so the public IP gains its `<label>.<region>.cloudapp.azure.com` FQDN, and the node resource group deny assignment blocks setting the label on the public IP resource directly.

## Decision

The Gateway lives in `platform` with no `addresses`, and Istio's automated deployment provisions `spi-gateway-istio` from it (`software/components/gateway/`). Azure mode carries the DNS label in `spec.infrastructure.annotations`, which the controller copies onto the generated Service (`software/overlays/gateway-tls-single-host/`); DNS and IP modes add nothing there. The add-on's external ingress gateway is disabled in `infra/aks.bicep`, so a cluster runs one ingress LoadBalancer and an existing cluster releases its DNS label from the add-on's public IP for the Gateway's public IP to claim. Routes, ClusterIssuer solvers, and certificates all name the Gateway in `platform`; no stack-owned object lives in a managed namespace.

A cluster deployed while the Gateway lived in `aks-istio-ingress` keeps that object, because admission denies Flux the delete as well as the update. Its owner, `spi-gateway-tls`, retires into the ADR-025 handoff shape (an empty path, `prune: false`, `deletionPolicy: Orphan`), so the inventory that lists the old Gateway is never pruned, on a spec change or on the Kustomization's own deletion, and the new owner `spi-ingress-gateway` renders the Gateway with pruning intact. A fresh cluster carries the retired name as an empty inventory.

Rejected: bind the Gateway to the add-on's external ingress Service by `Hostname` address and let Flux apply the DNS label there. Reuses the public IP AKS already provisioned, but every write to that Service passes an exemption list the stack does not control.

Rejected: set the DNS label on the public IP resource in the node resource group. The deny assignment overrides the deployer's role assignments.

Rejected: annotate the Service imperatively from the CLI. The admission policy denies the write, and impersonating an exempt identity is itself blocked.

## Consequences

- The public IP changes when an existing cluster upgrades: the add-on's Service and public IP go when Bicep disables the gateway, and the FQDN resolves again once the Gateway's Service has claimed the label. Azure mode clients follow the FQDN; `ip` mode clients re-read the address.
- Every write the ingress needs lands in `platform`, where Flux, cert-manager, and the deployer hold ordinary RBAC; the ingress no longer depends on an AKS exemption list.
- The gateway workload runs under the add-on's template on the stack's nodes; replicas, affinity, and Service fields are tunable only within the AKS allow list, through a `spec.infrastructure.parametersRef` ConfigMap.
- The legacy Gateway, and the ReferenceGrant that served it, stay on clusters that predate the move until the cluster is recreated; the retired `spi-gateway-tls` handoff stays in every tree until the rollout that removes it.
