# Copilot instructions

`AGENTS.md` at the repository root is the source of truth for setup, working
commands, conventions, and scope rules. Read it first; nothing here repeats it.

Skills under `.github/skills/` carry the repo's working discipline:

- `prime` orients in the tree at the start of a task.
- `gh-voice` governs the tone of every PR description, issue, and comment.
- `resolve-review-threads` is the loop for addressing review comments on a
  pull request: verify each finding before changing code, reply with
  evidence, resolve the thread.
- `code-review` is the rubric for reviewing pull requests in this repo: the
  invariants a diff can break silently, and the classes of comment not to
  leave.

Path-scoped rules live in `.github/instructions/`.

When reviewing a pull request, use the `code-review` skill and write comments
in the `gh-voice` style. When addressing review comments, use
`resolve-review-threads`. When writing a PR description, follow the shape in
`CONTRIBUTING.md` under "PR Descriptions" and the tone in `gh-voice`.

Commits and PR titles follow Conventional Commits. Do not add generated-with
footers, co-author trailers for tools, or session links to commits, PR
descriptions, or comments.
