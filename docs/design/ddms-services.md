# DDMS service layer

Status: implemented for the Seismic API and its opt-in RestoreRunner.

DDMS workloads are OSDU product services with a lifecycle after core
initialization. [ADR-041](../decisions/041-ddms-workloads-reconcile-in-their-own-layer.md)
places them in separate Flux inventories without adding a namespace or changing
SPI's Azure data plane, shared service chart, Workload Identity, or image lock.

## Repository ownership

| Concern | Owner |
|---|---|
| DDMS APIs and auxiliary workloads | `software/stacks/osdu/services-ddms/` |
| DDMS routes | `software/stacks/osdu/routes/<shape>/ddms/` |
| Profile ordering | `software/stacks/osdu/profiles/core/stack.yaml` |
| Ingress ordering | `software/stacks/osdu/ingress/<mode>/stack.yaml` |
| Immutable API and companion images | `src/spi/images.py`, `src/spi/pins.py`, `osdu-image-lock` |
| Azure resources and role assignments | `infra/` Bicep modules |
| Tenant and sample-data state | Named load registry and `src/spi/loads.py` |

The `minimal` and `bare` profiles stop before this layer. The `core` profile
declares it because a service present in the image registry and manifests is
part of that environment, not an optional overlay.

## Reconciliation graph

| Kustomization | Direct dependencies | Result |
|---|---|---|
| `spi-osdu-ddms` | `spi-osdu-init` | Partition and entitlements initialization precede DDMS workloads |
| `spi-osdu-ddms-routes` | `spi-osdu-ddms`, selected gateway Kustomization | Routes appear only after their DDMS backends and gateway are Ready |

`spi-osdu-ddms` uses `wait: true`, consumes `osdu-image-lock` and
`spi-cluster-config` through `postBuild.substituteFrom`, and carries a timeout
sized for service rollouts. Its HelmReleases use the local
`osdu-spi-service` chart. The API, a worker, and another DDMS are separate
releases even when the same repository builds them.

The layer is gating. `spi status` therefore reports the environment as not
deployable when a declared DDMS is unhealthy, while the independent
`spi-osdu-services` condition still shows whether core services are healthy.
The separation narrows the failed inventory and the resources a pin causes
Flux to reconcile.

## Workload and identity boundaries

DDMS pods run in `osdu` with Istio injection. An ordinary API uses
`workload-identity-sa` and the shared OSDU UAMI under ADR-005. The service
chart supplies its security context, resource limits, probes, topology, and
image substitution as it does for core and reference services.

An auxiliary worker is not an extra container in the API pod. It has its own
HelmRelease and replica control, which permits the image pair to exist while
the process is disabled. A worker that needs permissions outside the shared
OSDU envelope remains at zero replicas until a separate identity and permission
decision is implemented. RestoreRunner follows this model under
[ADR-042](../decisions/042-seismic-restore-is-an-explicit-privilege-boundary.md):
its dedicated ServiceAccount federates to a dedicated UAMI with scoped restore
access. API onboarding cannot broaden the shared UAMI to make that worker
start.

Container pulls still use the kubelet identity. Runtime Azure calls use
Workload Identity. DDMS-specific Cosmos, Storage, Service Bus, and Key Vault resources stay in
Bicep; the workload layer consumes their names and endpoints through the
existing configuration inputs. A DDMS-specific in-cluster backend and an
upstream service-specific chart are outside this model.

## Images move as one service contract

The image registry owns a DDMS primary and any companions. Canonical refresh,
fork pins, reset, and legacy-lock backfill use the companion rules in ADR-031.
The DDMS Kustomization reads the resulting primary and companion keys from
`osdu-image-lock`.

A pin changes only the DDMS inventory that consumes those keys. Core services
continue serving during its rollout. The environment remains guarded by the
same before-and-after lock and rollout checks used by `spi test`; layer
isolation does not add a second pin protocol.

## Deployment does not create domain data

Flux establishes a healthy runtime. It does not create a Seismic tenant,
subproject, dataset, or sample objects. Those writes have their own
idempotency, source, duration, and failure state, so they belong to a named
load under ADR-040.

A DDMS acceptance descriptor declares that load in `requires.loads` when its
tests need the state. A missing or failed load refuses that suite without
making the runtime undeployable. This keeps service health distinct from test
fixtures and domain data.

## Seismic implementation

`services-ddms/seismic.yaml` deploys two API replicas and routes
`/seistore-svc/api/v3`. The API uses the shared OSDU Workload Identity for
Cosmos, Storage Queue, Key Vault, and partition-resolved Storage access. Its
password-protected in-cluster Redis connection remains separate from that
Azure identity and reads bootstrap aliases from Key Vault.

Each partition always contains `sdms-db/data`. The bulk-delete, change-tier,
and analytics flags remain false. Restore defaults to false, with the API flag
off and RestoreRunner at zero replicas. `spi up --enable-seismic-restore`
enables the API flag, one worker replica, the common-storage restore queue,
the two restore Cosmos containers, and the partition storage recovery
policies. The CLI preserves that setting on later `spi up` calls when neither
restore option is supplied.

`spi up --disable-seismic-restore` turns the API flag and recovery policies
off and scales RestoreRunner to zero. Because ARM deployments are incremental,
the restore queue, Cosmos containers, identity, custom role, and assignments
remain dormant. `spi reset` removes them with the environment.

RestoreRunner uses `seismic-restore-sa` rather than
`workload-identity-sa`. Its UAMI can read the environment vault, consume the
common-storage restore queue, update Seismic Cosmos and blob state, and call
the exact blob-service restore action on partition storage accounts. The API
identity receives common queue access only while restore is enabled.

## Source files

- [`software/stacks/osdu/profiles/core/stack.yaml`](../../software/stacks/osdu/profiles/core/stack.yaml)
- [`software/stacks/osdu/ingress/azure/stack.yaml`](../../software/stacks/osdu/ingress/azure/stack.yaml)
- [`software/stacks/osdu/services-ddms/seismic.yaml`](../../software/stacks/osdu/services-ddms/seismic.yaml)
- [`software/stacks/osdu/services-ddms/seismic-restore.yaml`](../../software/stacks/osdu/services-ddms/seismic-restore.yaml)
- [`infra/modules/identity.bicep`](../../infra/modules/identity.bicep)
- [`infra/modules/partition.bicep`](../../infra/modules/partition.bicep)
- [`infra/modules/rbac.bicep`](../../infra/modules/rbac.bicep)
- [`src/spi/images.py`](../../src/spi/images.py)
- [`src/spi/pins.py`](../../src/spi/pins.py)
- [`src/spi/loads.py`](../../src/spi/loads.py)
