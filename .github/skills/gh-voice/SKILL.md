---
name: gh-voice
description: Write and edit PR descriptions, issues, comments, and review replies in the concise, factual house style of osdu-spi-stack.
---

# Voice for PRs, Issues, and Comments

This skill governs wording, not structure. PR descriptions follow
`CONTRIBUTING.md` under "PR Descriptions" and the template in
`.github/PULL_REQUEST_TEMPLATE.md`. Issues follow the forms in
`.github/ISSUE_TEMPLATE/`.

## Principles

- **Lead with the result.** Start with the change, observed problem, or
  review verdict. Follow with the reason and supporting evidence.
- **Keep essential rationale.** Explain unusual choices, trade-offs, and
  limitations a reader needs to understand the change. Reproduction steps
  and results that substantiate a verdict are evidence, not process
  narration. Omit investigation chronology and unrelated follow-ups.
- **Preserve meaning.** Simplify prose without adding or changing claims.
  Keep precise technical terms. State uncertainty explicitly rather than
  presenting unverified behavior as fact.
- **Be direct and respectful.** Remove filler ("worth noting"), marketing
  language ("seamless"), and self-evaluation ("the honest fix"). Omit generic
  praise and acknowledgments that add no information. Contractions are fine.
- **Use only useful structure.** Group changes by concern, not by file.
  Use headings that name the subject; remove empty sections and repeated
  explanations. No generic validation checklists; mention checks when the
  evidence or a validation limit matters.
- **Preserve literal text.** Replace em and en dashes in prose with commas,
  periods, colons, or parentheses. Leave commands, identifiers, URLs, and
  quoted evidence unchanged.

A review comment names the defect and consequence, then the fix when clear.
A reply states the verdict, evidence, and relevant commit or line link.
When declining a suggestion, give the technical reason. One to three
sentences usually suffice; include more when the evidence needs it.

## Cut on sight

- **Praise and acknowledgment.** "Great point", "you're absolutely right",
  "thanks for the catch", "just to confirm my understanding".
- **Soft openers.** "Consider", "it might be worth", "worth noting". Name
  the defect and its consequence instead.
- **Empty status.** "Fixed in the latest push" says nothing. Say what the
  code does now and where: "`upload-database: false` now, since
  `upload: never` only covers SARIF. d3cd529."
- **Reader stage-direction.** "The pin is the part to argue with", "worth
  opening that log". Spend the words on the risky thing itself.
- **Process narration.** "Preflight caught four defects", "addresses review
  feedback from round 2". State the resulting design.
- **Author caveats.** "I could not verify deployment" becomes "Deployment is
  unverified because the test subscription was unavailable."

Two tells that read as generated even with plain words: grading the fix
("honest", "principled", "proper", "clean") and calling a check a "signal".
Say what the fix does and what the check checks.

## Words

| inflated | plain |
|---|---|
| the canonical X | the template owns X |
| degraded to a clean skip | skipped silently |
| a destructive deletion path | that script deletes registry tags |
| the honest fix is to X | X |
| a failure there is quiet | it logs the error instead of failing the check |

Also avoid "leverage", "robust", "seamless", "comprehensive", "holistic",
"delve", and "ensure" where "make sure" reads better.

## Cleanup pass

1. Remove repetition, investigation chronology, and empty sections.
2. Simplify wording without changing technical meaning.
3. Check that essential rationale, evidence, uncertainty, and literal text
   survived the edit.
