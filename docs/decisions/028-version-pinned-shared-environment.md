# ADR-028: Version-Pinned Shared Backing Environment

## Context

The stack serves as the deploy and test target for the `Azure/osdu-spi-*`
service forks: their CI deploys just-built images into a standing environment
(ADR-031) and runs acceptance tests against it. A target that tracks a branch
changes under whatever merges next; a platform break on a rolling ref blocks
the merge gate of eight fork repositories at once and blurs which change caused
it. The Flux configuration (`infra/flux.bicep`) can reference only a branch,
and no surface records which revision an environment runs.

## Decision

The shared backing environment deploys a release tag, never a branch, and its
whole declaration lives in one reviewed file.

- `infra/flux.bicep` gains a `repoTag` parameter; `repositoryRef` selects
  `{ tag: repoTag }` when the parameter is non-empty and `{ branch: repoBranch }`
  otherwise. `spi up --tag vX.Y.Z` carries it, mutually exclusive with a
  non-default `--branch`. Dev and smoke environments keep branch tracking.
- Release tags are the version currency. A `vX.Y.Z` tag from release-please
  snapshots the full tree, `software/` included, so the release process is
  unchanged; certifying a release is the smoke pipeline's job, not a new one.
- `ops/environments/shared.yaml` declares the environment: name, `stackVersion`,
  profile, location, ingress mode, image branch. It advances only through a
  reviewed PR to `main`; merging a `stackVersion` bump triggers the upgrade
  workflow (ADR-029).
- Publishing a release opens the bump PR automatically (a job in
  `.github/workflows/release.yml` under the release App token, so the resulting
  PR triggers checks). Merging stays with a human.

Rejected: track `main` continuously. The production-correct freshness model,
and the reason ADR-014 exists; one bad merge stops eight merge gates.

Rejected: pin to a commit SHA. Byte-for-byte as reproducible as a tag, but
unreadable in an RG tag and unnamed in the CHANGELOG.

Rejected: a separate environment repository holding the declaration. Clean
separation of tool from environment, at the cost of a second repo whose content
is one file plus workflows that live here anyway.

## Consequences

- Freshness costs a full cycle: a fix the fork CI needs reaches the shared
  environment only through a tagged release plus a merged bump PR. A hotfix
  path shorter than that does not exist by design.
- Which version the environment runs is a `git log` of one file; incident
  causality starts there instead of in Flux status archaeology.
- `infra/flux.bicep` carries two ref shapes, and the CLI must reject the
  ambiguous combination of `--tag` with an explicit `--branch`.
- The bump automation depends on the release App token; if it breaks, the
  fallback is an ordinary hand-opened PR, not a stale environment.
