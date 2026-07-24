# CI Smoke Pipeline

**What this explains.** How `.github/workflows/smoke.yml` is structured into three jobs (`provision`, `verify`, `teardown`), why each job runs its own Azure OIDC login, and how the orphan-RG sweeper backstops the design.

**Why it matters.** The smoke pipeline performs a real `spi up` against the Azure subscription, which takes ~45-50 minutes end-to-end. A naive single-job workflow leaves orphan resource groups in Azure when `spi up` fails after the 5-minute GitHub OIDC JWT has expired: every subsequent `az` call hits AADSTS700024 and silently no-ops, including the teardown step. The three-job split guarantees the teardown job starts with a fresh JWT regardless of how the upstream jobs ended.

## The three jobs

| Job | Owns | Timeout | OIDC login |
|-----|------|---------|------------|
| `provision` | `az group create` + `spi up` (AKS + PaaS + Flux extension Bicep) | 60 min | Fresh per job |
| `verify` | `wait_for_flux_ready.sh` + acceptance probe + diagnostics-on-failure | 50 min | Fresh per job |
| `teardown` | `az group delete --name <rg> --yes --no-wait` | 15 min | Fresh per job |

`provision` exposes the resource group name as a job output (`needs.provision.outputs.rg`). `verify` consumes it to call `az aks get-credentials`. `teardown` consumes it to issue the deletion. The teardown step guards on an empty RG so a provision that died before "Resolve env name" no-ops cleanly.

`teardown` runs with `if: ${{ always() && needs.provision.result != 'skipped' }}`, so it fires on provision failure, provision cancellation, verify failure, or verify timeout. It skips only when provision itself was skipped (currently impossible, but defensive against a future precondition job).

## Why the three-job split

The original single-job design had two failure modes:

1. **OIDC JWT expiry.** GitHub mints an OIDC JWT good for ~5 minutes. `azure/login@v3` exchanges it for AAD access tokens that live ~1 hour. When `spi up` takes 30-50 minutes and fails partway through, the original JWT is long dead. Any `az` command that needs to refresh against a dead JWT (e.g., teardown calling `az group delete` with stale tokens) silently fails with AADSTS700024. The `|| true` on the teardown step swallows the error, leaving an orphan resource group.
2. **Failure isolation.** A single 90-minute job step list hides where the failure happened. Splitting into provision/verify/teardown makes the workflow summary directly answer "did infra fail or did K8s fail?"

The fix: each job runs `azure/login@v3` at its own start. The teardown job's JWT is seconds old when it issues the delete, not hours old. The trade-off is repeating the tool-install steps (uv, kubectl, kubelogin, helm, flux CLI) across jobs; these run in ~30 seconds each and were deemed cheap compared to extracting them into a reusable composite action.

## Cancellation backstop

The split-job design catches normal failure paths but not full-workflow cancellation: when the user clicks "Cancel workflow" or GitHub kills the entire run, `if: always()` jobs are killed too.

[`.github/workflows/sweeper.yml`](../../.github/workflows/sweeper.yml) plus [`scripts/sweep_orphan_rgs.sh`](../../scripts/sweep_orphan_rgs.sh) are the backstop for that path. The sweeper runs daily at 04:00 UTC, four hours before the nightly smoke at 08:00 UTC. It deletes any RG named `spi-stack-ci-*` tagged `spi-ci-sweep-eligible=true` whose `spi-created-utc` tag is older than three hours.

The provision job's "Pre-create RG with sweeper tags" step is what makes the backstop work even for runs that die before `spi up` finishes: the tags are written before any other Azure work begins. So a workflow that gets killed in the first 30 seconds still leaves a sweep-eligible RG behind.

## Observed timings (`centralus`)

- `provision`: ~45 min (~30 min AKS Automatic Bicep + ~3 min PaaS Bicep + ~30s K8s bootstrap + ~10-15 min Flux extension Bicep)
- `verify`: highly variable; `scripts/wait_for_flux_ready.sh --timeout 2700` allows up to 45 minutes for Flux to reconcile every Kustomization
- `teardown`: under 15 seconds (fires-and-forgets the RG delete)

The 60-minute provision timeout gives ~15 minutes of headroom over the worst observed run.

## Running a smoke manually

```bash
# Default profile, run id as env suffix
gh workflow run smoke.yml --ref main

# Custom suffix + middleware-only profile
gh workflow run smoke.yml --ref main \
  -f env_suffix=mybranch \
  -f profile=minimal

# Tail the run
gh run watch <run-id>
```

To inspect a failed run:

```bash
# Per-job log tail
gh run view <run-id> --log --job=<job-id>

# Download diagnostics artifact (verify-failure only)
gh run download <run-id> --name smoke-diagnostics-<run-id>
```

To verify the sweeper sees what you expect without deleting anything:

```bash
gh workflow run sweeper.yml -f dry_run=true
```

## Related ADRs

The smoke pipeline is a CI artifact, not an architectural choice in its own right. The deployment shape it exercises is covered in:

- [ADR-003: In-cluster middleware scope](../decisions/003-in-cluster-middleware-scope.md)
- [ADR-008: Bicep for Azure provisioning](../decisions/008-bicep-for-azure-provisioning.md)
- [ADR-009: Flux CD for GitOps](../decisions/009-flux-cd-for-gitops.md)

## Source files

- `.github/workflows/smoke.yml`
- `.github/workflows/sweeper.yml`
- `scripts/sweep_orphan_rgs.sh`
- `scripts/wait_for_flux_ready.sh`
- `scripts/capture_diagnostics.sh`
