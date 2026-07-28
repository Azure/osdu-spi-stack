# ADR-017: Per-Deploy Image Lock via ConfigMap + Flux Substitution

**Status**: Accepted

## Context

OSDU community services publish container images to a single GitLab registry under tag patterns like `*-master:<sha>` plus a moving `latest`. Two operational realities sit underneath that:

1. **Tag churn.** Tags get pruned. A chart that names a SHA tag can break on any later reconcile once the upstream registry trims it.
2. **Cluster drift over a long-lived deploy.** Without a pin, two `spi up` runs on different days against `main` install different images. Reproducing a bug becomes a moving target.

The simple options both fail. Pinning images inside each service's `HelmRelease.values` ties chart edits to image bumps and produces noisy Git diffs every refresh. Letting Flux follow `latest` is the inverse failure mode: every reconcile risks a silent service rotation mid-test.

ADR-004 (a local Helm chart per service) and ADR-009 (Flux for in-cluster reconciliation) both provide natural seams to inject pinned values without editing per-service manifests.

## Decision

Resolve OSDU image tags **per `spi up` run**, write them into a single `osdu-image-lock` ConfigMap in `flux-system`, and have every service Kustomization consume that ConfigMap via Flux `postBuild.substituteFrom`. The image lock is generated, not committed.

Shape:

- `src/spi/images.py` queries the GitLab registry API for each service in `IMAGE_REGISTRY`, finds the newest immutable SHA tag on the configured branch (default `master`), and renders the ConfigMap.
- The lock is applied during K8s bootstrap (Phase 4) before Flux reconciles. Keys are uppercase service names: `PARTITION_IMAGE`, `PARTITION_IMAGE_TAG`, `PARTITION_IMAGE_DIGEST`, etc.
- Service Kustomizations under `software/stacks/osdu/profiles/core/` reference the ConfigMap with `spec.postBuild.substituteFrom`, so `${PARTITION_IMAGE}` in a YAML expands at apply time. Service Helm chart values stay generic; the lock holds the pin.
- `spi reconcile --refresh-images` re-resolves and re-applies the ConfigMap, then reconciles the service Kustomizations. Updates are explicit, not silent.
- The schema-load Job is intentionally excluded from the live lock (`image_lock: False`). A completed Kubernetes Job cannot be updated in place, so the schema-load tag stays a Git default that `scripts/resolve-image-tags.py --update` advances on demand.

Rejected:

- **Pin tags inside each service's `HelmRelease.values`.** Every image refresh is N service-file edits. Noisy Git diffs and easy to skew across services.
- **Follow `latest` and rely on `reconcileStrategy: Revision`.** Works for production GitOps but is the exact "surprise upgrade" failure mode ADR-014 was written to avoid.
- **Commit a static `osdu-image-lock.yaml`.** Reproducible but defeats the whole point: refreshes still require N Git edits, and the file goes stale between deploys.
- **A Helm post-renderer or Kustomize patch chain.** Moves the pin from a flat ConfigMap to template logic the operator has to debug at render time. The flat ConfigMap is debuggable with `kubectl get cm osdu-image-lock -o yaml`.

## Consequences

- A `spi up` deploys exactly one resolved image set. The set is reproducible from the ConfigMap; `spi info` surfaces the lock's resolution timestamp and per-service tags.
- Image refreshes are deliberate, not ambient. `spi reconcile --refresh-images` is the supported path; nothing else moves tags.
- Adding a new OSDU service to the stack is one entry in `IMAGE_REGISTRY` plus one service YAML that consumes `${SERVICE_IMAGE}`. No template changes.
- The image lock depends on the GitLab community registry being reachable from the CLI host. `spi check` covers tool prerequisites; registry reachability surfaces as a hard error during Phase 4.
- Adding non-service images (e.g., a one-shot loader Job that completes once) is a `image_lock=False` entry so the Job's resource template stays in Git rather than chasing a live ConfigMap. Refresh goes through `scripts/resolve-image-tags.py --update`.
