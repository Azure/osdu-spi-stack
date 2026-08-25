# ADR-017: Per-Deploy Image Lock via ConfigMap + Flux Substitution

## Context

The OSDU community GitLab registry is the current image source, publishing under tag patterns like `*-master:<sha>` plus a moving `latest`. Two operational realities sit underneath that:

1. **Tag churn.** Tags get pruned. A chart that names a SHA tag can break on any later reconcile once the upstream registry trims it.
2. **Cluster drift over a long-lived deploy.** Without a pin, two `spi up` runs on different days against `main` install different images. Reproducing a bug becomes a moving target.

The simple options both fail. Pinning images inside each service's `HelmRelease.values` ties chart edits to image bumps and produces noisy Git diffs every refresh. Letting Flux follow `latest` is the inverse failure mode: every reconcile risks a silent service rotation mid-test.

ADR-004 (a local Helm chart per service) and ADR-009 (Flux for in-cluster reconciliation) both provide natural seams to inject pinned values without editing per-service manifests.

## Decision

Resolve OSDU image tags **per `spi up` run**, write them into a single `osdu-image-lock` ConfigMap in `osdu-flux`, and have each service and schema-load Kustomization consume that ConfigMap via Flux `postBuild.substituteFrom`. The image lock is generated, not committed.

Shape:

- `src/spi/images.py` queries the GitLab registry API for each service in `IMAGE_REGISTRY`, finds the newest immutable SHA tag on the configured branch (default `master`), and renders the ConfigMap. The schema-load image is resolved from the selected schema-service SHA and fails fast if that exact loader tag is absent.
- The lock is applied during K8s bootstrap (Phase 4) before Flux reconciles. Keys are uppercase service names: `PARTITION_IMAGE`, `PARTITION_IMAGE_TAG`, `PARTITION_IMAGE_DIGEST`, etc.
- Service Kustomizations under `software/stacks/osdu/profiles/core/` reference the ConfigMap with `spec.postBuild.substituteFrom`, so `${PARTITION_IMAGE}` in a YAML expands at apply time. Service Helm chart values stay generic; the lock holds the pin.
- `spi reconcile --refresh-images` re-resolves and re-applies the ConfigMap, then reconciles the service Kustomizations and `spi-osdu-schema-load` before `spi-osdu-reference`. Updates are explicit, not silent.
- The schema-load Job is included in the live lock. Because a completed Kubernetes Job cannot be updated in place, its Flux Kustomization uses `force: true` so a changed image tag replaces the Job and re-runs the loader.
- The schema-load Job substitutes its image with no static default, so a lock generated before the loader joined would leave the Job unresolvable. `spi reconcile` backfills the missing `SCHEMA_LOAD_*` keys from the schema tag the lock already pins, leaving every other service pin untouched.
- The registry is a source parameter, not part of the decision. A registry move (for example to GitHub-hosted images from the SPI custom-image supply chain, ADR-023) changes the resolution path in `src/spi/images.py` and the pin flow's provenance checks; the lock, the substitution seam, and the refresh and pin semantics stay as they are.

### Per-service MR pins

Validating an upstream service fix requires running the exact image its merge-request pipeline built, on a live cluster, before the fix merges. Because the lock is the single substitution source and is CLI-owned, pins ride the lock rather than Flux: `spi service pin <service> --mr <iid>` resolves the image tagged with the MR's head commit from the community registry (checking the source branch, then its `trusted-` copy, the protected ref OSDU maintainers create to run the containerize pipeline; an image from a stale `trusted-` copy that no longer matches the MR head is rejected), overwrites the service's lock keys, and records provenance plus the canonical image in a lock annotation. `spi service reset` restores the canonical entries exactly; `spi service list` shows active pins. Both lock re-render paths (`spi reconcile --refresh-images` and a re-run `spi up`) re-assert active pins and name them, so a refresh cannot silently revert one. Pinning `schema` pins the paired loader image when the MR pipeline built one.

Rejected:

- **Pin tags inside each service's `HelmRelease.values`.** Every image refresh is N service-file edits. Noisy Git diffs and easy to skew across services.
- **Follow `latest` and rely on `reconcileStrategy: Revision`.** Works for production GitOps but is the exact "surprise upgrade" failure mode ADR-014 was written to avoid.
- **Commit a static `osdu-image-lock.yaml`.** Reproducible but defeats the whole point: refreshes still require N Git edits, and the file goes stale between deploys.
- **A Helm post-renderer or Kustomize patch chain.** Moves the pin from a flat ConfigMap to template logic the operator has to debug at render time. The flat ConfigMap is debuggable with `kubectl get cm osdu-image-lock -o yaml`.
- **MR pins via a suspended Kustomization and a patched HelmRelease.** Freezes every sibling service under the same owner and leaves drift correction off.
- **MR pins via `--image-branch`.** Moves every service to one branch; validation needs one service moved and thirteen held.

## Consequences

- A `spi up` deploys exactly one resolved image set. The set is reproducible from the ConfigMap; `spi info` surfaces the lock's resolution timestamp and per-service tags.
- Image refreshes are deliberate, not ambient. `spi reconcile --refresh-images` is the supported path; nothing else moves tags.
- Adding a new OSDU service to the stack is one entry in `IMAGE_REGISTRY` plus one service YAML that consumes `${SERVICE_IMAGE}`. No template changes.
- The image lock depends on the configured source registry being reachable from the CLI host. `spi check` covers tool prerequisites; registry reachability surfaces as a hard error during Phase 4.
- Adding a one-shot image to the live lock requires its Kustomization to tolerate immutable resource updates, for example `force: true` on Jobs whose Pod templates include lock substitutions.
- MR validation uses only pipeline-built, provenance-clean images, and pin state is declared on the cluster, not in operator memory. Re-asserting pins after a lock refresh clears the pinned entries' created-at and digest metadata until the pin is released and the entry re-resolved.
