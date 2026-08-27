# Shared Environment Lifecycle

**What this explains.** How the shared backing environment for the
`Azure/osdu-spi-*` fork CI is versioned, refreshed, upgraded, and reset, which
workflows run those verbs, and which surfaces fork pipelines consume.

**Why it matters.** Fork deploy and test jobs depend on a standing environment
being ready when they run. When it is not, the operator needs to know which
verb applies, what it costs, and what it will not fix; improvising that during
an incident is how a 20-minute refresh becomes a 4-hour rebuild.

**Status.** This doc describes the target mechanism ahead of the code. Each
section marks what is unbuilt; the roadmap at the bottom is the build order.
Remove the marks as the phases land.

![The backing environment at a glance](../diagrams/environment-lifecycle.png)

## Three lifetimes

| Layer | Contents | Advances by |
|---|---|---|
| Substrate | RG `spi-stack-shared`, AKS Automatic, PaaS, Flux extension | Reset rebuilds it; an upgrade's incremental ARM pass may also move it in place (ADR-029) |
| Instance | Flux-managed workloads, `osdu-image-lock`, in-cluster middleware state | Refresh, upgrade, and fork deploys (ADR-031) |
| Version contract | `ops/environments/shared.yaml` | Reviewed PR (ADR-028) |

The environment is one deployment of the ordinary stack: `spi up --env shared
--profile core --tag <stackVersion>`. Nothing about its manifests differs from
a dev environment; what differs is that its version is pinned to a release tag
and its lifecycle runs on workflows instead of an operator's terminal.

Version is three axes, not one. The stack definition is pinned by the file
above (ADR-028). Canonical service images advance on refresh under each
service's source policy (ADR-033) and are recorded in the image lock.
Ephemeral test pins (ADR-031) are transient overlays. `spi status --json`
reports all three, the pinned-service list included; `spi service list`
details the pins.

## The pin and the bump flow

`ops/environments/shared.yaml` (unbuilt) declares the environment:

```yaml
env: shared
stackVersion: v0.6.0
profile: core
location: westus3
ingressMode: azure
imageBranch: master
nameSuffix: x7k2q
```

Publishing a release opens a `stackVersion` bump PR (a job in
`.github/workflows/release.yml` under the release App token). Merging the bump
is the reviewed moment when the environment advances; the push trigger on the
pin file starts the upgrade. Nothing else moves the stack-definition version
(ADR-028).

## Lifecycle verbs

| Verb | Workflow | Trigger | Budget |
|---|---|---|---|
| refresh | `env-refresh` | weekday cron 05:00 UTC, dispatch | 90 min |
| upgrade | `env-upgrade` | push to `main` touching the pin file, dispatch | 6 h |
| reset | `env-reset` | Saturday cron 06:00 UTC, confirm-dispatch | 7 h |
| teardown | `env-teardown` | protected dispatch | 1 h |

The budgets contain their worst cases: a reset spends up to 45 minutes on
deletion, 75 minutes provisioning, and 230 minutes in the cold-cluster
schema-load converge before probes; an upgrade whose `--refresh-images` pass
moves the schema image spends up to 60 minutes in `spi up` plus the same
230-minute converge, hence its 6-hour budget.

All four (unbuilt) share concurrency group `env-shared` with
`cancel-in-progress: false`, so lifecycle operations serialize against each
other; fork deploys serialize per service and are otherwise concurrent
(ADR-031). GitHub concurrency groups are repository-scoped, so nothing here
can serialize against fork jobs in other repositories: coordination with the
fleet is the `maintenance` flag (which stops new deploys) plus the drain
(which waits out in-flight ones; ADR-029). The workflows
run under a GitHub environment `azure-shared` on the same app registration as
the smoke pipeline, reusing its job shape: one fresh OIDC login per job, token
pre-caching, `scripts/capture_diagnostics.sh` on failure
([ci-smoke.md](ci-smoke.md)). The sweeper cannot touch the shared RG: it
selects on the `spi-stack-ci-*` name pattern and the sweep-eligibility tag,
and the shared RG carries neither.

**Refresh** proves the environment is serving and sweeps what fork CI left
behind: set the `maintenance` flag and drain in-flight deploys (wait,
bounded, until each ephemeral pin's owning run has finished; ADR-029), run
the pin backstop (sweep ephemeral pins whose owning run has ended, then
`spi service refresh` per GitHub-origin service;
[fork-deployment.md](fork-deployment.md)), trigger `spi reconcile`, gate on
`scripts/wait_for_flux_ready.sh` plus the gateway probes lifted from
`smoke.yml`, and clear the flag only after they pass. A failed step leaves
the flag set (ADR-029), so a red 05:00 UTC run blocks the day's fork deploys
with a reason instead of letting them race a sick environment.

**Upgrade** is `spi up --env shared --tag <new> --refresh-images` re-run on
the standing environment, preceded by the `maintenance` flag, the drain, and
a lock snapshot (`kubectl get cm osdu-image-lock -n osdu-flux -o yaml`
uploaded as a workflow artifact), and followed by the same wait, probes, and
flag clear. The workflow reads the declaration from `main`, then installs
and runs the `stackVersion` release wheel, which carries its own Bicep, so
the executing code and the Flux ref name the same release and the deploy
record carries the stamped CLI version (ADR-028). The source rollover is
revision-verified: resume the suspended source, reconcile, check that
`GitRepository.status.artifact.revision` names the tag's commit, converge,
write the deploy record, suspend again (ADR-029). The `--refresh-images`
pass moves canonical images at the same time, but the bump pins only the
stack-definition axis; canonicals also advance between upgrades on the
weekday refresh (ADR-033).

**Reset** is teardown plus cold provision at the pinned tag: flag, drain,
snapshot the lock, `spi down`, poll until `az group exists` reports false
(the CLI's own wait covers acknowledgement only), then `spi up --tag <pin>
--name-suffix <declared>` and the cold-cluster wait with the 155-minute
schema-load budget. The declared name suffix is what makes recovery work:
the RG tag that normally carries it dies with the RG, and feeding the suffix
back keeps resource names and the hostname stable and lets the Key Vault
soft-delete recovery in `spi up` find the old vault, so its secrets return
with it (ADR-028). The test-identity ensure step then verifies and repairs
the acceptance-tester secrets and role assignments rather than assuming loss
(ADR-029). The rebuilt environment starts with `maintenance` set and opens
to deploys only after the probes pass.

## Surfaces fork CI consumes

- `spi status --json` (unbuilt): `ready` for convergence, `deployable` as the
  deploy gate, a typed reason, the deployed version, and the `maintenance`
  flag. Exit 0/2/1 (ADR-030).
- `spi info --json`: endpoints, partitions, Azure coordinates, secret
  references. In `azure` ingress mode the FQDN embeds the environment's name
  suffix; the declaration file persists the suffix across resets (ADR-028),
  so the hostname is stable, and consumers still re-read it per run rather
  than caching a value.
- `spi connect`, `spi service pin/verify/reset/refresh` (unbuilt): the deploy
  seam. The sequence and its recovery paths are
  [fork-deployment.md](fork-deployment.md); the fork-side jobs live in the
  `Azure/osdu-spi` template's workflows, not here.

## Recipes

Stand up the shared environment (after the versioning phase lands). Install
the release wheel matching `stackVersion` first: the wheel carries the Bicep
and stamps the CLI version the deploy record audits, where a source checkout
would record `0.0.0+source`. Each argument comes from the declaration file;
nothing is typed twice:

```bash
decl=ops/environments/shared.yaml
spi up \
  --env "$(yq .env $decl)" \
  --profile "$(yq .profile $decl)" \
  --location "$(yq .location $decl)" \
  --ingress-mode "$(yq .ingressMode $decl)" \
  --image-branch "$(yq .imageBranch $decl)" \
  --name-suffix "$(yq .nameSuffix $decl)" \
  --tag "$(yq .stackVersion $decl)"
bash scripts/wait_for_flux_ready.sh --timeout 13800
spi status --json | jq .ready   # true when converged
```

This recipe is provision-only: the fresh environment holds `maintenance`
(ADR-029) until the `env-refresh` workflow, or its manual dispatch, runs the
probes and clears it.

Check why the environment is not ready:

```bash
uv run spi status --json | jq -r .reason
uv run spi status          # the human view of the same collector
```

Run a manual refresh outside the cron:

```bash
gh workflow run env-refresh.yml --ref main
gh run watch
```

## Implementation roadmap

1. **Foundations** (unbuilt): `spi status --json`; chart digest rendering and
   the `render_lock_with_pins` digest fix; the generalized pin surface
   (`--image`, `verify`, `refresh`, ownership-checked `reset`, `connect`);
   the two fork RBAC Roles. Exit test: hand-pin a partition GHCR digest
   against a standing environment and reset it.
2. **Versioning** (unbuilt): `repoTag` in `infra/flux.bicep`, `spi up --tag`,
   the deploy record, the tri-state `--refresh-images`, the pin file; stand up
   `shared` at the release tag then in place.
3. **Ops workflows** (unbuilt): `env-refresh`, `env-upgrade`, `env-reset`,
   `env-teardown`, the bump-PR job, the test-identity ensure step.
4. **Onboarding** (unbuilt): `spi onboard`; onboard `osdu-spi-partition`; the
   template-side deploy, integration-test, and restore jobs under the reserved
   check names.
5. **Canonical flips** (unbuilt): `github_repo` on each onboarded service's
   registry entry, one PR per service (ADR-033).

## Related ADRs

- [ADR-014: Suspend GitOps reconciliation after deploy](../decisions/014-suspend-gitops-after-deploy.md)
- [ADR-017: Per-deploy image lock](../decisions/017-osdu-image-lock.md)
- [ADR-028: Version-pinned shared backing environment](../decisions/028-version-pinned-shared-environment.md)
- [ADR-029: Environment lifecycle verbs and the reset boundary](../decisions/029-environment-lifecycle-and-reset-boundary.md)
- [ADR-030: Machine-readable status and the deploy record](../decisions/030-machine-readable-status-contract.md)
- [ADR-031: Fork-built images deploy as ephemeral lock pins](../decisions/031-fork-image-deploys-as-ephemeral-pins.md)
- [ADR-032: Per-fork deploy identity and namespace RBAC](../decisions/032-per-fork-deploy-identity.md)
- [ADR-033: Canonical image source follows onboarding](../decisions/033-canonical-image-source-follows-onboarding.md)

## Source files

- `ops/environments/shared.yaml` (planned)
- `infra/flux.bicep`
- `src/spi/cli.py`, `src/spi/deploy.py`, `src/spi/status.py`,
  `src/spi/pins.py`, `src/spi/images.py`
- `.github/workflows/release.yml`, `.github/workflows/smoke.yml`
- `scripts/wait_for_flux_ready.sh`, `scripts/capture_diagnostics.sh`
