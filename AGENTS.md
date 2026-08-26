# AGENTS.md

Azure-native OSDU deployment. A Python CLI (`spi`) provisions Azure infra with
`az` + Bicep, bootstraps AKS Automatic, then Flux CD reconciles Kubernetes
workloads from this repository. Repo: `Azure/osdu-spi-stack`.

`uv` is the only tool needed for repo work; it manages Python, dependencies, and
every command below. Deploying to Azure also needs `az`, `bicep`, `kubectl`,
`kubelogin`, and `flux`, which `uv run spi check` verifies.

## Repository map

| Path | What lives here |
|------|-----------------|
| `src/spi/` | The `spi` CLI (Typer + Rich + Pydantic) |
| `infra/` | Bicep templates for Azure PaaS provisioning |
| `software/` | Helm charts, middleware components, OSDU service manifests |
| `docs/` | Architecture, ADRs (`decisions/`), design docs, prose `STYLE.md` |
| `scripts/`, `tests/` | Helper scripts and the pytest suite |

Directory and file names are self-describing; explore per task rather than from
a maintained map. The CLI's command surface is defined in `src/spi/cli.py`.

## Working commands

```bash
uv sync                             # install the CLI plus pytest, ruff, ty, pre-commit
uv run pre-commit run --all-files   # lint, format, types, and tests in one pass
uv run spi --help                   # sanity-check the CLI itself
```

From a checkout, invoke the CLI as `uv run spi`. The bare `spi` commands in
`README.md` assume the released wheel installed via `uv tool install`; do not
expect `spi` on PATH here.

`pre-commit` lints, formats, type-checks (`ty`), and tests in one pass. The
ruff hooks auto-fix what they can and fail the run so you re-stage the
corrected files. Run it before every PR; run individual tools from
`.pre-commit-config.yaml` when iterating on one kind of failure.

## Conventions

- **Commits and PR titles** follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat`, `fix`, `docs`, `refactor`, `test`, `ci`, `style`, `chore`).
  Squash-merge uses the PR title as the release-note subject, so it must conform.
  PR descriptions follow the shape in `CONTRIBUTING.md`: why first, then what
  changed, then honest validation results.
- **Branches** are named `<type>/<short-name>`, for example `feat/add-redis-component`.
- **Code**: ruff line length 100 with import sorting; keep `ty` clean.
- **Prose**: no em dashes; use commas, periods, or semicolons. Files under `docs/`
  additionally follow `docs/STYLE.md`.
- **Comments** only where they add something the code cannot say: cross-file
  coupling, an external contract, or why the obvious approach was not taken.
  Keep them to a line or two; delete comments that restate the code.
- **Transparency**: every `az` and `kubectl` command the CLI runs is shown to the
  user via a Rich panel before execution.

## Scope notes

- **Azure only.** When reading cloned OSDU service repos, only `*-azure/` providers
  (and shared `*-core/`) matter; skip `*-aws/`, `*-gc/`, `*-ibm/`, `*-core-plus/`.
- **No stored credentials.** Workload Identity is the only data-plane path; local
  (key/SAS) authentication is disabled on Cosmos and Service Bus (ADR-023).
  Never commit secrets or hardcode credentials.
- **Decisions are records.** `docs/decisions/` governs the deployment model. Read
  the record that owns a subject before changing it; `docs/design/` explains how
  the subsystems actually work.
