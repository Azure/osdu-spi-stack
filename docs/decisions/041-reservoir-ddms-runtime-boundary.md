# ADR-041: Reservoir DDMS Runtime Boundary

## Context

Open ETP server starts with application authentication disabled, and Open ETP client checks bearer-token shape without validating its signature. ADR-016 installs namespace-wide Istio JWT validation, but validation alone does not require a caller to present a token.

Open ETP client also keeps ETP sessions and transactions in a process-local map. A later transaction request routed to another replica cannot recover that state. On shutdown, the client attempts to roll back active transactions and close their WebSocket sessions.

The client calls in-cluster OSDU and Open ETP endpoints with the caller's bearer token. It does not call Azure PaaS directly and does not need a Kubernetes API token.

## Decision

Workload-scoped Istio `AuthorizationPolicy` resources protect Open ETP server and client. Exact health paths allow anonymous probes. Every other path requires a principal produced by the namespace `RequestAuthentication` from ADR-016.

Open ETP client runs with one replica until transaction state is externalized or a tested affinity contract keeps every transaction on its owning process. Its pod receives 45 seconds for termination, exceeding the application's 30-second transaction drain window.

The client uses the namespace `default` ServiceAccount with service-account token automount disabled and without the Azure Workload Identity label. Open ETP server retains its dedicated Azure workload identity because it accesses OSDU data-plane dependencies.

Rejected: rely on the applications' current authentication modes. The server explicitly disables authentication, and the client's token-shape check does not establish token authenticity.

Rejected: scale the client horizontally behind the ClusterIP. Load balancing does not preserve the process-local transaction map across requests.

Rejected: assign the shared workload identity to the client for consistency. The client has no Azure PaaS dependency, so the identity would add unused data-plane authority.

## Consequences

- REST, GraphQL, WebSocket, and local-token paths require an Istio-validated bearer principal.
- `/health` on the server and `/health/liveness` plus `/health/readiness` on the client remain available to Kubernetes probes without a bearer token.
- The client cannot provide horizontal capacity or pod-level failover while transaction state is process-local.
- A normal termination can spend up to 30 seconds draining transactions before Kubernetes ends the pod.
- The client has neither a projected Kubernetes API token nor Azure workload identity credentials.
