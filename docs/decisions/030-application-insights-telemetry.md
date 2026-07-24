# ADR-030: Application Insights for Service Telemetry

**Status**: Accepted

## Context

OSDU's Azure provider services are built on `core-lib-azure`, which registers an Application Insights request-telemetry filter at startup. With no connection string the filter dereferences a null configuration and the service NPEs before it serves traffic.

ADR-002 (now ADR-026) gives cluster-level Managed Prometheus and Container Insights, which cover node and container metrics and logs. They do not carry the per-request, per-service application traces and dependency calls that `core-lib-azure` is wired to emit, and they do not satisfy the agent's startup expectation of a connection string.

An interim fix wrote a dummy/disabled connection string to stop the NPE. That
unblocks startup without creating paid telemetry resources; operators can opt
into real request/dependency telemetry when the environment needs it.

## Decision

Support optional Application Insights in `infra/main.bicep` and wire either
real or disabled configuration to every service:

- A workspace-based **Application Insights** component (`Microsoft.Insights/components`, `kind: web`) linked to a **Log Analytics** workspace (`PerGB2018`, 30-day retention).
- `spi up --application-insights` deploys the workspace and component. New
  environments default to disabled to avoid telemetry cost.
- The connection string is a Bicep output the CLI writes into the `osdu-config`
  ConfigMap as `APPLICATIONINSIGHTS_CONNECTION_STRING`, mounted into every
  service via `envFrom` (ADR-010).
- The `osdu-spi-service` chart sets `APPLICATIONINSIGHTS_ROLE_NAME` per service so each one is a distinct node on the Application Insights application map.

When disabled, the CLI writes a syntactically valid dummy key and a non-routable
localhost ingestion endpoint. The Java agent still initializes, so
`core-lib-azure` does not NPE. Inline agent configuration sets sampling to zero,
disables live metrics, profiler, and statsbeat, and disables disk persistence so
no telemetry leaves the cluster or accumulates for retry.
When `osdu-config` changes on an existing environment, `spi up` restarts the
OSDU deployments because Kubernetes does not refresh `envFrom` values in
already-running pods.

The selected mode is persisted on the resource group and cannot be changed in
place. Recreate the disposable environment to switch modes; this prevents a
routine rerun from orphaning paid resources or silently changing pod
configuration.
Dry-run creates the empty resource group required by Azure what-if, but does
not persist the mode. The first real deployment remains free to choose either
setting.

Rejected: Container Insights only when request-level APM is required. It has no
per-request traces or distributed dependencies. Rejected: a classic
(non-workspace) Application Insights resource. Workspace-based is the supported
shape and keeps logs and traces in one Log Analytics workspace.

## Consequences

- Default deployments start cleanly with disabled telemetry and no Application
  Insights or Log Analytics resources.
- Opt-in deployments emit request, dependency, and exception telemetry,
  attributable per service via the cloud role name.
- Opt-in adds one Application Insights component plus a Log Analytics workspace;
  retention is capped at 30 days to bound cost.
