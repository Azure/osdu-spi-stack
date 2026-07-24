---
status: "accepted"
contact: "danielscholl"
date: "2026-07-24"
deciders: "SPI Stack maintainers"
---

# TLS Certificates in platform with Gateway ReferenceGrants

## Context and Problem Statement

The gateway TLS overlays declared their cert-manager `Certificate` resources in `aks-istio-ingress`, next to the Gateway whose listeners consume the secrets. On AKS Automatic that namespace is managed: the `aks-managed-protect-system-namespaces` ValidatingAdmissionPolicy denies writes to it from non-exempt identities. Flux's controllers are exempt (ADR-020), so the Certificate *applied* cleanly, but cert-manager's controller is not exempt, so its every status update was denied. No CertificateRequest, no ACME order, no secret; the HTTPS:443 listener never opened while every Kustomization reported Ready.

The failure was silent twice over: a status-less Certificate passes Flux's health checks, and the smoke workflow probed pods and Services but never performed a TLS handshake.

## Decision Drivers

- cert-manager must be able to write status on the resources it reconciles; no SPI-controlled identity can be exempted from the AKS-managed policy.
- The Gateway itself must stay in `aks-istio-ingress` (managed Istio owns the GatewayClass there).
- The deployer cannot hand-patch resources in managed namespaces either; the declarative Flux path is the only write path, so the topology must be correct in Git.

## Considered Options

- **Issue Certificates into `platform`, bridge with ReferenceGrants.** Gateway API supports cross-namespace `certificateRefs` when the secret's namespace grants access.
- **Exempt cert-manager from the policy.** Not possible; the policy and its binding are AKS-managed.
- **Run cert-manager inside a managed namespace.** Exemption is by identity, not location; this changes nothing and violates the managed boundary.

## Decision Outcome

Chosen option: "Issue Certificates into `platform`, bridge with ReferenceGrants". `platform` already hosts cert-manager-issued material (`redis-tls-cert`), proving the write path. Each TLS overlay declares its Certificates in `platform` plus a ReferenceGrant (in `platform`) allowing the `spi-gateway` Gateway in `aks-istio-ingress` to read the named secrets; listener `certificateRefs` carry an explicit `namespace: platform`. HTTP-01 solver routes are created in the challenge's namespace (now `platform`) and attach to the gateway via its `allowedRoutes: from: All` listeners, so issuance needs no writes to the managed namespace at all.

### Consequences

- Good, because issuance works under the managed-namespace policy; validated live (issuance completed in ~30 s in `platform` after stalling indefinitely in `aks-istio-ingress`).
- Good, because cert-manager can now publish `Ready` conditions, so the `spi-gateway-tls` Kustomization genuinely gates on issuance instead of passing a status-less Certificate.
- Good, because the smoke workflow gained an HTTPS-handshake probe, closing the CI blind spot that let this ship.
- Bad, because the TLS trust topology spans two namespaces; readers must follow a ReferenceGrant to see why the listener resolves.
- Neutral, because Flux prunes the stalled Certificates from `aks-istio-ingress` on reconcile (it is exempt and owns them).
