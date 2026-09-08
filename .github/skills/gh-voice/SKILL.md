---
name: gh-voice
description: House tone for PR descriptions, issues, and PR or issue comments in this repo. Use when writing any of those, when reviewing a pull request and leaving comments, or when asked to tighten or clean one up.
---

# Voice for PRs, Issues, and Comments

One tone for everything written on GitHub in this repo: PR descriptions,
issues, review comments, review replies. Each tells the reader what is true
and what to do. None is a record of how the work went.

Structure is owned elsewhere. PR descriptions follow the three-part shape in
`CONTRIBUTING.md` under "PR Descriptions" and the template in
`.github/PULL_REQUEST_TEMPLATE.md`. Issues follow the forms in
`.github/ISSUE_TEMPLATE/`. This skill governs how the words read inside
those shapes.

## Principles

**Write about the change, not about writing the change.** The reviewer sees
`main...HEAD`. A bug introduced and fixed inside the branch never existed for
them. Sections named "Rejected alternatives" or "What I could not verify" are
process narration wearing a heading.

- An alternative earns a mention only when the diff looks wrong without it.
  Then it is a note explaining the code, in Notes.
- An open risk is stated as system behavior, not as a caveat about the
  author. "The switch first runs Monday and logs failures instead of failing
  a check" tells the reader what to do. "I could not verify this" does not.

**Headings are labels, not theses.** Use a flat noun (Notes, Risk) or name
the subject. A section carrying one fact is a sentence, not a section.

**Drop any section with nothing real to say.** An empty section trains
readers to skip the filled ones.

## Issues

- **Problem**: what is observed, with evidence a reader can chase: exact
  error text, the run or PR link, the file and line. One paragraph.
- **Cause**: only when known. Say what the code does that produces the
  problem, not how it was found.
- **Required change**: numbered, each item checkable. A recommendation, not a
  menu.

## Comments and review replies

Lead with the outcome or the answer, then the reason, in one to three
sentences. Link the commit or line instead of quoting the diff.

- No thanks, no praise, no restating the other person's comment. "Great
  point", "you're absolutely right", "just to confirm my understanding" all go.
- "Fixed in the latest push" says nothing. Say what the code does now and
  where: "`upload-database: false` now, since `upload: never` only covers
  SARIF. d3cd529."
- Declining a suggestion: one sentence on what the code does and why. No
  apology, no "happy to change it if you prefer".
- A review comment names the defect and the consequence, then the fix if it
  is obvious. It does not open with "Consider" or "It might be worth".

## Cut on sight

- **Reader stage-direction.** "The pin is the part to argue with", "worth
  opening that log". Spend words on the risky thing instead of pointing at
  it.
- **Process narration.** "Preflight caught four defects", "addresses review
  feedback from round 2". State the resulting design.
- **Backlog and follow-up sections.** File an issue and leave the PR alone.
- **Test-plan checklists.** CI results are on the pull request. Name a check
  only when how it was proven is itself the interesting part.
- **File-by-file enumeration.** Group by behavior.
- **Restatement.** If the commit message or the diff already says it, cut it.

## Words

No em dashes or en dashes anywhere. A comma, colon, period, or parentheses
always works.

Plain engineer vocabulary:

| inflated | plain |
|---|---|
| the canonical X | the template owns X |
| ADR-023 posture | (ADR-023) |
| four defects | four bugs |
| degraded to a clean skip | skipped silently |
| a destructive deletion path | that script deletes registry tags |
| the contract this consumes | the contract this uses |
| grows a new mode | gets a new mode |
| by approved decision | as agreed |
| the honest fix is to X | X |
| a failure there is quiet | it logs the error instead of failing the check |

Two tells that read as generated even with plain words: grading the fix
("honest", "principled", "proper", "clean") and calling a check a "signal".
Say what the fix does and what the check checks.

Also avoid: "worth noting", "notably", "leverage", "robust", "seamless",
"comprehensive", "holistic", "delve", and "ensure" where "make sure" reads
better. Contractions are fine. Prose with none of them reads stiff enough to
be a tell on its own.

## Cleanup pass

When asked to tighten an existing description or comment:

1. Delete process narration and follow-up sections. That is usually most of
   the excess.
2. Replace every dash.
3. Run the vocabulary swaps.
4. Re-read Notes. Anything that reads as history, restate as design or cut.
5. Check: `gh pr view <n> --json body --jq '.body' | grep -c '—'` returns 0.

Preserve the load-bearing why while cutting. A note explaining why an
odd-looking line exists is the reason a reviewer does not have to ask.
