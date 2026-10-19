# ADR-043: Sizing Through One Substituted ConfigMap

## Context

Every CPU and memory request and limit, replica count, and volume size in the stack is a literal in a manifest, set for the shared environment. Elasticsearch runs 3 nodes with 128Gi each, Postgres 3 instances, and partition, entitlements, and legal 2 replicas. An environment that needs less, such as a CI run or a developer cluster, or more, can only get it by editing the stack and cutting a release that changes every environment. The image lock (ADR-017) already carries per-environment values into these manifests through Flux postBuild substitution.

## Decision

Sizing reaches the manifests the same way image pins do: Flux substitutes keys from a ConfigMap, and each manifest carries its default.

- **Each sized value is a placeholder with today's value as its default**, `${STORAGE_MEMORY_LIMIT:=1536Mi}`. Quantities are quoted so they stay strings; counts are not, so they render as integers.
- **`spi-sizing` in `osdu-flux` holds the overrides**, read through `postBuild.substituteFrom` with `optional: true` by `spi-elasticsearch`, `spi-redis`, `spi-postgresql`, `spi-airflow`, `spi-osdu-services`, and `spi-osdu-reference`. A key the ConfigMap does not set, or an environment without the ConfigMap, renders the default.
- **`src/spi/sizing.py` is the vocabulary.** It names every key and its default: replicas and CPU and memory requests and limits for each of the 13 services on the shared chart; count, resources, and storage for Elasticsearch; instances, resources, storage, and WAL storage for Postgres; replicas, resources, and storage for Redis; and resources for the Airflow API server, scheduler, DAG processor, and triggerer. `tests/test_sizing.py` fails when a manifest's placeholders or defaults differ from it, or when a Kustomization applying a sized manifest does not read the ConfigMap as optional.
- **The service chart renders its PodDisruptionBudget only at two or more replicas**, so a one-replica override with `minAvailable: 1` cannot block a node drain.

Rejected: HelmRelease `valuesFrom` for the Helm-based components. Keeps placeholders out of the manifests, but inline `spec.values` take precedence over `valuesFrom`, so an inline `replicaCount: 2` hides an override, and the Elasticsearch and Postgres resources are operator objects that need substitution anyway.

Rejected: a floor that overrides may not go below. Protects the shared environment's sizing, but the defaults are sized for that environment and smaller environments are the main reason to size.

## Consequences

- An environment changes size without a stack release, and the defaults in git remain the sizing of any environment that sets nothing.
- Substitution is textual, so an override that is not a valid quantity reaches the cluster unless whatever writes the ConfigMap validates it first.
- Storage values can grow on a running environment, since the StorageClasses the CLI creates allow expansion, but never shrink. Redis keeps its volumes in a StatefulSet claim template the chart cannot change, so its storage takes effect only when the claims are created.
- The 13 service HelmReleases now set `replicaCount` and `resources` explicitly. On an existing environment Helm records an upgrade per service whose rendered objects are unchanged, so no pod restarts.
- `scripts/validate_manifests.sh` validates each placeholder as its default.
