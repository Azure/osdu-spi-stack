---
status: "accepted"
contact: "danielscholl"
date: "2026-07-28"
deciders: "SPI Stack maintainers"
---

# Airflow 3, Single-Engine

## Context and Problem Statement

Airflow 3 restructures the deployment relative to Airflow 2: the webserver becomes an `api-server` (UI + REST API + task execution API), DAG parsing moves to a standalone `dag-processor`, the REST API moves from `/api/v1` with per-request Basic auth to `/api/v2` with a JWT token exchange, and the chart introduces dedicated api-secret/JWT signing keys alongside the fernet key.

The question is how the stack adopts a new Airflow major: as the sole engine, or hedged behind transition machinery (dual engines, a version switch, service-name indirection) that lets deployments choose.

## Decision Drivers

- The stack has no installed base; there is nothing to migrate and no transition window to serve.
- Dual-engine support is pure carrying cost: every mechanism it needs (component duplication, profile overlays, path rewriting, service-name indirection) exists only to serve a transition.
- Flux re-renders Helm values on every reconcile. The chart mints its api-secret and JWT keys fresh on each render, and keeps its fernet key in a pre-install-hook Secret that lives outside the Helm release — so any key not seeded by the CLI is either unstable or untracked.
- Workflow→Airflow integration is optional in this stack (no DAGs are shipped; `IGNORE_DAGCONTENT=true`), so the Airflow version can move independently of the engines the OSDU workflow service supports.

## Considered Options

- **Airflow 3 only.** One component, one engine, chart 1.22.x pinned to Airflow 3.2.2.
- **Dual-engine behind a version switch.** Keep Airflow 2 deployable as a fallback.
- **Track the OSDU ecosystem.** Hold Airflow at the newest major the community workflow service supports.

## Decision Outcome

Chosen option: "Airflow 3 only."

`software/components/airflow` deploys chart `1.22.*` with `airflowVersion`/`defaultAirflowTag` pinned to `3.2.2` (so a chart bump can never silently change the Airflow release). The topology is api-server, scheduler, dag-processor, triggerer, statsd, with KubernetesExecutor and CNPG Postgres. The triggerer stays enabled so deferrable operators remain available.

Key mechanics:

- **All signing material is CLI-seeded.** `airflow-api-credentials` carries the admin password, `api-secret-key`, `jwt-secret`, and `fernet-key`, referenced via `apiSecretKeySecretName` / `jwtSecretName` / `fernetKeySecretName`. This keeps every key stable across Flux reconciles and inside CLI ownership; chart-managed alternatives either rotate per render (api-secret, JWT) or live in an untracked hook Secret (fernet).
- **Subpath serving needs no route changes.** `config.api.base_url`'s path (`/airflow`) becomes the FastAPI `root_path`; Starlette strips the prefix when present and matches without it otherwise, so the gateway forwards `/airflow`-prefixed requests unrewritten, the chart's un-prefixed health probes keep working, and the chart derives `execution_api_server_url` from the same path. Because the UI router's basename is that path, the Airflow URL keeps the `/airflow` suffix even on a dedicated host (dns mode).
- **Service identity.** Routes and ReferenceGrants target `airflow-api-server`. The FAB auth manager (chart default) keeps the `createUserJob` admin-user flow valid; the admin identity lives solely in the job's args.
- **Schema lifecycle.** The chart's migrate job (`airflow db migrate`, `useHelmHooks: false` so Flux runs it) initializes and upgrades the metadata schema.

Deliberately omitted: dual-engine switching machinery of any kind, runtime `_PIP_ADDITIONAL_REQUIREMENTS` package installs (the stack ships no DAGs, and runtime pip trades slow, registry-dependent pod starts for the image immutability the rest of the stack assumes), and Istio sidecar opt-outs (the `platform` namespace has no sidecar injection).

### Workflow service

`OSDU_AIRFLOW_URL` targets `airflow-api-server`. The Airflow version deployed here is decoupled from the engines the OSDU workflow service's images support: when the deployed workflow image carries an Airflow 3 client, engine selection is wired through its Azure provider config (`OSDU_AIRFLOW_VERSION=airflow3` plus `OSDU_AIRFLOW_AIRFLOW3_URL/USERNAME/PASSWORD`, the latter requiring the admin credential mirrored into the `osdu` namespace). Until then, workflow→Airflow API calls are unavailable — acceptable because this stack loads no DAGs.

### Consequences

- Good, because the stack tracks the current Airflow major with one component and zero transition machinery.
- Good, because signing-key lifecycle is correct under Flux: nothing rotates on reconcile, and every key is CLI-owned.
- Bad, because the OSDU workflow service can lag Airflow majors, leaving its Airflow integration dormant until its images support the deployed engine.
