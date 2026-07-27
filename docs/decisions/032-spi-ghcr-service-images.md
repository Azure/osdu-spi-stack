---
status: "accepted"
contact: "Yuchen Wang"
date: "2026-07-20"
deciders: "Yuchen Wang"
---

# ADR-032: Use SPI-built GHCR images as the service baseline

## Context and Problem Statement

The SPI engineering system now builds public service images in GHCR, while the
stack still resolves its service baseline from OSDU community GitLab images.
This prevents the stack from validating Microsoft SPI changes until equivalent
community images are published. It also makes the deployed source different from
the source reviewed and released in the SPI service repositories.

The Stack must support two valid operating models:

- SPI-built images for Azure implementation development and release;
- OSDU community images when an operator explicitly chooses the community
  baseline.

Changing the default must not remove the community option.

## Decision Drivers

- The stack baseline must represent the source in the SPI service repositories.
- The default must follow each service's latest successful `main` build.
- Release and feature selectors must resolve to immutable image digests.
- Mutable image tags must never be deployed directly.
- Resolution must be atomic across the service fleet.
- Community images must remain a supported operator-selected source after the
  default changes.

## Considered Options

- Keep community GitLab images as the only baseline.
- Deploy mutable GHCR branch tags.
- Use GHCR tags as discovery selectors and pin their OCI digests.
- Copy every SPI image into the Stack's private Azure Container Registry.

## Decision Outcome

Chosen option: "Use GHCR tags as discovery selectors and pin their OCI
digests." The default baseline is each SPI service package's
`main-snapshot`, representing its latest successful `main` build. The CLI
resolves that tag once and stores the immutable OCI digest in
`osdu-image-lock`.

The selector contract is:

| Selector | Behavior |
| --- | --- |
| No image selector | Resolve `main-snapshot` in every configured GHCR package |
| `--image-tag <tag>` | Resolve one exact GHCR tag across the fleet |
| `--image-ref <git-ref>` | Resolve each service repository ref to its `sha-<12>` image |
| `--image-source community --image-branch master` | Resolve the OSDU community GitLab baseline |

Community selection is a supported mode, not a removed legacy path. Operators
can choose it during `spi up` or an explicit image refresh.

All selectors are discovery inputs only. The Stack writes
`repository@sha256:<digest>` into the effective Helm values.

## Implementation

The change adds the following behavior:

- `src/spi/images.py`
  - supports GHCR and community sources;
  - resolves public GHCR manifests through the registry Bearer-token flow;
  - resolves optional Git refs through the GitHub commits API;
  - fails the complete resolution when any required service cannot resolve;
  - renders source, organization, tag/ref, repository, tag, timestamp, and
    digest metadata into `osdu-image-lock`.
- `src/spi/config.py` and `src/spi/cli.py`
  - default new deployments to GHCR `main-snapshot`;
  - add `--image-org`, `--image-tag`, and advanced `--image-ref`;
  - preserve `--image-branch` as the community compatibility option;
  - reject incompatible or empty selectors;
  - preserve the live source and selector during
    `spi reconcile --refresh-images` unless explicitly overridden.
- `src/spi/deploy.py`
  - resolves the image fleet before Azure provisioning so registry failures do
    not leave a partially provisioned environment.
- `software/charts/osdu-spi-service`
  - accepts `image.digest`;
  - renders `repository@digest` when present;
  - retains `repository:tag` for sources that do not provide a digest.
- Service and reference-service HelmRelease manifests
  - consume the per-service digest from the Flux substitution ConfigMap.

The image lock covers 13 running services. The schema-load Job remains outside
the lock because a completed Job cannot be upgraded safely in place; it is
still pinned to an immutable community image.

The GitHub organization is configurable via `--image-org`. It defaults to
`Azure`; until that organization hosts the complete service and package fleet,
adopters point it at whichever organization publishes their SPI service forks,
or fall back to `--image-source community`.

## Live Validation

The feature-ref path was validated in retained AG1 environment
`spi-stack-ghcr1`:

- 23/23 Flux Kustomizations Ready;
- all HelmReleases Ready;
- all 13 service Deployments Ready on GHCR digests with zero final restarts;
- partition and Entitlements initialization complete;
- all 1,386 schemas loaded;
- 16/16 Workload Identity smoke checks passed;
- Storage -> Service Bus -> Indexer Queue -> Indexer -> Search passed;
- public HTTPS returned 200 with a valid Let's Encrypt certificate;
- an image refresh with no selector overrides preserved the selected feature
  ref and changed zero digests;
- no AADSTS70011, Cosmos TLS/410/Gone, unreadable agent, missing Gremlin key, or
  conflicting logging-provider signatures remained in final service logs.

Live validation also identified provider compatibility changes required for the
fleet:

- older Partition, Entitlements, Legal, and Storage forks needed the canonical
  App Insights agent `ADD --chmod=0755` fix;
- Entitlements needed the validated Gremlin managed-identity implementation
  because the Stack disables Gremlin local authentication;
- CRS Conversion needed to exclude Boot's default logging starter after the
  core-lib-azure 3.0.1 dependency jump.

### Consequences

- Good, because a clean `spi up` follows the latest successful `main` build for
  each service without depending on non-code commits at the repository HEAD.
- Good, because the mutable discovery tag never reaches Kubernetes; the image
  lock and Helm releases use immutable digests.
- Good, because coordinated release tags are reusable and feature refs remain
  available when a fleet-wide branch test is genuinely needed.
- Good, because community images remain explicitly selectable when an operator
  needs the community source rather than the Azure SPI implementation.
- Good, because resolution is all-or-nothing and occurs before provisioning.
- Bad, because all selected service packages and tags must exist publicly before
  provisioning begins.
- Bad, because the default cannot become operational until every service has a
  published `main-snapshot`.
- Bad, because current per-service Release Please versions are independent; a
  single coordinated semantic-version tag is not yet produced across the fleet.
- Bad, because normal pull-request builds can publish a synthetic merge-SHA tag
  while `--image-ref` resolves the source branch-head SHA. The engineering
  system must align those tags before feature-ref publication is automatic.
- Bad, because the schema-load Job remains a separately managed community image.

## Follow-up Decisions

- Define a fleet release mechanism, preferably a manifest mapping each service
  to a version and digest unless coordinated tags are introduced.
- Align pull-request image tags with the source branch-head SHA.
- Publish `main-snapshot` for the complete fleet before merging this default
  change.
- Replace the temporary public GitOps mirror with an official public source or
  supported private-repository authentication.
- Build an SPI-owned schema-load image if the Azure implementation must remove
  all community runtime-image dependencies.
