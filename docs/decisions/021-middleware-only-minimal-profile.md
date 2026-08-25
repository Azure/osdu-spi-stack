# ADR-021: Middleware-Only `minimal` Profile, Replacing the Unbacked `full`

## Context

The `Profile` enum offered `core` and `full`, but only `software/stacks/osdu/profiles/core/` existed on disk: `--profile full` provisioned the full Azure estate, then pointed the Flux `stack` Kustomization at a nonexistent path, a ~45-minute deploy ending in a reconcile error instead of an up-front validation failure. Separately, there was no way to stand up only the middleware substrate; working on an HTTPRoute or the trust-manager CA distribution meant waiting on ten OSDU services that contribute nothing to that work.

## Decision

Add `minimal`, drop `full`. `Profile` is `bare | minimal | core`: `bare` is infrastructure plus activated GitOps against empty trees, `minimal` is the middleware substrate, `core` adds the OSDU services. `software/stacks/osdu/profiles/minimal/stack.yaml` reproduces layers 0a through 4b verbatim and stops at the boundary `spi-osdu-services` starts from, so the middleware layers are identical across profiles and what is validated on `minimal` holds on `core`.

The ingress trees declared one `spi-osdu-routes` Kustomization with `dependsOn: spi-osdu-services`; under `minimal` that dependency never appears and Flux stalls the Kustomization on `DependencyNotReady` indefinitely. Two changes resolve this:

- Routes split by scope: `middleware/` (Kibana, Airflow, their ReferenceGrants) and `osdu/` (the OSDU APIs) reconcile as separate Kustomizations, so middleware routing does not depend on OSDU services being present.
- `infra/flux.bicep` derives the ingress path from the profile: `minimal` selects `ingress/<mode>-minimal/`, the mode's tree minus the OSDU routes block, keeping flat trees per combination rather than conditional overlays (consistent with ADR-012). `profile` and `ingressMode` carry `@allowed` constraints, so an unbacked value fails at template validation; `tests/test_profiles.py` asserts for each pairing that every referenced path exists and no `dependsOn` names an undeclared Kustomization.

Dropping back to `bare` removes the middleware workloads. Redis is a cache (ADR-003), so its StatefulSets set `persistentVolumeClaimRetentionPolicy` to `Delete` on both `whenDeleted` and `whenScaled`: rolling upgrades preserve data, while profile removal deletes the PVCs and their Azure disks instead of leaving billable unattached volumes. Helm retains operator CRDs on uninstall, so `bare` promises no active workloads, not cluster-scoped CRD cleanup.

Rejected: build out `full` with a real tree of additional services. This repo has none; every OSDU service already ships under `core`.

Rejected: leave `full` documented as unimplemented. Keeps advertising a broken flag.

## Consequences

- Every accepted `--profile` value resolves to a tree that reconciles to Ready, and the dangling-dependency class of bug is caught by a unit test rather than a timed-out cloud deploy.
- Middleware work gets a deploy with no OSDU services and layers identical to `core`.
- Each ingress mode carries a near-duplicate `-minimal` stack file; edits to shared blocks touch two files per mode.
- `ip` plus `minimal` deploys no ingress at all; middleware UIs need `kubectl port-forward` on that combination.
- Returning to `bare` deletes Redis volumes but can leave operator CRDs behind; they carry no running workload or storage cost.
