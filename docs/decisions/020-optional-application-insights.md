# ADR-020: Opt-In Application Insights Provisioning

## Context

OSDU service images bundle the Application Insights Java agent, and `core-lib-azure >= 2.5.6` reads request-telemetry context on every request with no null guard: services return HTTP 500 on every request when the agent is enabled but App Insights is not initialized. The stack needs a telemetry story that neither forces every developer to pay for a Log Analytics workspace nor breaks services that expect an agent context. Dev/test deployments are cost- and time-sensitive; telemetry backends add both, and service behavior must be correct in both modes (real telemetry and none).

## Decision

Provisioning is opt-in behind `enableApplicationInsights` (default `false`) in `infra/main.bicep`, deploying a workspace-based Application Insights component plus Log Analytics only when requested and exposing the connection string as a deployment output. When disabled, the CLI's osdu-config carries disabled/dummy agent configuration so the bundled agent stays inert.

Rejected: always provision App Insights and Log Analytics. Adds telemetry cost and deploy time to every dev/test environment.

Rejected: never provision and document a manual wiring procedure. Trades a one-parameter enable for a hand-executed sequence.

## Consequences

- Default deployments carry no telemetry cost or extra deploy time.
- Enabling telemetry is a single parameter, not a manual wiring procedure.
- CLI surfacing (flag and osdu-config wiring) must follow for the parameter to be reachable end to end.
