# ADR-041: DDMS Workloads Reconcile in Their Own Layer

## Context

Domain data management services depend on initialized OSDU core APIs, but they
do not belong to the core services' rollout or inventory. A DDMS can also ship
an API beside an auxiliary worker, require Azure PaaS resources that core
services do not use, and need tenant state that is data loading rather than
infrastructure bootstrap. Putting those workloads in `spi-osdu-services` makes
a DDMS failure read as a core-services failure and makes one DDMS image change
reconcile the core inventory.

The namespace boundary remains unchanged. DDMS APIs are OSDU product workloads,
so ADR-006 places them in `osdu` with Istio injection. Their images still follow
the lock and companion rules in ADR-017 and ADR-031, and their Azure access
still uses Workload Identity under ADR-005.

## Decision

The `core` profile adds a gating Layer 7 Kustomization named
`spi-osdu-ddms`. It owns `software/stacks/osdu/services-ddms`, depends on
`spi-osdu-init`, consumes `osdu-image-lock`, and waits for its HelmReleases.
The core, reference, and DDMS inventories remain separate. The `minimal` and
`bare` profiles do not declare the DDMS layer.

- **API and worker are separate workloads.** A DDMS API and each auxiliary
  process use separate HelmReleases, configuration, probes, replica controls,
  and ServiceAccount settings. A companion image remains paired with its
  primary service through the image registry and lock contract in ADR-031.
  A privileged worker stays disabled until an accepted identity and permission
  decision grants the access it needs; adding the API does not enable it.
- **Routes have their own inventory.** Each ingress profile adds
  `spi-osdu-ddms-routes`, backed by
  `software/stacks/osdu/routes/<shape>/ddms`. It depends on
  `spi-osdu-ddms` and the selected gateway Kustomization. A DDMS route change
  does not reconcile core routes.
- **Azure remains the data plane.** DDMS-specific Cosmos containers, Storage
  objects, Service Bus entities, identities, and assignments are declared in
  Bicep and provisioned before Flux. The DDMS layer does not introduce an
  in-cluster database or adopt a service-specific deployment chart in place
  of `osdu-spi-service`.
- **Runtime registration is a load.** Tenant, subproject, dataset, and sample
  data creation do not run in the DDMS Kustomization and do not gate
  `deployable`. A DDMS that needs those records adds a named, idempotent load
  under ADR-040, with its state published by `spi info`.
- **The layer is still a readiness gate.** A failed DDMS leaves
  `spi-osdu-services` Ready and identifies the failed inventory directly, but
  the environment remains not deployable until the DDMS layer is Ready. Layer
  separation improves ownership and diagnosis; it does not turn a declared
  service into an optional failure.

Rejected: keep DDMS releases in `spi-osdu-services`. One inventory is shorter,
but a DDMS rollout, pin, or failure then shares the core health boundary.

Rejected: add a separate deployment profile for DDMS. It would make DDMS
optional at provision time, but it multiplies profile and ingress combinations
for services that the `core` environment declares as installed.

Rejected: put DDMS workloads in a new namespace. A namespace gives a stronger
Kubernetes policy boundary, but it duplicates mesh, configuration, and
ServiceAccount wiring while the workloads remain part of the OSDU product.

Rejected: deploy each DDMS through its upstream chart and bundled backend. It
reduces stack-owned manifests, but bypasses the shared chart, immutable image
lock, Workload Identity contract, and Azure PaaS data plane.

## Consequences

- A core deployment gains two gating inventories, one for DDMS workloads and
  one for their routes. Each ingress shape carries a matching DDMS route path.
- A DDMS failure no longer marks the core service Kustomization failed. It
  still keeps the environment not deployable and blocks fork testing.
- DDMS APIs can reconcile and be pinned without restarting sibling core
  services. Primary and companion images still move through one lock write.
- A newly deployed DDMS can be healthy before its tenant or sample data exists.
  Its acceptance descriptor names the required load when the suite needs that
  state.
- A privileged companion worker contributes no runtime capability until its
  identity, permissions, and data-plane feature gate are implemented.
