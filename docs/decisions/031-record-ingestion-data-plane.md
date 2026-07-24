# ADR-031: Record-Ingestion Data-Plane Enablement

**Status**: Accepted

## Context

ADR-015 deliberately scoped the bootstrap to a schema-loaded cluster and deferred record ingestion ("Reference data, legal tags ... remain out of scope; add later if the stack needs record ingestion"). Reaching all Kustomizations Ready and all pods green therefore proves the control plane and bootstrap, but never exercises the `storage -> indexer-queue -> indexer -> search` data flow or the system-service (schema, workflow) query paths.

Running those paths for the first time surfaced three latent gaps that green pods masked. They are tenant-agnostic; they break record ingestion in any tenant, not just the Microsoft one. The fourth gap on this path, the Cosmos SQL data-plane role, is covered by ADR-027.

## Decision

Close the three gaps so the record-ingestion data plane works end to end, all in Bicep / GitOps:

- **System-partition Cosmos secrets.** Write `system-cosmos-endpoint`, `system-cosmos-primary-key`, and `system-cosmos-connection` to Key Vault from the primary partition module (the `osdu-system-db` database lives in the primary partition's Cosmos account). System services resolve their catalog from these `system-` prefixed secrets; without them schema and workflow fail with "system-cosmos-endpoint cannot be null".
- **Per-partition record container.** Pre-create the blob container named after the partition id (`opendes`) alongside the fixed service containers. `storage-azure` writes record bodies there and `core-lib-azure`'s `BlobStore` does not auto-create it, so ingestion otherwise fails with 404 `ContainerNotFound`.
- **Elasticsearch TLS alignment.** Set `elastic-ssl-enabled: true` in the partition record and point `elastic-endpoint` at `elasticsearch-es-http.platform.svc`. ECK serves HTTPS with a self-signed cert whose CA trust-manager already distributes (ADR-011); the old `false` flag forced plaintext against the TLS port (ConnectionClosed) and the `...svc.cluster.local` FQDN is absent from the cert SANs (SSLPeerUnverifiedException).

Rejected: leave record ingestion unverified. Green pods gave a false "ready" signal; only a real API smoke test exposes these. Rejected: disable ECK HTTP TLS to match the old plaintext flag. That throws away the CA-distribution and truststore design ADR-011 already built, and weakens in-cluster traffic that Istio mTLS plus ECK TLS otherwise protect.

## Consequences

- The full `storage create -> recordstopic -> indexer-queue -> indexer -> search` flow works, which is also the end-to-end proof of the indexer-queue Workload-Identity image (ADR-027).
- A 13-check in-cluster smoke test (a Job under `workload-identity-sa`) exercises every service through the mesh and is the regression guard for this path.
- Readiness is now judged at the data plane, not just at pod and Kustomization readiness. A deploy is "green" only when ingestion and search succeed.
