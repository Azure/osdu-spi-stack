# ADR-033: Canonical Image Source Follows Onboarding

## Context

Each service has two plausible canonical image sources: the OSDU community
GitLab registry, which ADR-017 resolves from upstream `master`, and the
service's `Azure/osdu-spi-*` fork, which publishes its `main` line to GHCR
with digest identity, releases, and a 30-day retention on `sha-*` tags. Both
cannot be canonical at once: the canonical entry is what a pin restores to,
what refresh re-resolves, and what the environment runs between deploys. The
choice is independent of the ephemeral pin mechanism (ADR-031); it can change
per service without touching how deploys work.

## Decision

A service's canonical source flips from community GitLab to its fork's GHCR
`main` image when the fork onboards, one service at a time.

- The flip is recorded in the environment, not in the CLI: `spi onboard`
  writes the service's source (`community` or `<org>/<fork>`) into the
  `osdu-image-lock` ConfigMap (`src/spi/pins.py`). From then on each
  canonical resolution path (`--refresh-images`, `spi service refresh`)
  reads the fork's GHCR image for that service; absent, community GitLab
  stays canonical. Declared environments carry the same list as `forks:` in
  their declaration, and the lifecycle workflows reconcile the lock to it.
  A pin's reset is not a resolution path: it restores the target captured
  when the pin was written (below).
- On the shared environment the flip lands after the fork's deploy and test
  gates are active, so the image line the environment runs is the one those
  gates certify. A personal or customer stack follows its own fork from the
  moment its operator says so.
- `schema` has a flip precondition its siblings lack: schema-load resolves a
  loader image at the schema service's exact commit (ADR-017), and the fork
  publishes no loader. Schema keeps its community canonical until its fork
  publishes a paired `schema-load` image at the same commit; flipping it
  earlier would leave every refresh unable to resolve the loader and the
  environment stuck in maintenance.
- The weekday refresh re-resolves GitHub-origin canonicals (ADR-029), and
  that cadence matters against GHCR retention: continued fork builds move
  `main-snapshot` to newer package versions, and the retention job then
  deletes older `sha-*`-only versions outright, digest included. The refresh
  extends no version's life; it moves an environment that fell behind up to
  the current version before its recorded one lands in that deletion bucket.
  A genuinely quiet fork needs no such protection: its newest version keeps
  the `main-snapshot` tag and is never selected.
- A pin's restore target is captured when the pin is written (`canonical_*`,
  ADR-017), so reset restores the capture and refresh applies the policy: a
  flip while a pin is active does not retarget the pin, and the restored
  pre-flip image stands until the next refresh re-resolves under the new
  source.
- Push builds deploy as ephemeral pins on both sides of the flip (ADR-031);
  the refresh then converges the canonical, backward to the community image
  before the flip and forward to the fork's `main` after it. The transition
  needs no dedicated mechanism.

Rejected: the flip as a `github_repo` line in `src/spi/images.py`. One
reviewed line per service, but CLI code ships to every environment, so a
personal or customer stack could not follow its own fork without a code
change.

Rejected: community GitLab stays canonical for the fleet permanently. No
divergence from upstream to track, but the shared environment then never runs
the image line the forks ship and the deploy gates certify.

Rejected: flip the fleet in one change. Uniform behavior across services, but
it couples eight onboarding schedules to the slowest fork.

Rejected: dual-source fallback per service (GHCR first, GitLab when absent).
Resilient to a missing fork image, but two possible answers for one canonical
entry make "what should this service run" a runtime question instead of a
declaration.

## Consequences

- Divergence between community `master` and a fork's `main` becomes visible
  per service: two services can canonically run images from different
  lineages during the onboarding period, and the lock records which.
- The environment inherits a retention coupling: if the weekday refresh
  stalls for longer than the retention window while fork builds continue,
  the recorded canonical can age into a version the retention job has
  deleted, and the digest becomes unpullable, not merely untagged.
- Reversal is `spi onboard --remove`, with the next refresh restoring the
  community image.
- Which source is canonical is readable from `spi onboard --list` and the
  lock's per-service keys, not from operator memory.
