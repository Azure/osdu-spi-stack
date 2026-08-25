# ADR-006: Three-Namespace Model

## Context

Workloads need isolation boundaries for ownership, Istio sidecar injection, and resource policy. A common pattern is four namespaces (`foundation`, `istio-system`, `platform`, `osdu`), but AKS Automatic's managed Istio owns `aks-istio-system` and `aks-istio-ingress` itself.

## Decision

The SPI Stack creates and reconciles three application namespaces:

| Namespace | Purpose | Istio injection | Contents |
|---|---|---|---|
| `foundation` | Cluster operators | No | ECK, CNPG, cert-manager, trust-manager |
| `platform` | Stateful middleware and ingress | No | Elasticsearch, Redis, PostgreSQL, Airflow, Istio Gateway |
| `osdu` | OSDU services | Yes (`istio.io/rev`) | OSDU services, schema-load Job, `osdu-config` ConfigMap, `workload-identity-sa` |

`flux-system` is owned by the AKS Flux extension (ADR-009). `aks-istio-system` and `aks-istio-ingress` are owned by AKS.

Only `osdu` carries the Istio sidecar injection label. `platform` is intentionally out of the mesh: `istio-init` requires `NET_ADMIN`, which Safeguards rejects, and the middleware components already terminate their own TLS where required.

Rejected: collapse `foundation` into `platform` to save one namespace. Operators and their managed workloads should not share a namespace; a bad operator upgrade should not take out its managed cluster in the same blast radius.

## Consequences

- Clear ownership boundaries: `foundation` is infrastructure operators, `platform` is middleware tenants, `osdu` is the product.
- Cross-namespace CA material is distributed deliberately (ADR-011), not ambient.
- Resource quotas and NetworkPolicies can be scoped per namespace without accidentally catching operator workloads.
- `osdu-config` and in-cluster secrets sit in `osdu`, not `flux-system`; HelmReleases reference them by namespace.
