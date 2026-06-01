# ADR-007: Layered Flux Kustomization Ordering

**Status**: Accepted

## Context

A Kubernetes workload graph has hard ordering constraints: CRDs before CRs, operators before instances, cert-manager before certs, middleware before consumers. Applying everything at once surfaces as CrashLoopBackOff and CRD-not-found errors that resolve eventually but obscure real failures.

Flux Kustomizations with explicit `dependsOn` let us encode those constraints once, in Git, where the graph is reviewable.

## Decision

The core profile (`software/stacks/osdu/profiles/core/stack.yaml`) defines a set of ordered layers (0a through 6) wired by explicit `dependsOn`, including two one-shot Jobs (`spi-osdu-init`, `spi-osdu-schema-load`). Kustomizations within the same layer reconcile in parallel when they have no mutual dependency.

| Layer | Kustomization(s) | Depends on |
|---|---|---|
| 0a | `spi-namespaces` | none |
| 0b | `spi-nodepools` | 0a |
| 1 | `spi-cert-manager`, `spi-trust-manager`, `spi-eck-operator`, `spi-cnpg-operator`, `spi-gateway` | 0a (trust-manager also on cert-manager) |
| 2 | `spi-elasticsearch`, `spi-redis`, `spi-postgresql` | matching L1 operator + 0b |
| 3 | `spi-airflow` | `spi-postgresql` |
| 4a | `spi-osdu-config` | 0a |
| 4b | `spi-bootstrap` (trust-manager Bundles + Redis DestinationRule, ADR-011) | trust-manager, ES, Redis, osdu-config |
| 5 | `spi-osdu-services` (core services) | 4b, 0b |
| 5a | `spi-osdu-init` (partition + entitlements bootstrap, ADR-015) | `spi-osdu-services` |
| 5b | `spi-osdu-schema-load` (one-shot Job, ADR-013) | `spi-osdu-init` |
| 6 | `spi-osdu-reference` (reference services) | 5, 5b |

The ingress profile (`software/stacks/osdu/ingress/<mode>/stack.yaml`, ADR-012) attaches additional Kustomizations at Layer 1 (cert issuers, ExternalDNS, TLS overlays) and Layer 6 (HTTPRoutes). The two profiles reconcile independently under one `fluxConfigurations` resource (ADR-009).

All Kustomizations use `wait: true` so each layer's Ready gate reflects actual workload health; per-layer `timeout` is tuned to the slowest workload in that layer (15 min for Elasticsearch and Airflow, 30 min for the OSDU service layers, 35 min for schema-load).

Rejected: one flat Kustomization with an implicit apply order. Apply order in kustomize is not a dependency graph; it gives no ordering guarantees across independent sources.

## Consequences

- Later layers start only when earlier layers report `Ready`. Spurious CRD-not-found startup noise is gone.
- The graph is reviewable in one file and surfaces in `flux get kustomizations` and `spi status`.
- Adding a new middleware means inserting a Kustomization at the right layer and wiring `dependsOn`; the cost is one file and one edit to the profile `stack.yaml`.
- `wait: true` on middleware layers is a trade-off: a slow-starting operator delays everything behind it. Timeouts are tuned per layer.
