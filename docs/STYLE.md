# Documentation Prose Style

This page fixes the prose for the repo's documentation: what it sounds like,
what it cites, and what a reviewer rejects. It governs ADRs
([`decisions/`](decisions/)), design docs ([`design/`](design/)), and
[`architecture.md`](architecture.md). Structural rules stay with each genre:
ADR process in [decisions/README.md](decisions/README.md), the design-doc
template in [design/README.md](design/README.md).

House exemplars: [ADR-012](decisions/012-ingress-profiles.md),
[ADR-017](decisions/017-osdu-image-lock.md),
[ADR-025](decisions/025-tls-certificates-in-platform.md),
[design/ci-smoke.md](design/ci-smoke.md).

## Voice

- Impersonal and active. The subject of a sentence is the mechanism, not the
  authors: "the policy denies the write", not "we configured the policy to
  deny writes". First person is reserved for confessing a limit ("a token we
  do not hold"). Second person is for text whose reader performs a task
  (debugging recipes, runbook steps), nowhere else.
- Declarative confidence. Uncertainty is a named state ("unproven", "a known
  blind spot"), never a softened verb. Hedge adverbs are banned (word list
  below).
- Present tense. Future tense is for genuinely future events.
- Idiom is allowed when it does work ("footgun", "blast radius",
  "break-glass"); decoration is not. A metaphor introduced once may be reused
  as terminology; extended metaphors and analogies are out.

## Justifying claims

A claim is backed by a named artifact, an exact number, or an admission that
it is not yet backed. Nothing rests on best practice or belief.

- Point at the thing: file paths, resource names, chart keys, a `kubectl`
  command. "Debuggable with `kubectl get cm osdu-image-lock -o yaml`" beats
  "easy to debug".
- Keep numbers exact where one exists: instance counts, versions, timeouts,
  observed timings. Never round into vagueness.
- State the trade-off in the same breath as the decision: "X delivers
  immutability, not authenticity". A benefit listed without its paired cost is
  incomplete.
- Name the structural failure mode that motivated a design, not the incident.
  "A status-less Certificate passes Flux's health checks" ages well; cluster
  names, dates, and one-off timeout values do not.
- No external-project narrative. What a sibling repository tried and what it
  cost them does not justify a choice here. State the rejected mechanism and
  its cost on its own merits.

## Mechanics

- Say a thing once. Not prose, then a bold restatement, then a summary line.
  Nothing ends with a recap, a maxim, or a call to action.
- Prose argues; bullets enumerate; tables carry fixed row sets with identical
  column semantics. A causal chain never rides in a bulleted list.
- The bold-label bullet is the standard compound form:
  `- **Tag churn.** Tags get pruned. A chart that names a SHA tag...`
  The label is a noun phrase or short claim, never a sentence duplicating what
  follows.
- Headings are noun phrases or short declarative claims ("Resolution is
  deterministic"), never questions. No H4 or deeper.
- No em dashes. Colons, commas, semicolons, and parentheses carry the load.
- Code blocks hold real artifacts (commands that run, YAML that ships), not
  illustrative pseudo-code.
- Do not explain standard tooling (Helm, Flux, Bicep, ADRs themselves) to the
  reader; the audience is assumed expert.

## Word list

| Class | Words | Rule |
|---|---|---|
| Marketing | leverage, utilize, seamless, robust, powerful, streamline, cutting-edge | Banned |
| Hedges | probably, likely, perhaps, arguably, it seems, we believe | Banned; name the state instead |
| Filler | in order to, note that, it should be noted, obviously, clearly, of course | Banned |
| Dismissives | simply, just, easily | Banned when they minimize work the reader must still do |
| Intensifiers | all, every, always, complete | Only when literally, enumerably true |
| Crutches | deliberately, posture, "is what lets", "exists precisely because" | One per page; several read generated, not written |

## ADRs

An ADR is a closed record, read months or years after acceptance by someone
deciding whether a constraint still holds. Write for that reader, not for the
PR reviewer.

- **No moment-in-time status.** "Currently in review", "not yet merged
  upstream", "as of this sprint" decay into archaeology. When a time-bound
  fact is the accepted trade-off, state it as a standing condition: "the
  workflow service can lag Airflow majors", not "the client is on an unmerged
  branch".
- **Rejected options keep their real advantages**, one line each. A rejection
  that reads as a strawman signals the comparison was never made. The
  construction "X, not Y" is the house move for fixing a boundary against a
  plausible misreading: "the lock is generated, not committed".
- **Consequences mix good and bad unsorted**, and the honest limitation is
  worth leading with. "What becomes easier, what becomes harder, what we now
  have to maintain, what we are accepting."

Two format shapes are in the corpus and both are acceptable:

- **Classic** (ADR 001-018): `**Status**` line, `## Context`, `## Decision`
  with inline `Rejected:` bullets, `## Consequences`.
- **Frontmatter MADR** (ADR 019 onward, the templates): YAML frontmatter,
  `## Context and Problem Statement`, `## Decision Drivers`,
  `## Considered Options`, `## Decision Outcome`, `### Consequences`.

New ADRs use the frontmatter template. Existing ADRs are not converted:
restructuring a closed record churns history without changing what the reader
learns.

## Design docs

A design doc is a living document, updated in place as the code evolves
([design/README.md](design/README.md) holds the section template and
lifecycle).

- **Status-marked present tense replaces the ADR durability rule.** A living
  doc may and should carry current state, marked as such: "unproven",
  "defensive against a future precondition job". The mark is what keeps the
  doc honest between updates.
- **Design docs explain; ADRs justify.** A design doc that argues "we chose X
  instead of Y" is carrying an ADR's content; move it and link.
- **Recipes are imperative.** Debugging and how-to sections address the
  reader directly and end with the observable state that proves success.

## Review checklist

Reject a page that:

1. Hedges instead of naming an uncertainty as a state.
2. Justifies a choice by another project's history.
3. States the same idea more than once in different forms.
4. Ends with a summary, a maxim, or a call to action.
5. Explains standard tooling to the reader.
6. Narrates its own virtues ("stated honestly", "this document makes clear").
7. Lists a benefit whose paired cost appears nowhere.
8. Uses banned words or em dashes.
9. (ADRs) Contains a dateable status or rejects only strawman alternatives.
10. (Design docs) Re-justifies a decision an ADR already owns.

A mechanical first pass:

```bash
grep -nwiE "currently|today|leverage|utilize|seamless|robust|streamline|obviously|probably|likely|perhaps|arguably" docs/decisions/0*.md docs/design/*.md docs/architecture.md
grep -nE "in review|will soon|note that|in order to|we believe|—" docs/decisions/0*.md docs/design/*.md docs/architecture.md
```

`-w` matters: without it, API values such as Karpenter's
`WhenEmptyOrUnderutilized` match the word list. A hit is a prompt to read the
sentence, not an automatic failure; a banned word inside a quoted identifier
or error message stays, and "currently" or "today" is legitimate in a design
doc when it marks status ("currently impossible, but defensive against a
future precondition job"). "Current" stays out of the pattern: its indexical
use ("tracks the current Airflow major") is a standing condition, and the
dateable use ("current AKS restricts") is rare enough to catch by reading.
