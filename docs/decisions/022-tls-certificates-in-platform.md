# ADR-022: TLS Certificates in platform

## Context

cert-manager must write status on every Certificate it reconciles, and on AKS Automatic the `aks-managed-protect-system-namespaces` ValidatingAdmissionPolicy denies those writes in managed namespaces such as `aks-istio-ingress`. A Certificate applied there gets no CertificateRequest, no ACME order, and no secret, while a status-less Certificate still passes Flux's health checks, so the HTTPS listener never opens and every Kustomization reports Ready. No SPI-controlled identity can be exempted from the AKS-managed policy.

## Decision

Certificates issue into `platform`, the Gateway's own namespace (ADR-026), and listener `certificateRefs` name the secret without a namespace or a ReferenceGrant (`software/overlays/gateway-tls-single-host/`, `software/overlays/gateway-tls-multi-host/`). HTTP-01 solver routes are created in the challenge's namespace and attach to the Gateway through its `allowedRoutes: from: All` listeners. The smoke workflow performs an HTTPS handshake, so a status-less Certificate fails CI instead of passing a health check.

Rejected: declare Certificates beside a Gateway in a managed namespace. Keeps the trust topology in one place, but cert-manager's status writes are denied there and issuance stalls silently.

Rejected: exempt cert-manager from the policy. Not possible; the policy and its binding are AKS-managed.

## Consequences

- The handshake check in CI is what catches a stalled Certificate; Flux's health check alone does not.
- Issuance and the listener resolve in one namespace under ordinary RBAC, and `platform` already hosts cert-manager-issued material (`redis-tls-cert`).
- Nothing the stack owns lives in a managed namespace, so the policy's exemption list cannot stall issuance.
