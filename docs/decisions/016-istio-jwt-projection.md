# ADR-016: Istio JWT Projection for Azure-Provider OSDU Services

**Status**: Accepted

## Context

Following ADR-015, the partition + entitlements bootstrap Jobs acquire a Workload Identity bearer token and POST to the in-cluster OSDU services. Both POSTs are rejected: partition returns 403 from `AzureIstioSecurityFilter`, entitlements returns 401 from `AuthorizationFilter`. In both rejections, the service-side request log records `app-id=` empty even though the bearer carries a valid `appid` claim that matches the OSDU UAMI client id.

The Azure-provider OSDU service images (`*-service-azure:*`) include an in-process Spring filter chain that reads the caller's application identity from a request header, not from the bearer token directly. The header is expected to be populated by the Istio sidecar before the request reaches the Java application. With no Istio policy configured to perform that projection, the header is absent and authorization fails before any business logic runs.

This filter chain is part of the service image; it cannot be disabled by configuration. Choosing the Azure provider therefore implies a runtime contract that something in the request path must extract the JWT payload and surface it as a header the service understands.

A separate reference implementation exists for the same provider that satisfies this contract by combining three Istio resources: a `RequestAuthentication` that validates the bearer and parks the decoded payload as Envoy dynamic metadata, an `EnvoyFilter` whose Lua reads that metadata and writes `x-app-id` / `x-user-id`, and a permissive `PeerAuthentication`. A different reference implementation that uses non-Azure OSDU images takes a simpler route — `RequestAuthentication` plus `AuthorizationPolicy` keyed on JWT claims — but that route depends on service code that consumes Istio's `RequestPrincipal` directly, which the Azure provider does not.

## Decision

Adopt the three-resource pattern, applied imperatively from the CLI in the same Phase 4 step that writes `osdu-config` and `spi-init-values`. The CLI already has the tenant id and the OSDU UAMI client id in `infra_outputs`, which keeps the substitution local and avoids introducing a Flux variable-substitution path for one ConfigMap.

Resources:

- `RequestAuthentication` accepting both AAD v1 and v2 issuers, audiences `{client_id}` and `https://management.azure.com[/]`, with `outputPayloadToHeader: x-payload` and `forwardOriginalToken: true`.
- `EnvoyFilter` `spi-osdu-identity-filter` in the `osdu` namespace, applied to `SIDECAR_INBOUND`. Its Lua reads `envoy.filters.http.jwt_authn` dynamic metadata and writes `x-app-id` / `x-user-id`. The branch that special-cases `aud == https://management.azure.com/` replaces both headers with the OSDU UAMI client id, matching the audience presented by Workload Identity tokens.
- `PeerAuthentication` `spi-osdu-mtls` mode `PERMISSIVE` in `osdu`, defensive against managed-mesh defaults that could otherwise break the init Jobs.

A per-service default-deny `AuthorizationPolicy` is intentionally not adopted in this ADR. The reference implementation that uses it treats it as defense in depth: even if the bearer is missing or invalid, the request never reaches the service. Our Azure-provider services already enforce identity in the Spring filter chain, so the second layer is duplicative for the bootstrap problem we are solving. Adding default-deny on services that are already serving traffic also has a wider blast radius than the rest of this change. We may revisit and adopt it later as a hardening pass.

## Consequences

- The CLI-applied resources are present before any caller is expected to authenticate, so the bootstrap Jobs and ongoing service-to-service traffic both see populated `x-app-id` headers.
- A new dependency on Istio Envoy Lua sits between deployment and authorization. Failures in JWKS reachability, RA configuration drift, or sidecar version skew now manifest as `app-id=` empty rather than as a clear-cut auth error. The runbook should call out checking the EnvoyFilter and RequestAuthentication first when bootstrap Jobs return 401/403.
- Tying the EnvoyFilter to the Workload Identity audience (`https://management.azure.com/`) means future identity changes (different audience, switch to a managed identity with different claims, etc.) require revisiting the Lua. The mapping is small and contained, but it is a coupling that did not previously exist.
- The audience list must include every value services use to mint service-to-service tokens. Bootstrap Jobs use `aud=https://management.azure.com/`, but `core-lib-azure`'s `getWIToken` mints subsequent service-to-service calls with scope `${aadClientId}/.default`. If `AAD_CLIENT_ID` is overridden to a separate OSDU AAD app registration, that appid must also be in the RA audience list — otherwise `jwt_authn` skips validation, the Lua exits early, the Spring filter sees an empty `x-app-id`, and downstream services return 403 with an empty `app-id=` in the request log. `istio_auth_resources()` accepts both `entra_client_id` (UAMI) and `aad_client_id` and emits both, deduped when they match.

Rejected alternatives:

- **Solving with `AuthorizationPolicy` alone.** Works for service images whose Spring chain reads `RequestPrincipal` directly. Our images do not, so the in-process filter still rejects after Istio admits the request.
- **Switching to a different OSDU provider.** Out of scope and inconsistent with the SPI Stack's stated commitment to the Azure provider (ADR-001).
- **Imperative side-channel that pre-populates entitlements without going through the service API.** Bypasses the auth chain entirely but ties bootstrap to schema details internal to the entitlements implementation, and re-creates the maintenance burden ADR-013 and ADR-015 reduced.
