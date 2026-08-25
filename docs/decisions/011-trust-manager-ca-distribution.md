# ADR-011: Cross-Namespace CA Distribution via trust-manager

## Context

OSDU services in `osdu` speak TLS to two in-cluster middleware systems in `platform`:

- **Redis** (TLS via cert-manager self-signed CA). The Lettuce client's truststore must trust the Redis CA.
- **Elasticsearch** (HTTP TLS via ECK-managed self-signed CA). Search and indexer need the ECK CA.

Both CAs live as Secrets in the `platform` namespace. Kubernetes Secrets do not cross namespaces. The OSDU services expect to read these CAs from Secrets in their own namespace so the chart's `import-ca-certs` init container can fold them into a Java truststore.

A second, unrelated concern: Istio in the `osdu` namespace wraps outbound traffic in mTLS by default. Lettuce already speaks TLS directly to Redis, and Istio's automatic mTLS wraps that connection in a second TLS layer that Lettuce cannot unwind (TLS-in-TLS).

## Decision

Handle both concerns declaratively, entirely inside the GitOps tree:

- **CA mirroring.** Install trust-manager in `foundation`. Declare two `Bundle` CRs in `software/stacks/osdu/bootstrap/ca-bundles.yaml` that source from the platform-namespace CA Secrets (`redis-tls-secret`, `elasticsearch-es-http-certs-public`) and target Secret outputs in the `osdu` namespace (`redis-ca-cert`, `elastic-ca-cert`).
- **Redis mTLS disable.** Apply a static Istio `DestinationRule` (`redis-disable-mtls` in `osdu`) that sets `trafficPolicy.tls.mode: DISABLE` for `platform-redis-master.platform.svc.cluster.local`.

Both resources ship in one Kustomization (`spi-bootstrap`, Layer 4b per ADR-007) that depends on trust-manager, Elasticsearch, Redis, and the osdu-config Kustomization. trust-manager is locked down: `secretTargets.authorizedSecrets` is pinned to the two names above, and its default-CAs package is disabled.

Rejected:
- **A Flux-managed Job with a dedicated UAMI** that reads Secrets across namespaces and writes them. Works, but needs a dedicated identity, federated credential, and RBAC role assignment; and re-runs only on a deliberate trigger. trust-manager watches the sources and reconciles continuously on CA rotation.
- **kubernetes-replicator or Reflector.** Equivalent at the controller level, but pulls in a cluster-wide controller with cluster-scope Secret permissions for two targets. trust-manager's allowlist is tighter.
- **Imperative CA copy from the CLI on every `spi up`.** Brittle (stale on CA rotation), requires the CLI to stay open, and lives outside the GitOps source of truth.

## Consequences

- Fresh checkout plus a Flux bootstrap reproduces the full CA and DestinationRule state. No imperative tail.
- CA rotations (cert-manager renewal, ECK CA rotation) re-sync the target Secrets automatically; OSDU service pods pick up the new CA on next restart.
- trust-manager's blast radius is scoped: two authorized target Secret names, source CAs watched only in `platform`.
- The Redis DestinationRule is a single static manifest; adding a second in-cluster TLS target is another entry in the same Kustomization.
