# ADR-029: Environment Lifecycle Verbs and the Reset Boundary

## Context

A standing shared environment accretes state the platform never sheds:
acceptance runs leave Elasticsearch indices against the single-node shard
budget (ADR-003), test records in Cosmos, and entitlements groups. That state
spans two layers with different costs: in-cluster middleware that Flux can
rebuild in minutes, and Azure PaaS data that only deleting the resource group
removes. `spi up` provisions both layers on one idempotent path in 50 to 75
minutes cold, and `spi down` removes the whole RG; the lifecycle needs verbs
whose semantics, costs, and triggers are fixed rather than improvised per
incident.

## Decision

The environment has five verbs, composed from existing commands, with an
explicit boundary between them: refresh changes runtime state only; upgrade
re-runs the provision path and may change substrate and workloads in place
(the ARM deployments are incremental); reset deletes and recreates the
substrate, and only that full resource-group rebuild clears both cluster and
PaaS state.

| Verb | Mechanism | Cost | Trigger |
|---|---|---|---|
| status | `spi status --json` (ADR-030) | seconds | on demand |
| refresh | `spi reconcile`, then `scripts/wait_for_flux_ready.sh`, then probes | 5 to 20 min healthy | weekday cron |
| upgrade | `spi up --env shared --tag vNEW --refresh-images` re-run | 20 to 60 min; hours when refreshed images rerun schema-load | `stackVersion` bump merge (ADR-028) |
| reset | `spi down`, poll until the RG is gone, `spi up --tag <pin>` | 3 to 6 h | Saturday cron |
| teardown | `spi down` | 15 to 45 min | protected manual dispatch |

- **Upgrade is the provision path re-run.** Each phase of `deploy_azure()` is
  idempotent (RG create-when-absent, ARM incremental deployments, seed-secret
  reuse, re-suspend per ADR-014), so a re-run with a new tag is the in-place
  upgrade and there is no second code path to trust separately.
- **No partial reset exists.** The weekly teardown-and-rebuild at the pinned
  tag sheds accreted state in both layers and keeps the rebuild path
  continuously proven at the `core` profile, which the nightly smoke (default
  `bare`) does not exercise. Saturday puts the outage where the merge gate is
  idle.
- **Refresh is composition, not a command.** The reconcile trigger, the
  readiness wait, and the probes each exist; the ops workflow
  (`docs/design/environment-lifecycle.md`) sequences them. Refresh also runs
  the pin backstop (ADR-031).
- **Lifecycle workflows own the `maintenance` flag** that ADR-030 surfaces.
  A workflow sets it before it changes the environment and clears it only
  after the readiness wait and probes pass; a failed run leaves it set for an
  operator. A freshly provisioned shared environment starts with it set, and
  a missing deploy record reads as not deployable, so fork deploys fail
  closed instead of racing a half-built environment.
- **Setting the flag stops new deploys; a drain covers the in-flight ones.**
  GitHub concurrency groups are repository-scoped, so nothing in Actions can
  serialize this repo's lifecycle runs against eight forks' deploy jobs.
  The pins are the cross-repository signal: after setting the flag, the
  workflow waits until every ephemeral pin's owning run (recorded in the pin,
  ADR-031) has finished, bounded by a drain window sized to the fork
  deploy-plus-test budget, and only then mutates the environment.
- **Test identities belong to the lifecycle.** Acceptance-tester service
  principals are Entra objects and outlive the RG. The Key Vault returns
  through the soft-delete recovery in `spi up` because the declaration file
  persists the environment's name suffix across the RG deletion (ADR-028);
  without that, a rebuild would derive a new vault name and the old secrets
  would be unreachable. An idempotent ensure step after each reset verifies
  and repairs the required secrets and role assignments rather than assuming
  loss.
- Lifecycle operations serialize under one concurrency group; fork deploys do
  not (ADR-031).

Rejected: a workload-only mid-tier reset (delete and re-reconcile the Flux
tree, keep PaaS). Faster than a full reset, but it sheds only the in-cluster
half of the accreted state while Cosmos and Storage keep theirs, and it adds a
third lifecycle path to maintain.

Rejected: blue/green environment swap. No merge-gate outage, at the cost of a
cutover primitive the stack does not have and a second standing environment's
Azure spend.

Rejected: a `spi refresh` CLI verb. One command for a human operator, but it
would re-implement `scripts/wait_for_flux_ready.sh`'s watch loop in Python;
`spi reconcile && spi status --watch` covers the interactive case.

Rejected: clear the `maintenance` flag on a workflow's failure path. Restores
availability without an operator, but reopens deploys against an environment
whose upgrade or rebuild did not finish; unavailability with a reason is the
safer failure.

## Consequences

- The Saturday reset is a full outage window of up to 6 hours (deletion,
  cold provision, and a schema-load converge that alone budgets 230
  minutes); a fork pipeline that runs into it fails at deploy and is re-run,
  not queued.
- A red upgrade, reset, or refresh leaves the environment refusing deploys
  until an operator intervenes; availability is traded against testing on a
  half-changed platform.
- State accretion is bounded to one week, and the rebuild is exercised weekly
  rather than attempted for the first time during an incident.
- An upgrade restarts services in place, and its incremental ARM deployments
  can change substrate resources; fork test jobs observe rolling restarts
  during the window, absorbed by their dependency health gate (ADR-031).
- The reset must wait for actual RG deletion before re-provisioning
  (`cleanup_azure` acknowledges the delete within 60 s but does not wait for
  completion).
