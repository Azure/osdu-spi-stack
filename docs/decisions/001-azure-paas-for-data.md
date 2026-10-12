# ADR-001: Azure PaaS for OSDU Data Services

## Context

OSDU services need persistent document, graph, relational, and object storage;
asynchronous messaging; centralized secret management; and an identity provider.
A cloud-agnostic stack runs these in-cluster (PostgreSQL, Keycloak, RabbitMQ,
MinIO, Kubernetes Secrets). Each adds operational surface: backup, upgrade,
monitoring, capacity planning.

OSDU's upstream Azure provider modules are written against Azure Cosmos DB,
Service Bus, Storage, Key Vault, Entra ID, and Azure Database for PostgreSQL.
Pointing them at those services is a configuration choice, not a rewrite.

## Decision

Use Azure PaaS for every data service with a managed equivalent, and leave the SPI Stack explicitly Azure-only:

| Concern | In-cluster equivalent | Azure PaaS (SPI Stack) |
|---|---|---|
| Document store | PostgreSQL | Cosmos DB SQL (per partition) |
| Graph store | PostgreSQL | Cosmos DB Gremlin (shared) |
| Reservoir store | PostgreSQL | PostgreSQL Flexible Server (per partition) |
| Messaging | RabbitMQ | Service Bus (per partition, 14 topics) |
| Object storage | MinIO | Azure Storage (common + per partition) |
| Secret store | Kubernetes Secrets | Azure Key Vault |
| Identity | Keycloak | Azure Entra ID |

Rejected: run the same workloads in-cluster for portability. The SPI Stack targets Azure-specific OSDU provider code; portability is not a goal.

## Consequences

- Reservoir data joins the document, graph, object, messaging, and secret state
  outside the cluster. Karpenter provisions fewer nodes; no in-cluster backup
  or upgrade strategy is needed for those systems.
- The stack is locked to Azure. A different cloud is a fork, not a flag.
- Every PaaS resource is reached via Workload Identity (ADR-005); the Reservoir
  connection string in Key Vault carries no password.
- The CLI must provision these resources before Flux can reconcile the services that depend on them (ADR-008, ADR-009).
- A small in-cluster middleware surface remains (Elasticsearch, Redis, PostgreSQL for Airflow); its scope is the subject of ADR-003.
