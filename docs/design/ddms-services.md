# DDMS service layer

Status: planned. The `core` profile does not yet declare the DDMS workload and
route Kustomizations described here.

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

`spi-osdu-ddms` uses `wait: true`, consumes `osdu-image-lock` through
`postBuild.substituteFrom`, and carries a timeout sized for service rollouts.
Its HelmReleases use the local `osdu-spi-service` chart. The API, a worker, and
another DDMS are separate releases even when the same repository builds them.

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
decision is implemented. API onboarding cannot broaden the shared UAMI to make
that worker start.

Container pulls still use the kubelet identity. Runtime Azure calls use
Workload Identity. DDMS-specific Cosmos, Storage, Service Bus, and Key Vault
resources stay in Bicep; the workload layer consumes their names and endpoints
through the existing configuration inputs. An in-cluster backend and an
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

## Planned implementation surfaces

The first implementation adds the DDMS and route directories, their profile
entries, status coverage, and tests before moving Seismic manifests out of the
core service inventory. The Seismic API can then add its Azure data plane and
shared-identity runtime. Restore execution follows only after its dedicated
identity, role-definition ownership, and PITR feature gate have their own
accepted decision.

## Source files

- [`software/stacks/osdu/profiles/core/stack.yaml`](../../software/stacks/osdu/profiles/core/stack.yaml)
- [`software/stacks/osdu/ingress/azure/stack.yaml`](../../software/stacks/osdu/ingress/azure/stack.yaml)
- [`src/spi/images.py`](../../src/spi/images.py)
- [`src/spi/pins.py`](../../src/spi/pins.py)
- [`src/spi/loads.py`](../../src/spi/loads.py)
