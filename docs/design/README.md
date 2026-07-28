# Design Documentation

This section holds narrative design explainers for the SPI Stack platform. Unlike the architectural overview in [`architecture.md`](../architecture.md) or the individual decision records in [`decisions/`](../decisions/), design docs zoom in on specific subsystems and explain how they actually work, paired with diagrams.

## How this section relates to the others

| Document type | Purpose | Lifecycle |
|---|---|---|
| [`architecture.md`](../architecture.md) | 30,000-ft overview of the whole system | Updated when the top-level model changes |
| [`decisions/`](../decisions/) (ADRs) | Why a specific choice was made, with alternatives considered | Immutable after acceptance; superseded by new ADRs |
| [`design/`](./) (this section) | How a subsystem actually works, with diagrams | Living documents; updated as code evolves |

Design docs reference ADRs for decision rationale. They do not re-justify decisions. If a doc finds itself arguing "we chose X instead of Y," that belongs in an ADR.

## Design explainers

| Doc | Answers |
|---|---|
| [Deployment lifecycle](deployment-lifecycle.md) | What actually happens in the ~45-50 minutes that `spi up` takes, from CLI invocation to a healthy cluster |
| [Bicep architecture](bicep-architecture.md) | How `infra/` is organised, why three top-level templates exist, and where the imperative seams live |
| [Flux reconciliation](flux-reconciliation.md) | The layer DAG, `dependsOn` mechanics, the `osdu-image-lock` substitution loop, suspend and resume |
| [Workload Identity](workload-identity.md) | One UAMI, one ServiceAccount, the token federation chain, what the JWT projection in ADR-016 does after the bearer arrives |
| [Gateway and ingress](gateway-ingress.md) | The three ingress modes (`azure`, `dns`, `ip`) concretely, what each provisions, how to switch, how to debug a 404 |
| [Secret lifecycle](secret-lifecycle.md) | The three secret stores, what Bicep writes vs what the CLI writes post-handoff, how trust-manager mirrors CAs into `osdu` |

## Doc template

Every design doc should include these sections, in order:

1. **What this explains** -- one sentence describing the scope.
2. **Why it matters** -- one or two sentences on the developer pain this doc removes.
3. **How it works** -- the narrative body. Lead with a diagram where possible.
4. **Concrete examples or recipes** -- at least one worked example that a reader can run or trace.
5. **Related ADRs** -- bulleted list linking to `../decisions/NNN-*.md`.
6. **Source files** -- bulleted list of files in the repo this doc stays consistent with.

When writing a new design doc, copy the shape of an existing one and replace the content.

## Diagram convention

- Diagrams live in `docs/diagrams/` as `.excalidraw` source plus an exported `.png`.
- The filename stem matches the doc stem. Multiple diagrams for one doc use a suffix.
- Markdown references the `.png` so it renders inline on GitHub.
- The `.excalidraw` source is the editable truth. When you update a diagram, edit the source in [Excalidraw](https://excalidraw.com/), re-export the PNG, and commit both.

## Contribution guide

**Add a new design doc when** a reader would benefit from a narrative plus a diagram to understand a subsystem, and the information is not already obvious from `architecture.md` or a single ADR.

**Update an existing design doc when** the code it references changes shape (file paths, function names, YAML field names). The Source files list at the bottom of each doc is the consistency anchor.

**Write a new ADR instead when** you are making a fresh architectural choice, not explaining an existing one. Design docs document the present; ADRs capture the decision moment.

**Writing style**: no em dashes, no incident narrative, descriptive headings.
