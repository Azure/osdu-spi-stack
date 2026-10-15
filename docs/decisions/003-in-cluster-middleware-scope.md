# ADR-003: In-Cluster Middleware Scope

## Context

ADR-001 moved every data service with a managed Azure equivalent out of the cluster. Three middleware components have no clean managed drop-in for OSDU's upstream service code:

- **Elasticsearch.** OSDU search and indexer services call the Elasticsearch REST API directly. Azure AI Search is not API-compatible.
- **Redis.** OSDU services require custom CA TLS and specific database isolation (db 1 through 6). Azure Cache for Redis is reachable but adds network latency and cost for what is effectively a cache.
- **PostgreSQL (for Airflow only).** Airflow's scheduler and workers need low-latency access to their metadata database; a single-database workload is an expensive fit for Azure Database for PostgreSQL.

Airflow itself is in-cluster because it orchestrates ingestion workflows that call the in-cluster OSDU APIs.

## Decision

Run exactly three shared stateful middleware systems in the `platform`
namespace, each under a Kubernetes operator:

| Middleware | Operator | Sizing |
|---|---|---|
| Elasticsearch | ECK | 3 nodes, 128 GiB each |
| Redis (TLS) | Bitnami chart + cert-manager | 1 master + 2 replicas, 8 GiB each |
| PostgreSQL (Airflow metadata) | CloudNativePG | 3 instances, 10 GiB + 4 GiB WAL |

Airflow is a non-stateful middleware tenant of this namespace (api-server, scheduler, dag-processor, triggerer).

A service may own an ephemeral middleware Deployment in the `osdu` namespace
when the shared system lacks an API the service requires. Rock and Fluid
Sample DDMS owns `rafs-redis`, a single Redis Stack instance with RedisJSON and
RediSearch. It uses `emptyDir`; OSDU Storage and Search remain the durable
sources, and rebuilding the RAFS index restores its contents. The shared Redis
deployment remains the cache for services that require database isolation and
TLS.

Rejected:
- Azure Cache for Redis, Azure Database for PostgreSQL: extra latency and cost for workloads that benefit from co-location.
- Azure AI Search: not a drop-in for the Elasticsearch APIs OSDU uses.

## Consequences

- Three stateful workloads still require in-cluster Premium SSD storage (450 GiB provisioned in total).
- RAFS adds a 4 GiB memory request and 6 GiB limit for data derived from OSDU
  records. It adds no persistent volume or backup surface.
- The CA chains for Elasticsearch HTTP and Redis TLS are self-signed (cert-manager + ECK) and must reach the `osdu` namespace; ADR-011 covers the distribution mechanism.
- Operator updates (ECK, CNPG) move with their Flux HelmReleases and land through the normal reconcile loop.
- Backup and disaster recovery for these systems are out of scope for the SPI Stack's dev/test framing; durable state lives in the Azure PaaS resources covered by ADR-001.
