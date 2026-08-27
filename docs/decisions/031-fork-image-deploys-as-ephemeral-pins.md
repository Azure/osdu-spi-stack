# ADR-031: Fork-Built Images Deploy as Ephemeral Lock Pins

## Context

The `Azure/osdu-spi` engineering system builds service images to GHCR
(`ghcr.io/azure/<service>`, public packages) and mandates the manifest digest,
not a tag, as the deploy identity; its `sha-*` tags are pruned after 30 days.
A fork's PR pipeline must deploy that digest into the shared environment
(ADR-028), test against it, and restore, without breaking sibling services.
Under ADR-014 the Kustomizations keep reconciling from the cached source
artifact, so any deploy that bypasses the desired state, such as writing the
Deployment directly, is reverted on the next reconcile interval; a fork
deploy must land in the image lock (ADR-017), not on the workload.

## Decision

A fork deploy is a pin on the image lock, marked ephemeral, owned by the
workflow run that placed it, and returned by an always-run restore with a
scheduled backstop behind it. The operational sequence, command surface, and
annotation schema live in `docs/design/fork-deployment.md`.

- **Digest pins with provenance.** The CLI writes the service's lock keys
  from a GHCR digest reference and records provenance in the pin annotation:
  source repository, commit, owning workflow run, and the `ephemeral` marker.
  Validation requires an allow-listed GHCR owner and a digest whose manifest
  resolves. The pin refuses while the environment is not deployable
  (ADR-030).
- **Verified, not assumed.** Deploy success is the running pod carrying the
  digest, asserted by the CLI after the pin and re-asserted by the test job
  before it runs, so a deploy overwritten by a colliding pipeline fails fast,
  naming the colliding run from the pin annotation, instead of producing a
  silently wrong test result.
- **Ownership-conditional return.** The always-run restore resets a pin only
  while the live pin still belongs to its own workflow run; a newer run's pin
  is left standing. The weekday backstop (ADR-029) sweeps only ephemeral pins
  whose owning run has reached a terminal state, or whose age exceeds the
  threshold when that state is unreachable. Operator pins never carry the
  marker and are never swept.
- **Push builds deploy the same way.** A push to a trusted branch pins its
  digest ephemerally with no restore job; the weekday refresh then converges
  the canonical under the service's source policy (ADR-033), backward to the
  community image before a service's flip and forward to its fork `main`
  after it. One deploy path, and the template never needs to know a
  service's flip state.
- **Digest rendering.** The `osdu-spi-service` chart accepts `image.digest`
  and renders `repository@digest` when present, `repository:tag` otherwise
  (ADR-017); GitLab-resolved canonicals gain pull-by-digest against upstream
  tag pruning as a side effect.
- **Concurrency is per service; the lock write is compare-and-set.** Deploy
  jobs serialize per service; nothing serializes across services. All
  services share one lock object, so every pin, reset, and refresh submits
  its update conditioned on the lock's `resourceVersion` and retries on
  conflict; two services pinning at once serialize at the API server instead
  of overwriting each other's pin records. Bootstrap dependencies
  (partition, entitlements) need no wider lock: HelmRelease rollout
  semantics keep the old pods serving until new pods pass readiness, and a
  failed rollout remediates back, so a broken image never takes traffic.

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
  any lock preventing it. A candidate that passes readiness but is
  behaviorally broken can also fail a concurrently running sibling suite;
  the accepted recovery is that sibling's re-run after restore, not
  isolation.
- The `ephemeral` marker plus the recorded owner are the boundary automation
  respects: restore and sweep act only on pins they can prove theirs or
  stale, and an operator's investigation pin survives the night.
- The lock's digest keys become load-bearing; `render_lock_with_pins()` must
  carry the pin's digest and created-at through the overlay instead of
  blanking them (ADR-017).
- Chart-contract changes do not ride this seam: a service PR that needs a new
  env var or chart behavior lands a stack PR first, the environment picks it
  up on upgrade, and the fork PR deploys against it.
- Fork CI becomes a CLI consumer: the deploy job installs the released `spi`
  wheel, and annotation-schema changes must keep old pins decodable.
