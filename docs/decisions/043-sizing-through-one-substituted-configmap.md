# ADR-043: Sizing Through One Substituted ConfigMap

## Context

Every CPU and memory request and limit, replica count, and volume size in the stack is a literal in a manifest, set for the shared environment. Elasticsearch runs 3 nodes with 128Gi each, Postgres 3 instances, and partition, entitlements, and legal 2 replicas. An environment that needs less, such as a CI run or a developer cluster, or more, can only get it by editing the stack and cutting a release that changes every environment. The image lock (ADR-017) already carries per-environment values into these manifests through Flux postBuild substitution.

## Decision

Sizing reaches the manifests the same way image pins do: Flux substitutes keys from a ConfigMap, and each manifest carries its default.

- **Each sized value is a placeholder with today's value as its default**, `${STORAGE_MEMORY_LIMIT:=1536Mi}`. Flux serializes each object again before substituting, so a value renders as the type it parses to: counts as integers, and a quantity such as `2` as a number Kubernetes reads the same as `"2"`.
- **`spi-sizing` in `osdu-flux` holds the overrides**, read through `postBuild.substituteFrom` with `optional: true` by `spi-elasticsearch`, `spi-redis`, `spi-postgresql`, `spi-airflow`, `spi-osdu-services`, and `spi-osdu-reference`. A key the ConfigMap does not set, or an environment without the ConfigMap, renders the default. Flux skips substitution entirely, defaults included, when it has no variables, so each of these Kustomizations always has one: the four middleware ones set an inline `SPI_SIZING_DEFAULTS`, and the service ones read the required image lock.
- **`src/spi/sizing.py` is the vocabulary.** It names every key and its default: replicas and CPU and memory requests and limits for each of the 13 services on the shared chart; count, resources, and storage for Elasticsearch; instances, resources, storage, and WAL storage for Postgres; replicas, resources, and storage for Redis; and resources for the Airflow API server, scheduler, DAG processor, and triggerer. `tests/test_sizing.py` fails when a manifest's placeholders or defaults differ from it, or when a Kustomization applying a sized manifest does not read the ConfigMap as optional or has no variable without it.
- **`spi up` writes `spi-sizing` from a `resources:` section**, in the environment declaration or in a stack file passed with `-f`. Both build on core: a declaration with `resources:` sets `profile: core` and lists `forks:`, since `env-upgrade` hands a declaration the environment only then, and a stack file takes no profile and is refused with `--profile bare` or `minimal` and on a declared environment. Before anything is read from Azure the section is checked: known components and fields, whole-number counts, memory and storage with a binary unit, CPU requests of at least 100m, each request at or below its limit, at least two Postgres instances and 4Gi of WAL storage, each pod's requests within one node of its pool, a node per Elasticsearch, Postgres, and Redis pod within the pool, and sized requests within the `platform` and `osdu` NodePool limits. After Azure is provisioned and before the first write to the cluster, the storage keys are compared with the claims already in `platform`. The ConfigMap then holds the section's keys and carries Flux's watch label, so a change reconciles at once; an empty section removes it. A declared core environment is written on every run; an undeclared one run without `-f` keeps its sizing.
- **The service chart renders its PodDisruptionBudget only at two or more replicas**, so a one-replica override with `minAvailable: 1` cannot block a node drain.

Rejected: HelmRelease `valuesFrom` for the Helm-based components. Keeps placeholders out of the manifests, but inline `spec.values` take precedence over `valuesFrom`, so an inline `replicaCount: 2` hides an override, and the Elasticsearch and Postgres resources are operator objects that need substitution anyway.

Rejected: resetting an undeclared environment's sizing when `spi up` runs without `-f`. Keeps the file the whole truth, but a plain rerun would quietly undo replica and resource choices; a declared environment reads its file on every run and needs no exception.

Rejected: a floor that overrides may not go below. Protects the shared environment's sizing, but the defaults are sized for that environment and smaller environments are the main reason to size.

## Consequences

- An environment changes size without a stack release, and the defaults in git remain the sizing of any environment that sets nothing.
- Substitution is textual. A bare number for memory or storage would render as an integer, which Postgres refuses as a storage size, so the checks require a unit. A ConfigMap edited by hand is not checked.
- The checks show a value is well formed and schedulable, not that it is enough for a given load, and the NodePool check counts only the sized workloads.
- Elasticsearch and Postgres storage can grow on a running environment, since the StorageClasses the CLI creates allow expansion, but never shrink. Redis keeps its volumes in a StatefulSet claim template, which Kubernetes does not let an upgrade change, so a Redis storage change on a running environment would fail the Helm upgrade. `spi up` refuses Elasticsearch or Postgres storage below what the claims hold, and Redis storage that differs from it.
- Postgres keeps one synchronous replica, and the operator rejects a cluster whose instance count does not exceed it, so Postgres runs at least two instances.
- On an existing environment the 13 services, Redis, and Airflow each get a Helm upgrade, since their values now set `replicaCount` and `resources` explicitly or carry `cpu: 1` where they carried `cpu: "1"`. The objects Kubernetes stores are unchanged, so no pod restarts.
- `scripts/validate_manifests.sh` validates each placeholder as its default.
- A declaration gains `resources:` only once its `stackVersion` names a release whose CLI reads the section; an older CLI refuses the key and `env-upgrade` stops before it deploys.
