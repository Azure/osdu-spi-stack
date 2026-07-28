# ADR-027: Deploy-Lane CI-Mode and Digest-Pin Invariants

**Status**: Accepted

## Context

Bringing legal and storage live end-to-end on a fresh SPI Stack surfaced a
cluster of deploy-lane defects that each let a run *appear* to pass (or fail) for
the wrong reason. They share a theme: the lane sets the cluster image by digest
and then trusts a read that races Flux's own reconciliation, or trusts a fix
that lives in one fork but never reached the others.

## Decision

Adopt the following invariants and fixes across the canonical template
(`osdu-spi`) and every service fork. The template is the source of truth
(the template repository's own image-ownership decision applies to actions too);
a fix proven in one fork is not done until it is in the template.

### 1. CI mode must suspend HelmReleases, not only Kustomizations

OSDU services are Flux **HelmRelease**-managed. The deploy lane sets the image
with `kubectl set image`, an out-of-band change. Suspending the **GitRepository**
source (`spi reconcile --suspend`, ADR-014) does not stop reconciliation from the
cached artifact, and suspending the **Kustomization** does not stop the managing
**HelmRelease**'s own reconcile loop from drift-correcting the deployment back to
the chart's pinned (old) image. Observed concretely: with Kustomizations
suspended but HelmReleases running, a freshly deployed legal/storage pod was
reverted to the dev3-pinned image mid-run, and the integration-test digest guard
then skipped the suite on a false "pod is running X but deploy set Y" mismatch.

**Invariant:** before the deploy lane runs, the managing HelmRelease of the
target deployment (and, operationally, all service HelmReleases in the flux
namespace) must be suspended:
`flux suspend helmrelease --all -n osdu-flux` in addition to the existing
`flux suspend kustomization --all -n osdu-flux`. The `aks-deploy` pre-flight
should assert this alongside the Kustomization check and fail loudly with that
remediation, rather than letting a silent revert corrupt the run.

### 2. Deployed-digest capture must sample a ready, non-terminating pod

`aks-deploy` captured `deployed_digest` from `kubectl get pods … .items[0]`
immediately after `rollout status`. During a rolling update the old
(Terminating) pod can still be `.items[0]`, so the action recorded the
**previous** digest as the deployed one; the integration-test pin guard then
compared the live (new) pod against that stale value and skipped the suite. The
capture (and the symmetric previous-digest capture) now filter to a pod with
`metadata.deletionTimestamp == null`, `status.phase == "Running"`, and the target
container `ready == true` — the variant already proven in the partition fork,
now propagated to the template and every fork.

### 3. Custom deploy-role assignment must be race-proof and fail loud

`spi onboard` creates a per-service least-privilege custom role and assigns it at
the namespace scope. A brand-new custom role definition is not immediately
resolvable by name in `az role assignment create`, and the assignment previously
ran once with `check=False`, so the propagation race failed silently and left the
CI identity without the namespace deploy role — the deploy lane then returned
`Forbidden` on `kubectl get deployments` and the grant had to be made by hand.
Onboard now polls until the role definition is queryable, retries the assignment
with backoff, verifies via re-query, and hard-fails if it never materializes.
The native `spi-ci-flux-reader` Role also grants read-only access to both Flux
Kustomizations and HelmReleases. The deploy preflight checks both resource types;
granting only Kustomization read made the newer HelmRelease assertion fail before
deployment even when the cluster was correctly suspended.

### 4. App Insights agent jar must be readable by the non-root runtime user

The canonical service Dockerfile `ADD`s the App Insights Java agent from a URL,
which defaults to `0600` root-owned; services run `runAsUser 1000`. Without
`--chmod=0755` the JVM is handed `-javaagent:/opt/agents/applicationinsights-agent.jar`
but cannot open it → `Error opening zip file or JAR manifest missing` →
CrashLoopBackOff at VM init. The fix existed only in the partition fork's local
Dockerfile and was never propagated; it now lives in the template
`build/Dockerfile` (and the forks) so every service inherits it.

### 5. Per-attempt integration-test timeout is tunable and generous

The retry action's per-attempt timeout defaulted to 10 minutes; the storage
reactor build plus ~125 live tests exceed that, so attempt 1 always timed out
("Timeout of 600000ms hit"). `timeout_minutes` / `max_attempts` are wired to
repo vars (`IT_TIMEOUT_MINUTES` default 25, `IT_MAX_ATTEMPTS` default 2) in the
template and forks.

### 6. os-core-common 7.1.0 is held back behind a netty floor

Migrating to os-core-common 7.1.0 pulled lettuce 7.x which needs netty 4.2;
deployed images carry netty 4.1, so the entitlements/Redis cache path crashed
with `NoClassDefFoundError io.netty.channel.MultiThreadIoEventLoopGroup`. The
migration is reverted on the service forks (os-core-common 6.0.0 / lettuce 6.8.1
/ netty 4.1.130) until the images carry a netty 4.2 floor.

### 7. Deploy and integration-test should hold one cluster lock (known gap)

`validate.yml`'s concurrency group is declared on the **deploy** job only; the
**integration-test** job is outside it. A superseded run's deploy can therefore
acquire the lock between another run's deploy and its test, swapping the pod
under the running suite. The integration-test job is brought into the same
`spi-stack-<service>` concurrency group so a run holds cluster access from deploy
through test.

## Consequences

- The deploy lane no longer needs manual intervention to land and verify a single
  image digest, *provided* CI mode is established correctly (invariant 1).
- Invariant 1 makes HelmRelease suspension a required operational step, now
  provided by `spi reconcile --suspend`: it resolves the Flux namespace from the
  live `osdu-spi-stack-system` GitRepository and suspends the GitRepository, all
  Kustomizations, and all HelmReleases, so a single command yields the CI-mode
  state the deploy lane asserts (`--resume` reverses it for a baseline refresh).
- Several of these were fixes that existed in exactly one fork (partition) and
  had silently not propagated (agent chmod, digest capture). The standing rule is
  reaffirmed: deploy-lane actions and the service Dockerfile are template-owned;
  land the fix in `osdu-spi` and let template-sync carry it.
