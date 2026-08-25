# ADR-001: Azure PaaS for OSDU Data Services

## Context

OSDU services need persistent document, graph, and object storage; asynchronous messaging; centralized secret management; and an identity provider. A cloud-agnostic stack runs all of these in-cluster (PostgreSQL, Keycloak, RabbitMQ, MinIO, Kubernetes Secrets). Each adds operational surface: backup, upgrade, monitoring, capacity planning.

OSDU's upstream Azure provider modules are already written against Azure Cosmos DB, Service Bus, Storage, Key Vault, and Entra ID. Pointing them at those services is a configuration choice, not a rewrite.

## Decision

Use Azure PaaS for every data service with a managed equivalent, and leave the SPI Stack explicitly Azure-only:

| Concern | In-cluster equivalent | Azure PaaS (SPI Stack) |
|---|---|---|
| Document store | PostgreSQL | Cosmos DB SQL (per partition) |
| Graph store | PostgreSQL | Cosmos DB Gremlin (shared) |
| Messaging | RabbitMQ | Service Bus (per partition, 14 topics) |
| Object storage | MinIO | Azure Storage (common + per partition) |
| Secret store | Kubernetes Secrets | Azure Key Vault |
| Identity | Keycloak | Azure Entra ID |

Rejected: run the same workloads in-cluster for portability. The SPI Stack targets Azure-specific OSDU provider code; portability is not a goal.

## Consequences

- Five stateful systems leave the cluster. Karpenter provisions fewer nodes; no in-cluster backup or upgrade strategy is needed for them.
- The stack is locked to Azure. A different cloud is a fork, not a flag.
- Every PaaS resource is reached via Workload Identity (ADR-005); no connection strings land as Kubernetes Secrets.
- The CLI must provision these resources before Flux can reconcile the services that depend on them (ADR-008, ADR-009).
- A small in-cluster middleware surface remains (Elasticsearch, Redis, PostgreSQL for Airflow); its scope is the subject of ADR-003.
