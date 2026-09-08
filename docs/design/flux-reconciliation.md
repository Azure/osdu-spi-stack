# Flux reconciliation

Flux applies workload configuration from Git. OSDU service image tags are a
separate input, supplied by the CLI through `osdu-flux/osdu-image-lock`.
Changing either input can change the running environment.

**Suspending the Git source stops fetching new commits, not reconciliation
against the cached commit.** Kustomizations, HelmReleases, and operators can
continue working while the source is suspended.

## Git configuration and runtime inputs

`infra/flux.bicep` installs the AKS Flux extension and configures the
`osdu-spi-stack-system` Git source in `osdu-flux`. The extension's controllers
remain in protected `flux-system`; SPI-owned objects do not. When resumed, the
source polls every ten minutes.
Two root Kustomizations consume that source:

| Root | Path | Responsibility |
|---|---|---|
| `stack` | `software/stacks/osdu/profiles/<profile>` | Operators, middleware, OSDU services, initialization |
| `ingress` | `software/stacks/osdu/ingress/<mode>` for `core` | Certificates, DNS controller where needed, TLS overlays, routes |

`minimal` selects `<mode>-minimal` without OSDU API routes. `bare` selects empty
stack and ingress trees. These path combinations are derived in
`infra/flux.bicep`, not selected independently by the operator.

OSDU HelmReleases use the local `software/charts/osdu-spi-service` chart from
the Git source. Middleware HelmReleases can use external chart repositories.
Suspending Git therefore does not freeze every upstream chart or controller.

The CLI owns several inputs outside Git: `osdu-config` in `osdu`, and
`spi-cluster-config`, `spi-ingress-config`, `spi-init-values`, and
`osdu-image-lock` in `osdu-flux`. `spi-cluster-config` carries the detected
Istio revision used by namespace substitution.
The checked-in `osdu-config-placeholder.yaml` is comments only, not a second
ConfigMap. Editing it does not change live configuration.

## Dependency ordering

The core profile uses `dependsOn` to wait for named Kustomizations to report
Ready. With `wait: true`, a Kustomization also waits for health checks on its
applied resources. Layer labels help group the dashboard; they do not impose
additional ordering.

| Kustomization | Direct dependencies |
|---|---|
| `spi-namespaces` | None |
| `spi-nodepools`, `spi-fork-rbac` | Namespaces |
| `spi-cert-manager`, `spi-eck-operator`, `spi-cnpg-operator`, `spi-helm-sources` | Namespaces |
| `spi-gateway` (empty inventory handoff) | Namespaces |
| `spi-trust-manager` | cert-manager |
| `spi-elasticsearch` | ECK, NodePools |
| `spi-redis` | cert-manager, NodePools, shared Helm sources |
| `spi-postgresql` | CNPG, NodePools |
| `spi-airflow` | PostgreSQL |
| `spi-osdu-config` | Namespaces |
| `spi-bootstrap` | trust-manager, Elasticsearch, Redis, OSDU config |
| `spi-osdu-services` | Bootstrap, NodePools |
| `spi-osdu-init` | Core services |
| `spi-osdu-schema-load`, `spi-osdu-legal` | Initialization |
| `spi-osdu-reference` | Core services, schema load |

Names are shortened in the dependency column; the full definitions are in
[`stack.yaml`](../../software/stacks/osdu/profiles/core/stack.yaml). Ingress
adds dependencies defined in its selected profile. For example, TLS routes
depend on the gateway certificate layer as well as service readiness.

Legal-tag seeding is a non-gating branch: reference services wait for schema
load, without depending on `spi-osdu-legal`. Fork RBAC is also independent of
the middleware and service readiness chain.

A downstream error may name only its direct dependency. Follow that dependency
upstream rather than assuming the last blocked service is the root cause.

## Inventory ownership

Each Kubernetes object has one Flux inventory owner. The selected ingress
tree owns the Gateway under `spi-gateway-tls` in both HTTP and TLS modes.
The old `spi-gateway` Kustomization renders an empty handoff directory with
`prune: false` and `deletionPolicy: Orphan`; its continued presence does not
mean two owners still render the Gateway.

The `bitnami` HelmRepository has its own `spi-helm-sources` owner, shared by
Redis and ExternalDNS. Moving or deleting an inventory without a handoff can
delete objects already applied by another owner. Follow
[ADR-025](../decisions/025-single-flux-inventory-owner.md), rather than renaming
or removing those handoff Kustomizations in one rollout.

## Diagnosing a blocked rollout

These commands read the current cluster:

```bash
spi status
flux get kustomizations -n osdu-flux
flux get helmreleases -n osdu-flux
kubectl describe kustomization spi-osdu-services -n osdu-flux
```

If the service Kustomization reports that `spi-bootstrap` is not ready, inspect
that Kustomization next. If bootstrap is waiting for Elasticsearch, inspect
`spi-elasticsearch` and the Elasticsearch resources in `platform`. A
`HealthCheckFailed` condition differs from a missing source artifact, an invalid
manifest, or an image-pull failure; use the named resource's conditions and logs
to distinguish them.

For initialization failures:

```bash
kubectl get jobs -n osdu
kubectl logs job/schema-load -n osdu
```

Timeouts are defined per Kustomization. Increasing a timeout can accommodate a
slow rollout, but will not fix a missing Secret, bad image reference, or invalid
manifest.

## Stalled HelmReleases

Symptom: `flux get helmreleases -n osdu-flux` shows `Ready=False` with
`Stalled=True` and reason `RetriesExceeded`, and `spi status` renders the
release as `Stalled`; its dependents sit at `DependencyNotReady`.

A `HelmRelease` that exhausts `install.remediation.retries` is marked
`Stalled=True` with reason `RetriesExceeded`, and helm-controller stops
retrying it. Neither the `interval` nor a re-apply of the unchanged manifest
through its Kustomization clears that state, because the release's generation
has not moved. The release holds at `Ready=False` after whatever blocked it is
gone, and every Kustomization that depends on it holds at
`DependencyNotReady`.

The workload underneath can be healthy the whole time. A Deployment whose pods
stay unschedulable past `progressDeadlineSeconds` reports `Failed` to Flux's
health check, which fails the install early; once capacity arrives the
Deployment recovers on its own, but the retries are already spent. The
structural failure is that a transient capacity shortage outlasting four
install attempts converts into a permanent stall.

`RetriesExceeded` is not the only stall. helm-controller also sets
`Stalled=True` for terminal causes, an invalid CEL health-check expression or
a chart reference it may not read, where the release needs a change and
another forced attempt repeats the same failure. Only a `RetriesExceeded`
stall is recoverable by a reset, so the two are counted and captioned
separately.

`spi status` renders any stalled release as `Stalled` rather than
`InstallFailed`, which distinguishes an exhausted controller from a service
that is failing to start, and captions the table per cause:
`RetriesExceeded` points at `spi reconcile`, a terminal stall says a reset
repeats the same failure. `spi reconcile` annotates each `RetriesExceeded`
release with `reconcile.fluxcd.io/requestedAt`, `resetAt`, and `forceAt` at
one timestamp, clearing the failure count and forcing a single attempt;
helm-controller ignores `resetAt` and `forceAt` unless they match
`requestedAt`. Every other release is left alone, and a `HelmRelease` read
that fails for any reason other than the type being absent aborts the command
rather than reporting a reset that never happened.

## Immutable Job templates

Symptom: `osdu-spi-init` or `osdu-spi-legal` holds at `RollbackFailed`, the
Helm error names a Job `spec.template` field as `field is immutable`, and `spi
reconcile` leaves it there.

A Job's pod template is immutable, so anything that changes it after the Job
exists is a difference Helm can only close by patching, and the patch is
rejected. `safeguards-workload-mutating-webhook` raises a Job's CPU request to
100m, which is why `osdu-spi-init` requests exactly that. A lower request holds
`osdu-spi-init` and `osdu-spi-legal`, which render the same chart, at
`RollbackFailed` with their automatic rollbacks failing the same way, from the
first upgrade after the Jobs land. Nothing resets it; `spi reconcile` only
touches `RetriesExceeded`. Recovery is a chart that requests what admission
left on the live Job. Deleting the Jobs is not a recovery on its own: the retry
recreates them from the same manifest, admission raises them again, and the
next upgrade is rejected again.

A chart edit that changes a rendered Job wedges the release the same way, and
`force` is no escape. A Kustomization's `force` deletes and recreates on an
immutable-field error, which protects schema-load; a HelmRelease's
`spec.upgrade.force` maps to Helm's Replace, and the API server rejects that
for the same reason it rejects the patch.

The airflow Jobs are release-managed too, since `useHelmHooks: false` keeps
Flux from skipping them, but the chart's default `ttlSecondsAfterFinished: 300`
deletes them once they finish, so a later upgrade creates them rather than
patching them. `osdu-spi-init` cannot borrow that: `spi info` and `spi status`
read its Jobs as the evidence that bootstrap ran
([ADR-015](../decisions/015-partition-entitlements-bootstrap.md)).

helm-controller stores release history in `osdu-flux`, even when workloads
run in `platform` or `osdu`. Use `helm get`, `helm history`, and `helm list`
with `-n osdu-flux`; the target namespace reports a missing release.

## Refreshing service images

The first core deployment resolves an image lock; a retry preserves it unless
`--refresh-images` is explicit. `--no-refresh-images` fails when no lock exists.
Canonical resolution uses the community GitLab registry. Per-service fork
canonical-source promotion remains unbuilt; see
[environment lifecycle](environment-lifecycle.md).

The image lock covers 14 images, including the schema loader. Repository, tag, and digest values
are substituted into HelmRelease manifests during a service Kustomization
reconcile. For example:

```yaml
# Excerpt from the spi-osdu-services Kustomization.
postBuild:
  substituteFrom:
    - kind: ConfigMap
      name: osdu-image-lock
```

```yaml
# Excerpt from the partition HelmRelease.
image:
  repository: ${PARTITION_IMAGE_REPOSITORY}
  tag: "${PARTITION_IMAGE_TAG}"
  digest: "${PARTITION_IMAGE_DIGEST}"
```

The ConfigMap and consuming Kustomization are both in `osdu-flux`; there is
no `namespace` field in this `substituteFrom` entry. The service chart renders
`repository@digest` when a digest exists and falls back to `repository:tag` for
older entries. Schema-load consumes the composed `SCHEMA_LOAD_IMAGE_REF`.

**This command changes deployed service images and can cause rolling updates:**

```bash
spi reconcile --refresh-images
spi status --watch
```

It resolves canonical registry tags, preserves active service pins, and applies
the image lock. It then reconciles and waits in order for the present core
Kustomizations: services, schema load, and reference services. Layers absent
from `bare` or `minimal` are skipped. The command does not rotate credentials
or synchronize Key Vault values.

Each CLI wait uses a 40-minute timeout, shorter than the schema loader's own
deadline. A CLI wait timeout does not itself stop the Job or Flux reconciliation;
inspect their conditions before treating it as a failed load.

Canonical schema and schema-load images resolve to the same SHA. If the registry has no matching
loader image, resolution fails before replacing the lock. `spi-osdu-schema-load`
substitutes the loader image and uses `force: true` so a changed Job template
can be recreated. `spi reconcile` attempts to backfill older locks missing
loader keys before requesting reconciliation. A resolution failure in that
backfill exits with an error before any reconciliation is requested; the lock
is left as it was.

The lock records the resolved set for one environment. It does not guarantee
future registry retention or make a fresh resolution choose the same images.

## Service image pins

`spi service pin --mr` selects an OSDU merge-request pipeline image;
`--image` accepts an explicit GHCR digest. Pin provenance and the canonical image are recorded in the
`spi-stack.osdu.dev/pins` JSON annotation on `osdu-image-lock`. `spi up` and
image refresh preserve those overrides; unreadable pin state aborts the
overwrite rather than silently removing a pin.

Inspect pins without changing images:

```bash
spi service list
```

The following commands change images and trigger reconciliation. Replace the
placeholder with a merge-request IID from the schema service's OSDU GitLab
project, not a GitHub PR number:

```bash
spi service pin schema --mr <mr-iid>
spi service reset schema
```

Schema pins include the matching loader image when the MR pipeline built it.
Otherwise the CLI warns and retains or restores the canonical loader; it does
not silently keep a loader pinned by an older MR. Reset restores the recorded
canonical image; if that record is missing, the CLI reports that a subsequent
`spi reconcile --refresh-images` is required.

For fork CI, `--ephemeral` records the owning run and source provenance.
`spi service verify` checks the rollout and running image digest;
`spi service reset --if-run` restores only a pin still owned by that run.
The stale-pin sweep and `spi onboard` trust path exist; the scheduled backstop
and canonical-source promotion remain unbuilt. Ephemeral pins require a
repository matching the lock's trusted roster and its derived GHCR package.
Follow [fork deployment](fork-deployment.md) for required metadata, refusal
codes, and trust activation.

## Fetching a new Git revision

`spi up` resumes the source, reconciles it, verifies the requested branch or
tag artifact, then suspends it and writes the deploy record. An unavailable or
wrong-ref artifact fails deployment. The standalone reconcile command has a
different boundary: `spi reconcile` annotates the source and selected Kustomizations
with `reconcile.fluxcd.io/requestedAt`, but does not temporarily resume the
source. Do not rely on that command to fetch a new commit while suspended.

| Command | Current behavior |
|---|---|
| `spi reconcile` | Refresh cluster configuration, attempt loader-key backfill, reset `RetriesExceeded` HelmReleases, and request reconciliation; leave source suspension unchanged |
| `spi reconcile --resume` | Refresh cluster configuration, attempt loader-key backfill, then set the source's `spec.suspend` to `false` |
| `spi reconcile --suspend` | Set the Git source's `spec.suspend` to `true` |
| `spi reconcile --refresh-images` | Refresh cluster configuration and the image lock, preserve pins, reset `RetriesExceeded` HelmReleases, reconcile dependent layers in order; leave suspension unchanged |

`--suspend` and `--resume` exclude each other, and neither combines with
`--refresh-images`; the CLI rejects those combinations before touching the
cluster.

To intentionally fetch the tracked branch, use this sequence. **Resuming allows
new commits to begin applying; it is not an atomic update to one chosen SHA.**

```bash
spi reconcile --resume
flux reconcile source git osdu-spi-stack-system -n osdu-flux
kubectl get gitrepository osdu-spi-stack-system -n osdu-flux \
  -o jsonpath='{.status.artifact.revision}{"\n"}'
```

Confirm the reported revision is the one you expect, then stop further polling:

```bash
spi reconcile --suspend
spi reconcile
spi status --watch
```

If the fetch fails, suspend again before diagnosing it unless you intend to
leave automatic updates enabled. The cached artifact can continue reconciling
after suspension. To follow the branch continuously, leave the source resumed.

### Release tags and maintenance

`spi up --tag <release>` selects an immutable release tag instead of a branch.
Resuming that source fetches its configured tag; it does not switch to `main`.
A new tag deployment records `maintenance: true`, while an existing deploy
record retains its maintenance value. Source suspension and maintenance are
independent: suspension stops Git fetching, and maintenance blocks the
`spi status --json` deployable verdict even when workloads are Ready.

The shared lifecycle workflows explicitly set maintenance before mutation and
clear it after Flux readiness and gateway probes pass. A hand-run `spi up
--tag` against an existing record does not establish that maintenance gate for
the operator. Use the [environment lifecycle](environment-lifecycle.md) contract
when updating the shared environment.

## Decisions and implementation

- [ADR-007](../decisions/007-layered-kustomization-ordering.md): dependency ordering.
- [ADR-014](../decisions/014-suspend-gitops-after-deploy.md): intended default suspension and update behavior.
- [ADR-017](../decisions/017-osdu-image-lock.md): service image lock.
- [ADR-019](../decisions/019-osdu-flux-gitops-namespace.md): SPI-owned objects outside the controller namespace.
- [Flux activation](../../infra/flux.bicep): source and root Kustomizations.
- [Core stack](../../software/stacks/osdu/profiles/core/stack.yaml) and [ingress profiles](../../software/stacks/osdu/ingress/): dependencies.
- [CLI](../../src/spi/cli.py) and [deployment](../../src/spi/deploy.py): reconcile commands and initial suspension.
- [Images](../../src/spi/images.py): image-lock membership, resolution, and keys.
- [Pins](../../src/spi/pins.py): MR and fork image selection, verification, provenance, and reset.
- [Deploy record](../../src/spi/deploy_record.py): revision and maintenance state.
- [Init chart](../../software/charts/osdu-spi-init/): Job requests and Helm upgrade behavior.
