# ADR-026: Bind to the AKS Managed Istio Ingress

## Context

AKS managed Istio provisions the external ingress workload and its LoadBalancer Service (`aks-istio-ingress/aks-istio-ingressgateway-external`) but does not deploy a workload per Gateway API `Gateway`; an unbound Gateway requests a nonexistent `<gateway-name>-istio` Service. Azure mode also needs a DNS label on that Service so the public IP gains its `<label>.<region>.cloudapp.azure.com` FQDN, and AKS Automatic blocks both imperative paths to it: the `aks-managed-protect-system-namespaces` admission policy denies writes in `aks-istio-ingress` to every identity except an exempt list that includes the `flux-system` service accounts, and the node resource group deny assignment blocks writing the public IP resource directly.

## Decision

Bind the Gateway to the add-on's external ingress Service with a `Hostname` address, and let Flux, the one identity AKS Automatic permits to write in `aks-istio-ingress`, apply the DNS label. In Azure mode the `spi-ingress-dns-label` Kustomization server-side applies the `azure-dns-label-name` annotation from a partial manifest (`software/components/azure-dns-label/`); Flux owns that one annotation, never the Service spec. DNS and IP modes bind to the same Service without touching its annotations.

The Service stays owned by the AKS add-on. Its manifest carries the Flux prune-disabled marker and the Kustomization sets `prune: false`, so no mode switch or stack removal can delete it. Add-on revision upgrades retain the Service while replacing its backing workload; if the add-on recreates the Service, the next reconciliation restores the annotation.

Rejected: annotate the Service imperatively from the CLI. The admission policy denies the write, and impersonating an exempt identity is itself blocked.

Rejected: set the DNS label on the public IP resource in the node resource group. The deny assignment overrides the deployer's role assignments.

Rejected: deploy a second stack-owned ingress workload and LoadBalancer. Duplicates the add-on workload and public IP.

Rejected: Istio Gateway API automated deployment. Not enabled in the managed Istio configuration AKS Automatic ships, so no per-Gateway Service is created.

## Consequences

- Azure mode reuses the public IP AKS already provisioned, and mode switches keep the same Gateway, Service, and Flux owner.
- Every write passes admission under a supported exemption.
- Flux claims one annotation on an add-on-owned object; the prune-disabled marker is what separates a mode switch from deleting the Service.
