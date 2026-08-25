---
status: "proposed"
contact: "danielscholl"
date: "2026-08-24"
deciders: "danielscholl"
---

# Bind to the AKS Managed Istio Ingress

## Context and Problem Statement

AKS managed Istio provisions the external ingress workload and LoadBalancer
Service, but does not deploy a workload for each Gateway API `Gateway` in this
configuration. A Gateway infrastructure annotation therefore has no generated
Service to reach, and an unbound Gateway requests a nonexistent
`<gateway-name>-istio` Service.

Azure mode additionally needs a DNS label on the Service so the public IP
gains its `<label>.<region>.cloudapp.azure.com` FQDN. AKS Automatic blocks
both imperative paths to that label: the
`aks-managed-protect-system-namespaces` admission policy denies writes in
`aks-istio-ingress` to every identity except an exempt list that includes the
`flux-system` service accounts, and the node resource group deny assignment
blocks writing the public IP resource directly.

## Decision Drivers

- Azure mode must configure the existing public IP on fresh and live clusters.
- Ingress modes must share one stable Gateway and LoadBalancer.
- The stack must not own add-on workloads or a second public IP.
- Add-on upgrades must not create competing Flux ownership.
- Every write must pass AKS Automatic admission and authorization controls.

## Considered Options

- Bind to the add-on's external ingress Service; apply its DNS label via Flux.
- Bind to the add-on's Service; annotate it imperatively from the CLI.
- Configure the DNS label on the public IP resource in the node resource group.
- Deploy a second stack-owned ingress workload and LoadBalancer.
- Enable Istio Gateway API automated deployment.

## Decision Outcome

Chosen option: "Bind to the add-on's external ingress Service; apply its DNS
label via Flux", because AKS already provisions
`aks-istio-ingress/aks-istio-ingressgateway-external` as the supported
external ingress endpoint and the Flux extension controllers are the one
identity AKS Automatic permits to write there.

The Gateway uses a `Hostname` address for that Service. In Azure mode the
`spi-ingress-dns-label` Kustomization server-side applies the
`azure-dns-label-name` annotation onto the Service from a partial manifest
(`software/components/azure-dns-label/`); Flux owns only that annotation,
never the Service spec. Reconciliation converges existing clusters whose
public IP has no label. DNS and IP modes bind to the same Service but do not
mutate its annotations.

The Service remains owned by the AKS add-on, not Flux; the manifest carries
the Flux prune-disabled marker and its Kustomization sets `prune: false`, so
no mode switch or stack removal can delete the Service. Normal add-on
revision upgrades retain the external ingress Service while replacing its
backing workload, so its annotation remains in place. If the add-on recreates
the Service, the next reconciliation restores the annotation.

Rejected: imperative CLI annotation fails admission; the
`aks-managed-protect-system-namespaces` policy denies the write and
impersonating an exempt identity is itself blocked by AKS Automatic
authorization.

Rejected: writing the public IP resource fails authorization; the node
resource group deny assignment overrides the deployer's role assignments.

Rejected: a second stack-owned ingress duplicates the add-on workload and
public IP.

Rejected: automated deployment is not enabled by the managed Istio
configuration used by AKS Automatic, so no per-Gateway Service is created.

### Consequences

- Good, because Azure mode reuses the public IP already provisioned by AKS.
- Good, because mode switches keep the same Gateway, Service, and Flux owner.
- Good, because every write passes admission under a supported exemption.
- Bad, because Flux claims one annotation on an add-on-owned object, and the
  prune-disabled marker is all that separates a mode switch from deleting it.

This supersedes the ingress Service topology in ADR-012.
