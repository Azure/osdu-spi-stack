---
status: "accepted"
contact: "danielscholl"
date: "2026-07-24"
deciders: "danielscholl"
---

# Opt-In Application Insights Provisioning

## Context and Problem Statement

OSDU service images bundle the Application Insights Java agent, and
`core-lib-azure >= 2.5.6` reads request-telemetry context on every request
with no null guard: services return HTTP 500 on every request when the
agent is enabled but App Insights is not initialized. The stack needs a
telemetry story that neither forces every developer to pay for a Log
Analytics workspace nor breaks services that expect an agent context.

## Decision Drivers

- Dev/test deployments are cost- and time-sensitive; telemetry backends
  add both.
- Service behavior must be correct in both modes (real telemetry and none).

## Considered Options

- Opt-in provisioning behind a Bicep parameter, dummy agent config otherwise
- Always provision App Insights + Log Analytics
- Never provision; document a manual wiring procedure

## Decision Outcome

Chosen option: "Opt-in behind `enableApplicationInsights`" (default
`false`) in `infra/main.bicep`, deploying a workspace-based Application
Insights component plus Log Analytics only when requested and exposing the
connection string as a deployment output. When disabled, the CLI's
osdu-config carries disabled/dummy agent configuration so the bundled
agent stays inert.

### Consequences

- Good, because default deployments carry no telemetry cost or extra
  deploy time.
- Good, because enabling telemetry is a single parameter, not a manual
  wiring procedure.
- Bad, because CLI surfacing (flag and osdu-config wiring) must follow for
  the parameter to be reachable end to end.
