---
status: "proposed"
contact: "danielscholl"
date: "2026-08-25"
deciders: "danielscholl"
---

# Per-Service MR Image Pins on the Live Image Lock

## Context and Problem Statement

Validating an upstream OSDU service fix requires running the exact image the
merge-request pipeline built, on a live cluster, before the fix merges. The
stack previously offered no supported way to do that: operators either edited
the image lock by hand, which the next refresh reverted, or pulled images of
unknown provenance from outside the OSDU pipeline.

## Decision Drivers

- A pin must reference only images built by the service's own MR pipeline.
- A pin must survive `spi reconcile --refresh-images` and a re-run `spi up`.
- Releasing a pin must restore the exact pre-pin image without a re-resolve.
- Pin state must be visible on the cluster, not in operator memory.

## Considered Options

- Pin via the image lock: overwrite the service's lock keys, record
  provenance and the canonical image in one lock annotation.
- Suspend the service's Flux Kustomization and patch the HelmRelease.
- Point the whole deployment at the MR branch via `--image-branch`.

## Decision Outcome

Chosen option: "Pin via the image lock", because the lock is already the
single substitution source for service images and is CLI-owned, so a pin
needs no Flux suspension and no competing object ownership (ADR-017,
ADR-029). `spi service pin <service> --mr <iid>` resolves the MR's head
commit from the community registry, preferring the source branch and falling
back to its `trusted-` copy (the protected ref OSDU maintainers create to
run the containerize pipeline), and patches the lock;
the first pin captures the canonical image in the annotation so
`spi service reset` restores it exactly. The lock re-render paths re-assert
active pins and name them, so a refresh cannot silently revert one. Pinning
`schema` pins the paired loader image when the MR pipeline built one.

Rejected: suspending Kustomizations freezes every sibling under the same
owner and leaves drift correction off.

Rejected: `--image-branch` moves all services to one branch; validation
needs one service moved and thirteen held.

### Consequences

- Good, because MR validation uses only pipeline-built, provenance-clean images.
- Good, because pins are declared on the cluster and listable (`spi service list`).
- Bad, because a pinned lock entry carries no created-at or digest metadata
  until the pin is released and the entry re-resolved.
