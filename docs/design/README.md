# Design documentation

Start with the [architecture overview](../architecture.md) for system boundaries
and operating assumptions. Use these guides when you need to understand or
change a particular part of the stack.

| Guide | Read it when you need to... |
|---|---|
| [Deployment lifecycle](deployment-lifecycle.md) | Follow `spi up`, distinguish CLI completion from readiness, or recover a partial deployment |
| [Bicep architecture](bicep-architecture.md) | Find the template that owns a resource or understand the limits of `--dry-run` |
| [Flux reconciliation](flux-reconciliation.md) | Trace a blocked dependency, fetch a Git revision, refresh images, or manage service pins |
| [Workload Identity](workload-identity.md) | Separate Azure token exchange from incoming OSDU request authentication |
| [Gateway and ingress](gateway-ingress.md) | Choose an ingress mode or diagnose DNS, TLS, routing, and backend failures |
| [Secret lifecycle](secret-lifecycle.md) | Find credential writers and consumers, or understand rotation limitations |
| [Environment lifecycle](environment-lifecycle.md) | Operate the version-pinned shared environment, its maintenance gate, and its implemented and planned workflows |
| [Fork deployment](fork-deployment.md) | Pin and verify a fork image, restore it by run ownership, or inspect the onboarding roadmap |
| [CI smoke pipeline](ci-smoke.md) | Run the Azure smoke workflow or investigate cleanup failures |

Commands use the installed `spi` executable. From a source checkout, replace
`spi` with `uv run spi`. Commands containing `<placeholders>` need values from
your environment; do not paste them unchanged. Kubernetes commands act on the
current context.

## What belongs where

| Document | Purpose | Maintenance |
|---|---|---|
| Architecture overview | Explain the main components, ownership boundaries, and constraints | Update when the operating model changes |
| Subsystem guide | Explain current behavior, including failure cases and important limits | Update alongside the implementation |
| [ADR](../decisions/README.md) | State a standing decision, its alternatives, and trade-offs | Update through a PR under the decision-register model; Git carries history |
| Operational recipe | Give a supported procedure, expected outcome, and failure handling | Keep beside the subsystem it operates on, or link a dedicated runbook |

A short statement of a governing constraint is useful in a design guide. Link
the ADR for its rationale rather than repeating the argument. If implementation differs
from a standing ADR, describe that difference explicitly instead of presenting
the intended behavior as already implemented.

## Writing and reviewing a guide

Follow [the prose style guide](../STYLE.md). Use headings that match the subject,
not a mandatory template. Start with the
behavior the reader needs to know, rather than "What this explains" or "Why it
matters." A lifecycle may need a timeline; an identity guide may need two
separate request paths.

Before publishing, make sure the guide answers:

- Who writes the state, who reads it, and who keeps it up to date?
- What happens on failure, retry, restart, or partial completion?
- Which limitations and exceptions would change an operator's decision?
- Where can a maintainer find the implementation and the relevant ADR?

Keep exact inventories and tuning values in one place. Link the owning guide
or source instead of copying tables across the overview and every subsystem.
Name project-specific concepts without reteaching standard Kubernetes tooling.

Prefer specific claims over "always," "atomic," "zero secrets," or "everything
is GitOps." Distinguish an observed timing from a timeout or a guarantee, and
state the environment and source of an observation. The
[deployment guide](deployment-lifecycle.md#timing-and-readiness) owns timing
estimates.

For recipes, confirm the command exists and targets the documented resource.
State side effects before commands that mutate or delete anything. Describe
expected conditions rather than inventing CLI transcripts. Use commands and
manifest excerpts from the implementation. If a safe procedure is not implemented, say so rather
than offering the nearest unrelated command.

Use plain language and descriptive headings. No em dashes. Keep historical
debugging logs in issues and PRs, but retain failure mechanisms that help a
reader understand the current system.

## Diagrams and source links

Use a diagram when it explains relationships better than a table. Label
ownership boundaries and distinguish runtime traffic from provisioning or
reconciliation. Show parallel work as parallel, not as a single ordered chain.

Diagrams live in `docs/diagrams/` as editable `.excalidraw` files and exported
`.png` files. Update both together, inspect the rendered image for clipping and
ambiguous arrows, and use descriptive alt text in Markdown.

HTML-authored posters keep their `.html` source beside the exported `.png`,
for example `environment-lifecycle.html`. Edit and render that source when
updating a poster.

End each subsystem guide with links to the source files it explains. When those
files change, review the corresponding prose, diagrams, and recipes together.
