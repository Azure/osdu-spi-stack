# ADR-007: Layered Flux Kustomization Ordering

## Context

A Kubernetes workload graph has hard ordering constraints: CRDs before CRs, operators before instances, cert-manager before certs, middleware before consumers. Applying everything at once surfaces as CrashLoopBackOff and CRD-not-found errors that resolve eventually but obscure real failures.

Flux Kustomizations with explicit `dependsOn` encode those constraints once, in Git, where the graph is reviewable.

## Decision

The core profile (`software/stacks/osdu/profiles/core/stack.yaml`) defines a set of ordered layers (0a through 6) wired by explicit `dependsOn`, including three one-shot Job layers (`spi-osdu-init`, `spi-osdu-schema-load`, `spi-osdu-legal`). Kustomizations within the same layer reconcile in parallel when they have no mutual dependency.

| Layer | Kustomization(s) | Depends on |
|---|---|---|
| 0a | `spi-namespaces` | none |
| 0b | `spi-nodepools` | 0a |
| 1 | `spi-cert-manager`, `spi-trust-manager`, `spi-eck-operator`, `spi-cnpg-operator`, `spi-helm-sources`, `spi-gateway` (ownership handoff, ADR-025) | 0a (trust-manager also on cert-manager) |
| 2 | `spi-elasticsearch`, `spi-redis`, `spi-postgresql` | matching L1 operator + 0b (Redis: cert-manager + helm-sources) |
| 3 | `spi-airflow` | `spi-postgresql` |
| 4a | `spi-osdu-config` | 0a |
| 4b | `spi-bootstrap` (trust-manager Bundles + Redis DestinationRule, ADR-011) | trust-manager, ES, Redis, osdu-config |
| 5 | `spi-osdu-services` (core services) | 4b, 0b |
| 5a | `spi-osdu-init` (partition + entitlements bootstrap, ADR-015) | `spi-osdu-services` |
| 5b | `spi-osdu-schema-load` (one-shot Job, ADR-013) | `spi-osdu-init` |
| 5c | `spi-osdu-legal` (legal tag seeding, ADR-015) | `spi-osdu-init` |
| 6 | `spi-osdu-reference` (reference services) | 5, 5b |

The ingress profile (`software/stacks/osdu/ingress/<mode>/stack.yaml`, ADR-012) attaches additional Kustomizations at Layer 1 (the Gateway rendering, for which the selected ingress tree is the sole Flux owner per ADR-025, plus cert issuers, ExternalDNS, TLS overlays) and Layer 6 (`spi-middleware-routes`, `spi-osdu-routes`). The two profiles reconcile independently under one `fluxConfigurations` resource (ADR-009).

The `minimal` profile (ADR-021) declares layers 0a through 4b verbatim and stops, pairing with the `<mode>-minimal` ingress trees so no `dependsOn` is left unsatisfiable.

All Kustomizations use `wait: true` so each layer's Ready gate reflects actual workload health; per-layer `timeout` is tuned to the slowest workload in that layer (15 min for Elasticsearch and Airflow, 10 min for the other middleware, 30 min for the OSDU service layers; schema-load's 155 min tracks the Job's `activeDeadlineSeconds` of 9000 s, a pod-startup allowance plus the cold-cluster wait and the throttled load, with headroom for reconcile overhead).

`spi-osdu-legal` is the one layer nothing depends on. It carries `spi-stack.gating: "false"`, so a failed seed stays visible with its typed reason without holding back the Ready verdict, and its 70 min timeout tracks the legal Job's own deadline rather than any downstream layer.

Rejected: one flat Kustomization with an implicit apply order. Apply order in kustomize is not a dependency graph; it gives no ordering guarantees across independent sources.

## Consequences

- Later layers start only when earlier layers report `Ready`. Spurious CRD-not-found startup noise is gone.
- The graph is reviewable in one file and surfaces in `flux get kustomizations` and `spi status`.
- Adding a new middleware means inserting a Kustomization at the right layer and wiring `dependsOn`; the cost is one file and one edit to the profile `stack.yaml`.
- `wait: true` on middleware layers is a trade-off: a slow-starting operator delays everything behind it. Timeouts are tuned per layer.
