---
status: "accepted"
contact: "danielscholl"
date: "2026-07-24"
deciders: "SPI Stack maintainers"
---

# Middleware-Only `minimal` Profile, Replacing the Unbacked `full`

## Context and Problem Statement

The `Profile` enum offered `core` and `full`, but only `software/stacks/osdu/profiles/core/` existed on disk. Passing `--profile full` was accepted by the CLI, provisioned the full Azure estate, then pointed the Flux `stack` Kustomization at a nonexistent path: a ~45-minute deploy that ends in a reconcile error rather than an up-front validation failure.

Separately, there was no way to stand up just the middleware substrate. Working on an HTTPRoute, a Helm chart, or the trust-manager CA distribution meant waiting on ten OSDU services that contribute nothing to that work.

## Decision Drivers

- An enum value the CLI accepts must resolve to a real, reconcilable tree.
- Middleware-focused work needs a fast deploy that omits the OSDU services.
- The middleware layers must be identical across profiles, so what is validated on the smaller profile holds on the larger one.
- Ingress mode (ADR-012) and stack profile are independent axes; a profile change must not force an ingress rewrite.

## Considered Options

- **Add `minimal`, drop `full`.** Ship the profile that has manifests; remove the one that does not.
- **Build out `full`.** Give `full` a real tree of additional services. This repo has no such services; every OSDU service already ships under `core`.
- **Leave `full` and document it as unimplemented.** Keeps advertising a broken flag.

## Decision Outcome

Chosen option: "Add `minimal`, drop `full`", because it removes a flag that cannot work and adds one that serves a real workflow. `Profile` is now `minimal | core`.

**Amendment (2026-07-28):** `Profile` later gained a third, smaller `bare` value for infrastructure plus activated GitOps against empty stack and ingress trees. [ADR-012](012-ingress-profiles.md) records its ingress pairing.

`software/stacks/osdu/profiles/minimal/stack.yaml` reproduces layers 0a through 4b verbatim and stops. It ends at the same boundary `spi-osdu-services` starts from, so the middleware substrate is complete (including the trust-manager Bundles that mirror the Redis and Elasticsearch CAs into `osdu`, ADR-011) without a single OSDU service.

### Ingress pairing

All three ingress trees declared one `spi-osdu-routes` Kustomization with `dependsOn: spi-osdu-services`. Under `minimal` that dependency never appears, and Flux stalls the Kustomization on `DependencyNotReady` indefinitely; `scripts/wait_for_flux_ready.sh` waits on every Kustomization, so `spi up` would hang until timeout.

Two changes resolve this:

1. **Routes split by scope.** `software/stacks/osdu/routes/<tree>/` now has `middleware/` (Kibana, Airflow, and their ReferenceGrants) and `osdu/` (the OSDU APIs) subdirectories. The ingress stacks reconcile them as separate `spi-middleware-routes` and `spi-osdu-routes` Kustomizations, so middleware routing no longer depends on OSDU services being present.
2. **`<mode>-minimal` ingress trees.** `infra/flux.bicep` derives the ingress path from the profile: `minimal` selects `ingress/<mode>-minimal/`, which is the mode's tree minus the `spi-osdu-routes` block. `ip-minimal` is empty by construction, since `ip` mode carries no middleware routes at all.

This keeps flat, readable trees per combination rather than conditional overlays, consistent with ADR-012's rejection of a profile matrix. `profile` and `ingressMode` both gained `@allowed` constraints in `infra/flux.bicep`, so an unbacked value now fails at template validation instead of at reconcile time.

`tests/test_profiles.py` asserts, for every `Profile` × `IngressMode` pairing, that the trees exist, that every referenced path exists, and that no `dependsOn` names a Kustomization the pairing does not declare.

### Consequences

- Good, because every accepted `--profile` value resolves to a tree that reconciles to Ready.
- Good, because middleware work gets a deploy with no OSDU services, and the layers it exercises are identical to `core`.
- Good, because the dangling-dependency class of bug is now caught by a unit test rather than by a timed-out cloud deploy.
- Bad, because each ingress mode carries a near-duplicate `-minimal` stack file; edits to shared blocks such as the cert issuers touch two files per mode.
- Bad, because `ip` + `minimal` deploys no ingress at all, so middleware UIs need `kubectl port-forward` on that combination.
- Neutral, because `full` was never deployable; removing it breaks no working configuration.
