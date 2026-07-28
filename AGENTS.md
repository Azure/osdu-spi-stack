# AGENTS.md

Azure-native OSDU deployment. A Python CLI (`spi`) provisions Azure infra with
`az` + Bicep, bootstraps AKS Automatic, then Flux CD reconciles Kubernetes
workloads from this repository. Repo: `Azure/osdu-spi-stack`.

The only tool you need for repo work is [`uv`](https://docs.astral.sh/uv/); it manages
Python, dependencies, and every command below. Deploying to Azure additionally
requires `az`, `bicep`, `kubectl`, `kubelogin`, `flux`, and `helm`, which
`uv run spi check` verifies.

## Repository map

| Path | What lives here |
|------|-----------------|
| `src/spi/` | The `spi` CLI (Typer + Rich + Pydantic) |
| `infra/` | Bicep templates for Azure PaaS provisioning |
| `software/` | Helm charts, middleware components, OSDU service manifests |
| `docs/` | Architecture, ADRs (`decisions/`), design docs, prose `STYLE.md` |
| `scripts/`, `tests/` | Helper scripts and the pytest suite |

Full layout and design rationale live in `.github/skills/prime/reference.md`.

## Setup

```bash
uv sync   # installs the CLI plus dev tools (pytest, ruff, ty, pre-commit)
```

## Validate changes (run before every PR)

```bash
uv run pre-commit run --all-files   # runs all four checks below in one pass
```

Or run them individually:

| Command | Checks |
|---------|--------|
| `uv run ruff check src tests` | Lint |
| `uv run ruff format --check src tests` | Format |
| `uv run ty check src tests` | Types |
| `uv run pytest -q` | Tests |

Sanity-check the CLI itself with `uv run spi --help` and `uv run spi check`.

## Conventions

- **Commits and PR titles** follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat`, `fix`, `docs`, `refactor`, `test`, `ci`, `style`, `chore`).
  Squash-merge uses the PR title as the release-note subject, so the title must conform.
- **Branches** are named `<type>/<short-name>`, for example `feat/add-redis-component`.
- **Code**: ruff line length 100 with import sorting; keep `ty` clean.
- **Prose**: no em dashes; use commas, periods, or semicolons. Files under `docs/`
  additionally follow `docs/STYLE.md`.
- **Transparency**: every `az` and `kubectl` command the CLI runs is shown to the
  user via a Rich panel before execution.

## Scope notes for agents

- **Azure only.** When reading cloned OSDU service repos, only `*-azure/` providers
  (and shared `*-core/`) matter; skip `*-aws/`, `*-gc/`, `*-ibm/`, `*-core-plus/`.
- **No stored credentials.** Workload Identity is the strategic path for Azure PaaS
  access; a key/SAS compatibility path remains for community images, with keys
  kept in Key Vault (ADR-021). Never commit secrets or hardcode credentials.
- Architecture decisions are recorded as ADRs in `docs/decisions/`; check there
  before changing the deployment model.

## Deeper context

- `.github/skills/prime/reference.md` full repo map, CLI reference, image handling
- `CONTRIBUTING.md` dev setup and release process
- `docs/architecture.md` and `docs/design/` how the system fits together
- `.github/skills/prime/SKILL.md` fast repository re-orientation
