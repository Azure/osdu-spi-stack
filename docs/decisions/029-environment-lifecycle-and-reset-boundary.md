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

The environment has five verbs, composed from existing commands. The instance
(Flux-managed workloads, the image lock, in-cluster state) is what refresh and
upgrade move; the substrate (RG, AKS, PaaS, Flux extension) changes only on
reset.

| Verb | Mechanism | Cost | Trigger |
|---|---|---|---|
| status | `spi status --json` (ADR-030) | seconds | on demand |
| refresh | `spi reconcile`, then `scripts/wait_for_flux_ready.sh`, then probes | 5 to 20 min healthy | weekday cron |
| upgrade | `spi up --env shared --tag vNEW --refresh-images` re-run | 15 to 40 min plus rollouts | `stackVersion` bump merge (ADR-028) |
| reset | `spi down`, poll until the RG is gone, `spi up --tag <pin>` | 2.5 to 4 h | Saturday cron |
| teardown | `spi down` | 15 to 45 min | protected manual dispatch |

- **Upgrade is the provision path re-run.** Each phase of `deploy_azure()` is
  idempotent (RG create-when-absent, ARM incremental deployments, seed-secret
  reuse, re-suspend per ADR-014), so a re-run with a new tag is the in-place
  upgrade and there is no second code path to trust separately.
- **Reset is the only reset.** The weekly teardown-and-rebuild at the pinned
  tag sheds accreted state in both layers and keeps the rebuild path
  continuously proven at the `core` profile, which the nightly smoke (default
  `bare`) does not exercise. Saturday puts the outage where the merge gate is
  idle.
- **Refresh is composition, not a command.** The reconcile trigger, the
  readiness wait, and the probes each exist; the ops workflow
  (`docs/design/environment-lifecycle.md`) sequences them. Refresh also runs
  the pin backstop (ADR-031).
- **Test identities belong to the lifecycle.** Acceptance-tester service
  principals survive a reset (Entra objects outlive the RG) but their Key Vault
  secrets do not; an idempotent ensure step re-seeds them after each reset.
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

## Consequences

- The Saturday reset is a full outage window of up to 4 hours; a fork pipeline
  that runs into it fails at deploy and is re-run, not queued.
- State accretion is bounded to one week, and the rebuild is exercised weekly
  rather than attempted for the first time during an incident.
- An upgrade restarts services in place; fork test jobs observe rolling
  restarts during the window, absorbed by their dependency health gate
  (ADR-031).
- The reset must wait for actual RG deletion before re-provisioning
  (`cleanup_azure` acknowledges the delete within 60 s but does not wait for
  completion), and relies on the Key Vault soft-delete recovery already in
  `spi up`.
