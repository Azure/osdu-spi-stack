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
`style`, `chore`) are auto-extracted into release notes by the release
workflow. Commits that do not match a recognized prefix are silently omitted
from the auto-categorized notes; this is benign (cleanups and merges do not
need to appear).

### PR Titles

PR titles must follow Conventional Commits because GitHub squash-merge uses
the PR title as the commit subject. A non-conforming title would silently
disappear from auto-generated release notes.

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
8. Squash-merge via `gh pr merge` or the GitHub UI. Squash on merge keeps
   `main` linear and ensures the PR title becomes the commit subject (used
   by release notes generation).

## Cutting a Release

Releases are label-driven and automated on merge to `main`. To cut one, apply
`release:patch`, `release:minor`, or `release:major` to your pull request
before merging. The release workflow computes the next semver from the latest
git tag, generates release notes from conventional commits since that tag,
pushes the tag, builds the wheel via `uv build`, and creates a GitHub Release
with the wheel attached as an asset.

End users install via:

```bash
uv tool install https://github.com/Azure/osdu-spi-stack/releases/download/vX.Y.Z/spi-X.Y.Z-py3-none-any.whl
```

After install, the `spi` binary is on PATH; no `uv run` prefix.

## License

By contributing, you agree that your contributions will be licensed under the
[Apache License 2.0](LICENSE).
