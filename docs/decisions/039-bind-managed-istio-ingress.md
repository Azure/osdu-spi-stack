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

## Decision Drivers

- Azure mode must configure the existing public IP on fresh and live clusters.
- Ingress modes must share one stable Gateway and LoadBalancer.
- The stack must not own add-on workloads or a second public IP.
- Add-on upgrades must not create competing Flux ownership.

## Considered Options

- Bind to and configure the add-on's external ingress Service.
- Deploy a second stack-owned ingress workload and LoadBalancer.
- Enable Istio Gateway API automated deployment.

## Decision Outcome

Chosen option: "Bind to and configure the add-on's external ingress Service",
because AKS already provisions
`aks-istio-ingress/aks-istio-ingressgateway-external` as the supported external
ingress endpoint.

The Gateway uses a `Hostname` address for that Service. During every Azure-mode
`spi up`, the CLI applies `azure-dns-label-name` directly to the Service before
Flux activation. This converges existing clusters whose public IP has no label.
DNS and IP modes bind to the same Service but do not mutate its annotations.

The Service remains owned by the AKS add-on, not Flux. Normal add-on revision
upgrades retain the external ingress Service while replacing its backing
workload, so its annotation remains in place. If the add-on recreates the
Service, the next `spi up --ingress-mode azure` restores the annotation.

Rejected: a second stack-owned ingress duplicates the add-on workload and
public IP.

Rejected: automated deployment is not enabled by the managed Istio
configuration used by AKS Automatic, so no per-Gateway Service is created.

### Consequences

- Good, because Azure mode reuses the public IP already provisioned by AKS.
- Good, because mode switches keep the same Gateway, Service, and Flux owner.
- Good, because no Flux inventory claims an add-on-owned object.
- Bad, because the CLI must make one supported imperative Service
  customization during each Azure-mode deployment.

This supersedes the ingress Service topology in ADR-012.
