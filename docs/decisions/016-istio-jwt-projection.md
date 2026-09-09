# ADR-016: Istio JWT Projection for Azure-Provider OSDU Services

## Context

The Azure-provider OSDU service images ship an in-process Spring filter chain that reads the caller's application identity from a request header, not from the bearer token; the chain cannot be disabled by configuration. The header is expected to be populated by the Istio sidecar before the request reaches the Java application. With no Istio policy performing that projection, requests carrying a valid bearer fail with 401/403 and `app-id=` empty in the service request log, before any business logic runs. Choosing the Azure provider (ADR-001) therefore implies a runtime contract: something in the request path must extract the JWT payload and surface it as a header the service understands.

## Decision

Three Istio resources satisfy the contract, applied imperatively from the CLI in the same K8s bootstrap step that writes `osdu-config`; the CLI already holds the tenant id and the OSDU UAMI client id, which are the issuer and audience values the policy needs:

- `RequestAuthentication` `spi-osdu-jwt-authn` accepting the AAD v1 and v2 issuers and audiences `{client_id}` and `https://management.azure.com[/]`, with `outputPayloadToHeader: x-payload` and `forwardOriginalToken: true`.
- `EnvoyFilter` `spi-osdu-identity-filter` in `osdu`, on `SIDECAR_INBOUND`: its Lua reads `jwt_authn` dynamic metadata and writes `x-app-id` from the token's own `appid` (v1) or `azp` (v2) and `x-user-id` from the issuer-specific claims. No audience maps to a fixed principal; a caller is projected as itself and entitlements decides what it may do.
- `PeerAuthentication` `spi-osdu-mtls` mode `PERMISSIVE` in `osdu`, defensive against managed-mesh defaults that could break the init Jobs.

Sidecar injection is a prerequisite, so the `osdu` namespace `istio.io/rev` label is not pinned in Git; it is sourced from the live cluster revision via the `osdu-flux/spi-cluster-config` ConfigMap and Flux substitution.

The audience list must include every value services use to mint tokens. Bootstrap Jobs use `aud=https://management.azure.com/`, but `core-lib-azure` mints service-to-service calls with scope `${aadClientId}/.default`; if `AAD_CLIENT_ID` is overridden to a separate app registration, that appid must also be in the list, otherwise `jwt_authn` skips validation, the Lua exits early, and downstream services return 403 with `app-id=` empty. `istio_auth_resources()` accepts both `entra_client_id` and `aad_client_id` and emits both, deduped when they match.

Rejected: substituting the OSDU UAMI client id for any management-audience caller. It let the bootstrap Jobs land with the owner's app id, but the Jobs already run as that UAMI, so their own `appid` is the same value and the substitution added nothing for them. For every other caller it was an impersonation path: any tenant principal holding a management-audience token reached entitlements as the group owner, and the deploy identity could not be told apart from the no-access identity.

Rejected: `RequestAuthentication` plus `AuthorizationPolicy` keyed on JWT claims. Works for images whose Spring chain reads `RequestPrincipal` directly; the Azure provider's does not.

Rejected: per-service default-deny `AuthorizationPolicy` as defense in depth. The Spring chain already enforces identity, and default-deny on services serving traffic carries a wider blast radius; it remains available as a later hardening pass.

Rejected: pre-populating entitlements without going through the service API. Bypasses the auth chain but ties bootstrap to schema internals of the entitlements implementation, the burden ADR-013 and ADR-015 removed.

## Consequences

- The resources are present before any caller is expected to authenticate, so bootstrap Jobs and service-to-service traffic both see populated `x-app-id` headers.
- Envoy Lua now sits between deployment and authorization: JWKS reachability failures, RA drift, or sidecar skew manifest as `app-id=` empty rather than a clear auth error; check the EnvoyFilter and RequestAuthentication first when Jobs return 401/403.
- The Lua is coupled to the claim names Entra emits (`appid`, `azp`, `oid`, `unique_name`, `upn`); an issuer change means revisiting it. The audience list, not the Lua, is what a new caller must satisfy.
- A human's own token reaches the services as that human. Hand-testing an entitlements-backed API needs an app-only token from a seeded identity, not `az account get-access-token` under a user login.
