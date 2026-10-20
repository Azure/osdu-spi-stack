# ADR-033: Explicit Canonical Image Source Policy

## Context

Every OSDU service the stack deploys has an `Azure/osdu-spi-<service>` repository that carries the Azure provider and publishes its `main` line to GHCR, with digest identity and a 30-day retention on `sha-*` versions. The community GitLab registry no longer ships Azure provider code, so its images cannot run on this stack. A service can still need to run some other repository's build for a sustained period, for example a mirror of its Azure repository that an operator develops against, and that choice is environment state, not CLI code. The canonical entry is what a pin restores to, what refresh re-resolves, and what the environment runs between deploys, so each service has exactly one canonical source at a time.

## Decision

Every service follows its Azure repository unless the environment records a trusted mirror for it, one service at a time.

- **Azure is the default.** A service with no recorded source resolves the image `Azure/osdu-spi-<service>`'s `main` last published: the digest `main-snapshot` names, recorded under the `sha-<12>` tag of the commit that built it (`azure_source` and `resolve_images` in `src/spi/images.py`). Schema's loader is `ghcr.io/azure/osdu-spi-schema-load` at the same commit. No trust record is needed; the packages are public.
- **A mirror is explicit and trusted.** The resource-group tag `spi-source-<service>` records `azure` or `<owner>/<repo>`. A missing tag means `azure`, and a legacy `community` value reads as `azure`. A mirror source must match the repository the deploy identity trusts for that service through its `fork-<service>` credential (ADR-032). Both records survive `spi down` (ADR-034). An unreadable or invalid tag is an error.
- **Package naming is shared.** A source repository `<owner>/<repo>` publishes `ghcr.io/<lowercase-owner>/<lowercase-repo>`, or `ghcr.io/<lowercase-owner>/<service>` when its `SERVICE_NAME` variable names the service; schema's loader is that package plus `-load` at the schema commit. A missing or private package fails resolution and never falls back to another source.
- **Selection.** `spi onboard --canonical-source fork` makes the trusted repository the service's mirror; `--canonical-source azure` returns it to Azure without revoking trust. Omitting the option keeps the recorded source. Declared environments carry `canonicalSource` (`azure` or `fork`, default `azure`; `community` is read as `azure`) in each `forks:` entry, and the declaration is authoritative over the tags and credentials it reconciles. Removing an entry records `azure` and revokes its credential.
- **Projection.** `spi up` loads the declaration or retained tags before resolving images. The `osdu-image-lock` ConfigMap (`src/spi/pins.py`) carries the trusted-repository roster and the mirror policy as separate annotations, rebuilt at bootstrap; `spi service refresh` and `spi reconcile --refresh-images` read them. Changing the policy alone does not rewrite a resolved image or an active pin; the next refresh applies it.
- **Schema's precondition.** Schema follows a mirror only when the mirror published the paired loader at the same commit; onboarding refuses the promotion otherwise and schema stays on Azure.
- **Retention.** Every canonical is a GHCR image under retention, so the weekday refresh (`spi service refresh --forks`) re-resolves every service, and `spi status` and `spi info` mark a canonical built more than 21 days ago. A pin's restore target is captured when the pin is written (ADR-017), so a policy change while a pin is active does not retarget it.

Rejected: community GitLab images as the default. Tracks upstream with no fork to maintain, but those images no longer carry the Azure provider.

Rejected: Azure as the only source, with no mirror policy. One less durable record, but an operator could not run a mirror's `main` as the canonical without a CLI change.

Rejected: infer the source from the credential roster. One durable record, but it cannot represent a repository trusted for ephemeral deploys while its service stays on Azure.

Rejected: dual-source fallback per service. Resilient to a missing image, but two possible answers for one canonical entry make "what should this service run" a runtime question.

## Consequences

- A bare `spi up` deploys the same image lineage the shared environment runs and the Azure repositories' gates certify; there is no image branch to choose.
- Every environment inherits the retention coupling: if the weekday refresh stalls past the retention window while builds continue, a recorded canonical can age into a deleted version whose digest is no longer pullable.
- An environment that recorded `community` before this policy keeps working: the tag reads as `azure`, and `spi info` marks each running community image `azure (next refresh)` until a refresh moves it.
- Two durable records (credentials for trust, tags for source) need drift reporting and resumable reconciliation; `spi onboard --list` shows both when the cluster is gone.
