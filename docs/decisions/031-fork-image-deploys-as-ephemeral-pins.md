# ADR-031: Fork-Built Images Deploy as Ephemeral Lock Pins

## Context

The `Azure/osdu-spi` engineering system builds service images to GHCR
(`ghcr.io/azure/<service>`, public packages) and mandates the manifest digest,
not a tag, as the deploy identity; its `sha-*` tags are pruned after 30 days.
A fork's PR pipeline must deploy that digest into the shared environment
(ADR-028), test against it, and restore, without breaking sibling services.
Under ADR-014 the Kustomizations keep reconciling from the cached source
artifact, so any deploy that bypasses the desired state, such as writing the
Deployment directly, is reverted on the next reconcile interval. ADR-017
already routes image identity through the `osdu-image-lock` ConfigMap and
re-asserts pins across refreshes; that seam is where a fork deploy has to
live.

## Decision

A fork deploy is a pin on the image lock, marked ephemeral, written and
verified by the CLI, and returned by an always-run restore with a scheduled
backstop behind it.

- **Digest pins.** `spi service pin <service> --image
  ghcr.io/azure/<repo>@sha256:<digest>` writes the service's lock keys and
  records provenance in the pin annotation: origin, digest, source repository,
  commit, workflow run URL, and the `ephemeral` marker. Validation requires an
  allow-listed GHCR owner, a digest whose manifest resolves, and, when a
  commit is supplied, agreement between its `sha-*` tag and the digest. A
  human-driven variant, `--github <owner/repo> --sha <commit>`, resolves the
  digest from the commit. `--mr` remains the community-GitLab arm (ADR-017).
- **Verified, not assumed.** `spi service verify <service> --image <ref>`
  asserts the Deployment's pod template and a running pod's `imageID` carry
  the digest and the rollout is complete. The integration-test job runs the
  same check as a pre-flight, so a deploy overwritten by a colliding pipeline
  fails fast with the colliding run's URL from the pin annotation instead of
  producing a silently wrong test result.
- **Restore and roll-forward.** PR pipelines end with `spi service reset` in
  an always-run job, restoring the canonical entries the pin recorded. Push
  builds on a fork's default branches call `spi service refresh <service>`
  instead: re-resolve the canonical image, drop any pin, reconcile. Re-running
  a refresh is idempotent, so it needs no staleness detection.
- **The backstop.** A cancelled run, an expired OIDC token, or a lost runner
  strands a pin no restore job will return. The weekday refresh (ADR-029) runs
  `spi service reset --ephemeral`, which resets pins carrying the marker and
  nothing else, then `spi service refresh` for each GitHub-origin service.
  Operator pins, GitLab MR pins and GHCR pins placed without `--ephemeral`,
  are never swept.
- **Canonical follows onboarding.** An `IMAGE_REGISTRY` entry gains a
  `github_repo` field; setting it flips that service's canonical resolution
  from community GitLab to its fork's GHCR `main` image, one reviewable line
  per service. The nightly refresh of GitHub-origin canonicals also keeps the
  environment inside GHCR's 30-day `sha-*` retention.
- **Digest rendering.** The `osdu-spi-service` chart accepts `image.digest`
  and renders `repository@digest` when present, `repository:tag` otherwise;
  service YAMLs pass `${<SERVICE>_IMAGE_DIGEST}`. GitLab-resolved canonicals
  gain pull-by-digest against upstream tag pruning as a side effect.
- **Concurrency is per service.** Deploy jobs serialize per service via a
  GitHub concurrency group; nothing serializes across services. Bootstrap
  dependencies (partition, entitlements) need no wider lock: HelmRelease
  rollout semantics keep the old pods serving until new pods pass readiness,
  and a failed rollout remediates back, so a broken image never takes traffic.

Rejected: `kubectl set image` on the Deployment. The most direct mechanism,
and the next HelmRelease reconcile reverts it under ADR-014; it also leaves
the lock lying about what runs.

Rejected: per-deploy chart publication with a re-pointed HelmRelease source.
Carries chart changes as well as image changes, but each image-only deploy
then moves a chart version and needs a registry re-point and restore dance;
chart changes are stack-owned PRs here (ADR-004).

Rejected: a cluster-wide deploy lock. The strongest isolation, but it
serializes eight repositories behind each other's reconcile waits to prevent
a collision the verify pre-flight already converts into a cheap re-run.

Rejected: suspending the owning Kustomization for the pin's duration. Holds
the pin without CLI involvement, but freezes drift correction for the sibling
services under the same owner, the mechanism ADR-017 already declined.

## Consequences

- A test job can still observe a sibling service's rolling restart mid-run;
  the acceptance jobs' dependency health gate absorbs the window rather than
  any lock preventing it.
- The `ephemeral` marker is the ownership boundary: automation sweeps only
  what CI placed, and an operator's investigation pin survives the night.
- The lock's digest keys become load-bearing; `render_lock_with_pins()` must
  carry the pin's digest and created-at through the overlay instead of
  blanking them (ADR-017).
- Chart-contract changes do not ride this seam: a service PR that needs a new
  env var or chart behavior lands a stack PR first, the environment picks it
  up on upgrade, and the fork PR deploys against it.
- Fork CI becomes a CLI consumer: the deploy job installs the released `spi`
  wheel, and annotation-schema changes must keep old pins decodable.
