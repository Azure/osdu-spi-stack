# Contributing to OSDU SPI Stack

Thank you for your interest in contributing to OSDU SPI Stack. This guide covers
how to set up your development environment, make changes, and submit them for
review.

## Prerequisites

The only required tool is [`uv`](https://docs.astral.sh/uv/). Verify your
environment:

```bash
uv run spi check
```

This reports which tools are installed, which are missing, and how to install
them.

## Development Setup

```bash
git clone https://github.com/Azure/osdu-spi-stack.git
cd osdu-spi-stack

# Sync dev dependencies (pytest, ruff, ty, pre-commit, etc.)
uv sync

# Verify CLI runs
uv run spi --help

# Run prerequisite checks
uv run spi check
```

### Pre-commit hooks

Install once per clone to catch lint, format, type, and test regressions at
commit time:

```bash
uv run pre-commit install
```

The hooks run on every `git commit` and check the staged Python files against
ruff (lint + format with auto-fix), `ty` (type check), and pytest. Same scope
as the corresponding CI jobs, so anything pre-commit accepts will also pass
CI.

To run all hooks against the whole tree without committing:

```bash
uv run pre-commit run --all-files
```

If a hook auto-fixes a file, the commit fails so you can review the change and
re-stage. Skip hooks (rare, last resort) with `git commit --no-verify`.

## Project Structure

| Directory | Contains |
|-----------|----------|
| `src/spi/` | Python CLI (Typer + Rich + Pydantic) |
| `infra/` | Bicep templates for Azure PaaS provisioning |
| `software/components/` | Middleware Kubernetes manifests |
| `software/stacks/osdu/` | OSDU service deployments and profiles |
| `docs/decisions/` | Architecture Decision Records (ADRs) |
| `docs/design/` | Subsystem design documents |
| `.github/skills/` | Portable AI agent skills |

## Making Changes

### Branch Naming

Use descriptive branch names with a type prefix:

```
feat/add-redis-component
fix/storage-class-binding
docs/update-adr-index
chore/bump-deps
```

### Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/) format:

```
feat(cli): add --dry-run flag to up command
fix(bicep): handle missing partition tag gracefully
docs(adr): add ADR-012 for ingress profiles
chore(deps): bump typer to 0.24
refactor(providers): extract shared cluster validation
```

The recognized prefixes (`feat`, `fix`, `docs`, `refactor`, `test`, `ci`,
`style`, `chore`) are parsed by release-please from the squashed commit
subject. `feat` bumps minor, a breaking `!` bumps major (minor while
pre-1.0), everything else bumps patch; each type gets its own changelog
section.

### PR Titles

PR titles must follow Conventional Commits: squash-merge makes the title the
commit subject, and release-please computes versions and changelogs from it.
The `pr-title` required check enforces this; editing the title re-runs it.

### PR Descriptions

The description is the durable record of why a change happened; squash-merge
keeps only the title in history. Structure it in three parts:

1. **Why**: the problem or measurement that motivated the change, with the
   number or named artifact that demonstrates it. A reviewer should
   understand the motivation before reading any diff.
2. **What changed**: grouped by concern rather than by file, one line each.
   Do not restate the diff.
3. **Validation**: which checks ran and their results, reported honestly.
   A check that could not run is listed with the reason, never omitted.

Keep the whole description under roughly 20 lines.

## Validation Before Submitting

Before opening a pull request:

1. **Run the pre-commit hooks** against the whole tree:
   `uv run pre-commit run --all-files`. Covers ruff lint, ruff format, `ty`
   type check, and pytest. Same checks the CI validate jobs run.
2. **Verify the CLI works**: `uv run spi --help`
3. **Run prerequisite checks**: `uv run spi check`
4. **Test locally** if possible: deploy with `uv run spi up --env dev1` and
   verify with `uv run spi status`

## Submitting Changes

All changes reach `main` through a pull request. Direct pushes to `main` are
blocked by branch protection.

1. Create a feature branch locally: `git checkout -b <type>/<short-name>`.
2. Commit with a [Conventional Commits](https://www.conventionalcommits.org/)
   message.
3. Push the branch to the remote.
4. Open a pull request via `gh pr create` or through the GitHub UI.
5. Wait for the CI checks to pass. A failing check blocks merge.
6. Resolve all review threads. Unresolved threads block merge.
7. Obtain approval from a code owner listed in
   [`.github/CODEOWNERS`](.github/CODEOWNERS).
8. Squash-merge via `gh pr merge` or the GitHub UI. Squash is the only
   enabled merge method; it keeps `main` linear and makes the PR title the
   commit subject that release-please versions from.

## Cutting a Release

Releases are driven by [release-please](https://github.com/googleapis/release-please).
Every merge to `main` opens or updates a standing pull request titled
`chore: release X.Y.Z` that carries the changelog and the computed version.
To cut a release, merge that PR: release-please tags `vX.Y.Z` and creates a
draft GitHub Release; the assets job builds the wheel and sdist via
`uv build`, uploads them, and publishes.

To cut a specific version instead of the computed one (for example the jump
to `1.0.0`), open a PR adding `"release-as": "X.Y.Z"` to the `"."` package
block of `.release-please-config.json`, then remove it in a follow-up PR
after the release ships; leaving it in place pins every future release to
that version.

End users install via:

```bash
uv tool install https://github.com/Azure/osdu-spi-stack/releases/download/vX.Y.Z/spi-X.Y.Z-py3-none-any.whl
```

After install, the `spi` binary is on PATH; no `uv run` prefix.

## License

By contributing, you agree that your contributions will be licensed under the
[Apache License 2.0](LICENSE).
