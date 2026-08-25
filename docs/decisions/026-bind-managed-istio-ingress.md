# ADR-026: Bind to the AKS Managed Istio Ingress

## Context

AKS managed Istio provisions the external ingress workload and LoadBalancer Service, but does not deploy a workload for each Gateway API `Gateway` in this configuration. A Gateway infrastructure annotation therefore has no generated Service to reach, and an unbound Gateway requests a nonexistent `<gateway-name>-istio` Service.

Azure mode additionally needs a DNS label on the Service so the public IP gains its `<label>.<region>.cloudapp.azure.com` FQDN. AKS Automatic blocks both imperative paths to that label: the `aks-managed-protect-system-namespaces` admission policy denies writes in `aks-istio-ingress` to every identity except an exempt list that includes the `flux-system` service accounts, and the node resource group deny assignment blocks writing the public IP resource directly.

Azure mode must configure the existing public IP on fresh and live clusters, ingress modes must share one stable Gateway and LoadBalancer, the stack must not own add-on workloads or a second public IP, add-on upgrades must not create competing Flux ownership, and every write must pass AKS Automatic admission and authorization controls.

## Decision

Bind to the add-on's external ingress Service and apply its DNS label via Flux. AKS already provisions `aks-istio-ingress/aks-istio-ingressgateway-external` as the supported external ingress endpoint, and the Flux extension controllers are the one identity AKS Automatic permits to write there.

The Gateway uses a `Hostname` address for that Service. In Azure mode the `spi-ingress-dns-label` Kustomization server-side applies the `azure-dns-label-name` annotation onto the Service from a partial manifest (`software/components/azure-dns-label/`); Flux owns only that annotation, never the Service spec. Reconciliation converges existing clusters whose public IP has no label. DNS and IP modes bind to the same Service but do not mutate its annotations.

The Service remains owned by the AKS add-on, not Flux; the manifest carries the Flux prune-disabled marker and its Kustomization sets `prune: false`, so no mode switch or stack removal can delete the Service. Normal add-on revision upgrades retain the external ingress Service while replacing its backing workload, so its annotation remains in place. If the add-on recreates the Service, the next reconciliation restores the annotation.

Rejected: annotate the Service imperatively from the CLI. The `aks-managed-protect-system-namespaces` policy denies the write, and impersonating an exempt identity is itself blocked by AKS Automatic authorization.

Rejected: configure the DNS label on the public IP resource in the node resource group. The deny assignment overrides the deployer's role assignments.

Rejected: deploy a second stack-owned ingress workload and LoadBalancer. Duplicates the add-on workload and public IP.

Rejected: enable Istio Gateway API automated deployment. Not enabled by the managed Istio configuration used by AKS Automatic, so no per-Gateway Service is created.

## Consequences

- Azure mode reuses the public IP already provisioned by AKS.
- Mode switches keep the same Gateway, Service, and Flux owner.
- Every write passes admission under a supported exemption.
- Flux claims one annotation on an add-on-owned object, and the prune-disabled marker is what separates a mode switch from deleting it.
