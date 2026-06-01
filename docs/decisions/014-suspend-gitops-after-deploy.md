# ADR-014: Suspend GitOps Reconciliation by Default After Deploy

**Status**: Accepted

## Context

SPI Stack is a dev/test deployment target. Engineers run `spi up` against short-lived AKS clusters to verify a specific commit, iterate on service configuration, or reproduce an issue. The deployment uses Flux CD GitOps (ADR-009), which by default polls the tracked Git branch on a fixed interval and auto-reconciles any new commits.

For production GitOps, continuous reconciliation is the central feature. For SPI Stack, the same behavior is a liability:

1. **Surprise upgrades mid-test.** A merge to `main` rolls out to every connected environment within a minute. Users investigating a bug have their cluster state shift under them without warning.
2. **Rolling-update storms.** Changes to shared `ConfigMap` values trigger Helm upgrades across all 10+ services. Spring Boot services that take 30-60s to start compound the disruption.
3. **AI-driven churn.** Agents and skills that modify the repo can land changes faster than humans review their effect. One PR can disrupt every live cluster at once.
4. **Loss of reproducibility.** When debugging, knowing the exact commit an environment is running matters. With auto-reconciliation, "the environment" is whatever happens to be latest.

The CLI already exposes `spi reconcile --suspend` and `spi reconcile --resume` (the `reconcile` command in `src/spi/cli.py`), but these are opt-in and most users do not discover them until something goes wrong. cimpl-stack faced the identical problem and resolved it in its own ADR-018; this decision ports that behavior.

## Decision

Suspend the Flux `GitRepository` source (`osdu-spi-stack-system` in `flux-system`) automatically as the final step of `spi up`. The deployed environment is pinned to the commit that was current when `spi up` ran. Future commits do not auto-reconcile. Users opt into updates explicitly:

- `spi reconcile` performs a one-shot pull: fetches latest, reconciles once, stays suspended.
- `spi reconcile --resume` re-enables continuous auto-reconciliation.
- `spi reconcile --suspend` re-pins after a `--resume`.

### Sequence

`spi up` performs the following steps:

1. Phase 1-3: Azure infrastructure via Bicep.
2. Phase 4: Kubernetes bootstrap (namespaces, secrets, storage classes, Gateway API CRDs, `osdu-config` ConfigMap, workload identity ServiceAccounts, ingress config).
3. Phase 5: Flux extension + GitOps configuration applied via `infra/flux.bicep`. The `GitRepository` starts with `suspend: false`.
4. Phase 6: Key Vault bootstrap secrets written.
5. **Phase 7 (new): `_pin_gitops_source()` in `deploy.py`** — wait up to 120s for `gitrepository/osdu-spi-stack-system` to reach `Ready=True`, then `kubectl patch spec.suspend: true`.

The wait in step 5 is non-fatal: if the source does not become Ready in 120s, the CLI emits a warning and suspends anyway. Flux reconciles from whatever it has cached, and the user can run `spi reconcile` to force a fetch.

### Why suspend does not block the deploy

`GitRepository.spec.suspend: true` only stops the `source-controller` from fetching new revisions. It does not delete the cached artifact, and it does not stop downstream `Kustomization` or `HelmRelease` resources from reconciling. After step 5, Flux keeps processing the dependency chain (L0 namespaces → L1 operators → L2 middleware → L3 Airflow → L4 bootstrap → L5 services + schema-load → L6 reference services) from the cached artifact. A full deployment still takes ~30-45 minutes to complete; `spi up` exits as soon as the source is pinned.

### User feedback

The `spi status` dashboard already renders a yellow `SUSPENDED` banner when the `GitRepository` is suspended, and `spi info` includes the same field. The `spi up` completion message now reminds users that updates require an explicit `spi reconcile`.

## Consequences

- Environments deployed with `spi up` are stable by default. A push to `main` does not affect existing deployments until users explicitly pull updates.
- Users who want the original auto-reconciliation behavior run `spi up` followed by `spi reconcile --resume`. There is no flag on `spi up` to skip the suspend step; the two-command pattern is the supported escape hatch.
- The blast radius of a bad commit drops from "all live SPI Stack clusters instantly" to "clusters whose operator opts in via `spi reconcile`". Appropriate for a dev/test tool where reproducibility matters more than freshness.
- Re-running `spi up` against an already-suspended environment is safe: the Bicep `fluxConfigurations` resource re-applies the `GitRepository` template, which does not set `spec.suspend`. Flux clears the field, fetches the latest commit, reconciles, and the CLI suspends again at the end.
- The `spi reconcile` command becomes the canonical way to absorb upstream changes. Updates become explicit and reviewable: the user knows exactly when their environment changed and why.
- This decision applies only to SPI Stack (dev/test). Production OSDU deployments use the sister `osdu-spi-infra` Terraform repo, which does not go through this CLI and is unaffected.
