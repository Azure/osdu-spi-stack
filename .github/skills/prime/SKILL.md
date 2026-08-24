---
name: prime
description: "Build a lightweight mental model of the osdu-spi-stack repo: structure, conventions, design decisions. Read this at the start of a session, after a long pause, or when re-orienting."
---

# Prime Codebase Understanding

Build a quick mental model of `osdu-spi-stack` so you can answer questions and navigate
the codebase confidently. The goal is orientation, not deep analysis -- stay under
20k tokens of context and produce a concise summary the user can scan in 30 seconds.

## What NOT to read

Source code (`*.py` files), test bodies, individual Bicep modules, and Kubernetes
manifest contents are off-limits during prime. Only list their existence. Reading
them bloats context without adding orientation value. If deeper analysis is needed,
the user will ask for it separately.

## Phase 1: Project Overview

Read these three files (in parallel). They are small and together give the full
picture of what this project is and how it works.

| File | What to extract |
|------|-----------------|
| `README.md` | Purpose, install path, CLI commands, what gets deployed |
| `.github/skills/prime/reference.md` | Project layout, CLI reference, key design decisions |
| `pyproject.toml` | Python version, dependencies, CLI entry point |

## Phase 2: Structure Map

Run `git ls-files` and summarize the directory tree as a compact table, using the
Project Layout section of `reference.md` as the guide to what each top-level
directory contains. Count files per directory -- do not list individual files.
Also run `git log --oneline -10` to capture recent activity -- include the last
few commit subjects so the user knows what's been changing.

## Phase 3: Architectural Decisions

Read `docs/decisions/README.md` to get the ADR index. This file contains a table of
all architectural decisions with their titles and statuses. Present the full index
so the user (and you) know what decisions have been made and can reference them
later. Do not read individual ADR files -- the index is sufficient for orientation.

## Phase 4: Inventory

Collect these in parallel -- names/counts only, no contents:

### Design docs

```
docs/design/*.md
```

List filenames so the user knows which subsystems have written-up design notes.

### Tests

```
tests/**/*.py
```

Report count and filenames only. Do not read test bodies.

### CI workflows

```
.github/workflows/*.yml
```

List workflow filenames so the user can see what runs in CI.

## Phase 5: Summary

Present a single concise markdown summary with these sections:

- **Project** -- 1-2 sentence description (Azure-native OSDU deploy via AKS Automatic + Bicep + Flux GitOps)
- **Tech** -- Python version, Typer/Rich/Pydantic, uv, Bicep, Flux CD, Helm/Kustomize
- **CLI** -- Key commands: `check`, `up`, `down`, `status`, `info`, `reconcile`, `update`
- **Structure** -- Directory table from Phase 2
- **Decisions** -- ADR index from Phase 3 (titles + status)
- **Design docs** -- Filenames from Phase 4
- **Tests** -- Framework (pytest) and file count
- **Recent activity** -- Last few commit subjects
- **Next steps** -- Suggest what to explore for deeper analysis (e.g., read a specific ADR, look at `docs/design/deployment-lifecycle.md`, run `uv run spi check`)
