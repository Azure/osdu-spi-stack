# ADR Prose Style

Process rules (numbering, statuses, when to write an ADR, templates) live in
[README.md](README.md). This page fixes the prose: what an ADR sounds like,
what it cites, and what a reviewer rejects. House exemplars:
[ADR-012](012-ingress-profiles.md), [ADR-017](017-osdu-image-lock.md),
[ADR-025](025-tls-certificates-in-platform.md).

## Format

Two shapes are in the corpus and both are acceptable:

- **Classic** (ADR 001-018): `**Status**` line, `## Context`, `## Decision`
  with inline `Rejected:` bullets, `## Consequences`.
- **Frontmatter MADR** (ADR 019 onward, the templates): YAML frontmatter,
  `## Context and Problem Statement`, `## Decision Drivers`,
  `## Considered Options`, `## Decision Outcome`, `### Consequences`.

New ADRs use the frontmatter template. Existing ADRs are not converted:
restructuring a closed record churns history without changing what the reader
learns, and the prose rules below apply identically to both shapes.

## The reader

An ADR is read months or years after acceptance, usually by someone deciding
whether a constraint still holds. Write for that reader, not for the PR
reviewer.

- **No moment-in-time status.** "Currently in review", "not yet merged
  upstream", "as of this sprint" decay into archaeology. When a time-bound
  fact is the accepted trade-off, state it as a standing condition: "the
  workflow service can lag Airflow majors", not "the client is on an unmerged
  branch".
- **No external-project narrative.** What a sibling repository tried and what
  it cost them does not justify a decision here. State the rejected mechanism
  and its cost on its own merits.
- **Present tense, status-marked.** Implemented things get simple present
  ("Flux prunes the orphan"). Unbuilt things are named as a state: "unproven",
  "design ahead of implementation", "a known blind spot". Future tense is for
  genuinely future events.

## Voice

- Impersonal and active. The subject of a sentence is the mechanism, not the
  authors: "the policy denies the write", not "we configured the policy to
  deny writes". First person is reserved for confessing a limit ("a token we
  do not hold").
- Declarative confidence. Uncertainty is a named state, never a softened verb.
  Hedge adverbs are banned (word list below).
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
  line counts. Never round into vagueness.
- State the trade-off in the same breath as the decision: "X delivers
  immutability, not authenticity". A benefit listed without its paired cost is
  incomplete.
- Name the structural failure mode that motivated the design, not the
  incident. "A status-less Certificate passes Flux's health checks" ages
  well; cluster names, dates, and timeout values do not (see README: no
  incident narrative).

## Alternatives

- One line per rejected option, and the line keeps the option's real
  advantages intact. A rejection that reads as a strawman signals the
  comparison was never made.
- The construction "X, not Y" is the house move for fixing a boundary against
  a plausible misreading: "the lock is generated, not committed". Use it to
  correct real misreadings, not invented ones.

## Mechanics

- Say a thing once. Not prose, then a bold restatement, then a summary line.
  ADRs do not end with a recap; the last section is Consequences.
- Prose argues; bullets enumerate; tables carry fixed row sets with identical
  column semantics. A causal chain never rides in a bulleted list.
- The bold-label bullet is the standard compound form:
  `- **Tag churn.** Tags get pruned. A chart that names a SHA tag...`
  The label is a noun phrase or short claim, never a sentence duplicating what
  follows.
- Headings are noun phrases or short declarative claims ("Resolution is
  deterministic"), never questions. No H4 or deeper.
- No em dashes. Colons, commas, semicolons, and parentheses carry the load.
- Consequences mix good and bad unsorted, and the honest limitation is worth
  leading with. "What becomes easier, what becomes harder, what we now have
  to maintain, what we are accepting."
- Code blocks hold real artifacts (commands that run, YAML that ships), not
  illustrative pseudo-code.

## Word list

| Class | Words | Rule |
|---|---|---|
| Marketing | leverage, utilize, seamless, robust, powerful, streamline, cutting-edge | Banned |
| Hedges | probably, likely, perhaps, arguably, it seems, we believe | Banned; name the state instead |
| Filler | in order to, note that, it should be noted, obviously, clearly, of course | Banned |
| Dismissives | simply, just, easily | Banned when they minimize work the reader must still do |
| Intensifiers | all, every, always, complete | Only when literally, enumerably true |
| Crutches | deliberately, posture, "is what lets", "exists precisely because" | One per ADR; several read generated, not written |

## Review checklist

Reject an ADR that:

1. Contains a dateable status ("currently", "in review", "not yet merged").
2. Justifies a choice by another project's history.
3. Hedges instead of naming an uncertainty as a state.
4. States the same idea more than once in different forms.
5. Ends with a summary, a maxim, or a call to action.
6. Explains standard tooling (Helm, Flux, ADRs themselves) to the reader.
7. Narrates its own virtues ("stated honestly", "this ADR makes clear").
8. Lists a benefit whose paired cost appears nowhere.
9. Rejects only strawman alternatives.
10. Uses banned words or em dashes.

A mechanical first pass:

```bash
grep -nwiE "currently|leverage|utilize|seamless|robust|streamline|obviously|probably|likely|perhaps|arguably" docs/decisions/0*.md
grep -nE "in review|will soon|note that|in order to|we believe|—" docs/decisions/0*.md
```

`-w` matters: without it, API values such as Karpenter's
`WhenEmptyOrUnderutilized` match the word list. A hit is a prompt to read the
sentence, not an automatic failure; a banned word inside a quoted identifier
or error message stays.
