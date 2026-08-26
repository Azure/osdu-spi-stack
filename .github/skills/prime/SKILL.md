---
name: prime
description: "Orient in the osdu-spi-stack repo: where decisions and designs live, what is currently changing. Run at session start or when re-orienting after a pause."
---

# Prime

Load enough context to navigate `osdu-spi-stack` and to know where to look for
anything else.

`AGENTS.md` is already in context. It covers the repository map, setup,
validation commands, conventions, and scope rules. Do not re-derive or restate
any of it. Prime adds what AGENTS.md lacks: the live indexes and the current
state of the tree.

Budget: once this skill is loaded, run exactly one shell batch, then write
the report, under 20 lines. Nothing in between. No file reads.

## 1. Capture current state

Run one command:

```bash
git log --oneline -8; git status --short --branch; ls docs/decisions docs/design src/spi
```

The log shows what is moving. The listings are the indexes: ADR and design-doc
filenames carry their titles, and `src/spi` module names describe the CLI's
concerns, so nothing needs opening. The CLI's full surface is defined in
`src/spi/cli.py`; open it only when a task touches the CLI.

Translate to the local shell if needed, keeping the three listings labeled
per directory the way `ls` labels them.

If the shell is unavailable in this environment, say so in one line and go
straight to the report using AGENTS.md alone. Do not reconstruct the batch with
directory-listing tools, web fetches, or subagents; the information is not
worth a second attempt.

## 2. Report

Answer in this shape, then stop. The shape applies to the prime report only;
answer later questions in the session normally, without repeating it:

- **Stack** one sentence on what the repo deploys and by what mechanism
  (restating AGENTS.md here is the intentional exception; it makes the report
  self-contained).
- **Indexes** where decisions and designs live, how many of each (count
  numbered ADRs and substantive design docs, not READMEs or templates), and the rule
  that a change to the deployment model starts with the governing ADR.
- **In flight** the branch, the last few commit subjects, and whether the
  tree is clean, naming the dirty paths if not.
- **Next** two or three candidate entry points, drawn from the stated task if
  there is one, otherwise from what the recent commits touch.

Leave out ADR rulings, test and workflow listings, file counts beyond the two
index totals, and dependency versions. Point at `docs/decisions/` rather than reproducing the
register; the user will ask for a specific record when they want one.

## Out of scope

Source files, test bodies, Bicep modules, Kubernetes manifests, `README.md`,
`pyproject.toml`, `docs/architecture.md`, and individual ADRs and design docs
stay closed during prime. Open them when a task needs them.
