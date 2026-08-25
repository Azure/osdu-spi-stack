# ADR-014: Suspend GitOps Reconciliation After Deploy

## Context

SPI Stack is a dev/test target: engineers run `spi up` against short-lived clusters to verify a specific commit or reproduce an issue. Flux (ADR-009) by default polls the tracked branch and auto-reconciles every new commit. For production GitOps that is the central feature; here it means a merge to `main` rolls out to every connected environment within a minute, shared-ConfigMap changes trigger Helm upgrades across 10+ slow-starting services, and "the environment" becomes whatever happens to be latest rather than the commit under test. `spi reconcile --suspend` and `--resume` exist, but nothing in the default path invokes them; the safe default and the actual default are not the same setting.

## Decision

`spi up` ends by suspending the Flux `GitRepository` source (`osdu-spi-stack-system` in `osdu-flux`, ADR-019): wait up to 120s for the source to reach `Ready=True`, then patch `spec.suspend: true`. The wait is non-fatal; on timeout the CLI warns and suspends anyway. The environment stays pinned to the commit current when `spi up` ran, and updates are explicit:

- `spi reconcile`: one-shot pull; fetch latest, reconcile once, stay suspended.
- `spi reconcile --resume`: re-enable continuous reconciliation.
- `spi reconcile --suspend`: re-pin after a `--resume`.

Suspending the source does not block the deploy in progress. `spec.suspend` only stops `source-controller` from fetching new revisions; the cached artifact remains, and downstream Kustomizations and HelmReleases keep reconciling the full layer chain from it. `spi up` exits as soon as the source is pinned while the deployment continues to Ready. `spi status` renders a `SUSPENDED` banner and the completion message states that updates require `spi reconcile`.

Rejected: keep Flux's default continuous reconciliation. The production-correct behavior, but a merge to `main` keeps shifting live dev clusters mid-investigation.

Rejected: an `spi up` flag to skip the suspend step. `spi up` followed by `spi reconcile --resume` covers it without a second configuration surface.

## Consequences

- Environments are stable by default; a push to `main` reaches only clusters whose operator runs `spi reconcile`.
- Updates are explicit and reviewable: the user knows when the environment changed and why.
- Re-running `spi up` on a suspended environment is safe: the re-applied `GitRepository` template does not set `spec.suspend`, so Flux fetches latest, reconciles, and the CLI suspends again at the end.
- Freshness costs a manual step; a continuously reconciling workflow needs `spi reconcile --resume` after every `spi up`.
