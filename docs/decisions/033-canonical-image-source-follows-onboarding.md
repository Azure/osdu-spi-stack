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

A repository can be trusted for deploys while its service remains on the
community canonical. Onboarding establishes trust; explicit source promotion
selects the fork's GHCR `main` image, one service at a time.

- Trust and source policy have separate durable records. The deploy
  identity's `fork-<service>` credential names the trusted repository
  (ADR-032). The resource-group tag `spi-source-<service>` records
  `community` or `<org>/<fork>` as that service's canonical source. A missing
  source tag means community, even when a credential exists. When these tags
  are authoritative, an unreadable or invalid tag is an error. Both records
  survive `spi down` (ADR-034). A fork source must match the trusted
  repository for that service.
- `spi onboard --canonical-source fork` promotes the service's trusted fork;
  `--canonical-source community` selects community without revoking trust.
  On an undeclared environment, omitting the option preserves the recorded
  source, defaulting to community for a new service. Source-policy writes
  merge only the owned tags and preserve the suffix, declaration locator,
  and unrelated tags.
- Declared environments carry `service`, `repo`, and `canonicalSource`
  (`community` or `fork`, default `community`) in each `forks:` entry.
  The declaration is authoritative; RG tags and credentials are reconciled
  copies. An omitted CLI source option takes the declared value, and a
  conflicting option is refused (ADR-032). Removing an entry selects
  community and revokes its credential. On an undeclared stack the retained
  tags and credentials are authoritative instead.
- `spi up` loads the declaration or retained source tags before resolving
  images. It does not derive source policy from credential presence, or wait
  for a post-provision ensure step to replace obsolete sources. The
  `osdu-image-lock` ConfigMap (`src/spi/pins.py`) carries separate projections
  of the trusted-repository roster and source policy, rebuilt at bootstrap
  after their durable records are reconciled. Fork CI can read this
  ConfigMap but cannot read the identity from ARM (ADR-032).
- Canonical resolution through `spi reconcile --refresh-images` or
  `spi service refresh` reads the lock's source projection; lifecycle
  workflows reconcile it to durable intent before refreshing. Changing the
  policy alone does not rewrite a resolved image or an active pin. A first
  provision resolves the desired sources; an existing image changes on the
  next explicit refresh. A pin's reset restores its captured target (below).
- On the shared environment promotion follows a successful fork deploy and
  test run with the required gates active; the reviewed `canonicalSource`
  change records that promotion. A personal or customer operator selects
  the source explicitly. Trust-only onboarding does not require promotion.
- `schema` has a flip precondition its siblings lack: schema-load resolves a
  loader image at the schema service's exact commit (ADR-017), and the fork
  publishes no loader. Schema keeps its community canonical until its fork
  publishes a paired `schema-load` image at the same commit and promotion
  is requested. Trust can be enabled without a loader. A refused promotion
  leaves the durable source as community, so a rebuild cannot infer a flip
  from the retained credential. Publishing a loader alone does not promote
  the service.
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

Rejected: infer canonical source from the credential roster. One durable
record, but it cannot represent a repository trusted for testing while its
service remains on the community image.

Rejected: flip the fleet in one change. Uniform behavior across services, but
it couples eight promotion schedules to the slowest fork.

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
- Source reversal is `spi onboard --canonical-source community`; removal
  also revokes trust. `--remove` records community before deleting the
  credential, then updates the lock projections. Partial failure is reported
  and the operation is re-runnable. Declared changes require the matching
  reviewed declaration first. The next refresh resolves the community image;
  active pins retain their captured restore targets.
- Which source is canonical is readable from `spi onboard --list` and the
  lock's per-service keys, not from operator memory.
- The credentials show trust and the RG tags show source policy when the
  cluster is gone, including the trusted-but-community state. Maintaining two
  durable records requires drift reporting and resumable reconciliation.
