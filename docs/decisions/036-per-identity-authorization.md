---
status: "accepted"
contact: "Yuchen Wang"
date: "2026-07-24"
deciders: "Yuchen Wang"
---

# Per-Identity Authorization with Explicit Membership Seeding

## Context and Problem Statement

ADR-016 projects token claims into the `x-app-id` and `x-user-id` headers the
Azure-provider OSDU images read. Its original filter collapsed every token with
the management audience to the OSDU workload identity, which is the bootstrap
Entitlements owner created by ADR-015.

That collapse is invisible and dangerous. Any other principal that mints a
management-audience token, in particular a CI identity used by the deploy lane,
is projected as the bootstrap owner and inherits its authorization. Integration
tests then pass without the test identity being an Entitlements member, so they
prove nothing about authorization, and a negative test cannot fail.

## Decision Drivers

- Authorization must be an Entitlements decision, not a gateway rewrite.
- Every caller must reach services as itself.
- Bootstrap must keep working with no privileged side channel.
- Token versions project different claims for the same human identity.

## Considered Options

- Identity-only projection plus explicit membership seeding
- Keep the audience special case and grant test identities implicitly
- Seed membership directly in the Entitlements graph

## Decision Outcome

Chosen option: "Identity-only projection plus explicit membership seeding".

The Istio filter becomes a pure identity extractor: it removes the audience
special case and the embedded client-id constant, projects the caller's own
application id (`appid` for v1, `azp` for v2) as `x-app-id`, and extracts
`x-user-id` through the standard issuer-based path. It makes no authorization
decision and knows nothing about Entitlements. Because the workload identity's
own application id is the bootstrap owner, the ADR-015 Jobs are unaffected.

Identities that need access are seeded through the public AddMember API into the
same four root groups ADME seeds. `spi up` seeds the Stack creator by default,
resolving it from the current Azure token with the same v1/v2 precedence the
filter uses and including the user object ID as an alias so either token version
authorizes. `--no-seed-creator` disables this for workload-only automation and
`--creator-user-id` overrides discovery. `spi onboard` seeds each onboarded CI
identity the same way. The shared no-data test identity is deliberately never
seeded: it must authenticate successfully and still be denied.

Rejected: keeping the audience special case, because it makes authorization
tests meaningless. Rejected: graph-level seeding, because it couples bootstrap
to Entitlements internals that ADR-015 already avoided.

### Consequences

- Good, because authorization is enforced per identity and is testable in both
  directions.
- Good, because bootstrap keeps using public APIs only.
- Bad, because every new caller now requires an explicit membership step.
- Bad, because human identities may need two projected aliases.
