---
status: "accepted"
contact: "danielscholl"
date: "2026-07-28"
deciders: "SPI Stack maintainers"
---

# Airflow 3, Single-Engine

## Context and Problem Statement

The stack briefly carried Airflow 2.10.5 (chart 1.16.x), which is in maintenance mode upstream. Airflow 3 restructures the deployment: the webserver becomes an `api-server` (UI + REST API + task execution API), DAG parsing moves to a standalone `dag-processor`, the REST API moves from `/api/v1` with per-request Basic auth to `/api/v2` with a JWT token exchange, and the chart introduces dedicated api-secret/JWT signing keys alongside the fernet key.

A sibling project (cimpl-stack) adopted Airflow 3 by running both engines behind a feature flag. That transition state cost it a duplicated component tree, ten profile overlays, a stack rewriter, and per-instance engine metadata — none of which serves a repo with no installed base.

## Decision Drivers

- The stack has no users yet; there is nothing to migrate and no transition window to serve.
- A dual-engine switch is pure carrying cost: every mechanism it needs (component duplication, path rewriting, service-name indirection) exists only for a transition.
- Flux re-renders Helm values on every reconcile. The chart mints its api-secret and JWT keys fresh on each render, and keeps its fernet key in a pre-install-hook Secret that lives outside the Helm release — so any key not seeded by the CLI is either unstable or untracked.
- The OSDU workflow service's Airflow 3 client is not yet in community `master` (it is in review on an unmerged upstream branch), but workflow→Airflow integration is inert in this stack today (no DAGs are loaded; `IGNORE_DAGCONTENT=true`).

## Considered Options

- **Airflow 3 only.** One component, one engine, chart 1.22.x pinned to Airflow 3.2.2.
- **Dual-engine with a flag (cimpl-stack model).** Keep Airflow 2 as fallback behind a switch.
- **Stay on Airflow 2** until the OSDU workflow service supports Airflow 3 in `master`.

## Decision Outcome

Chosen option: "Airflow 3 only."

`software/components/airflow` deploys chart `1.22.*` with `airflowVersion`/`defaultAirflowTag` pinned to `3.2.2` (so a chart bump can never silently change the Airflow release). The topology is api-server, scheduler, dag-processor, triggerer, statsd, with KubernetesExecutor and CNPG Postgres. The triggerer stays enabled so deferrable operators remain available.

Key mechanics:

- **All signing material is CLI-seeded.** `airflow-api-credentials` carries the admin password, `api-secret-key`, `jwt-secret`, and `fernet-key`, referenced via `apiSecretKeySecretName` / `jwtSecretName` / `fernetKeySecretName`. This keeps every key stable across Flux reconciles and inside CLI ownership; chart-managed alternatives either rotate per render (api-secret, JWT) or live in an untracked hook Secret (fernet).
- **Subpath serving needs no route changes.** `config.api.base_url`'s path (`/airflow`) becomes the FastAPI `root_path`; Starlette strips the prefix when present and matches without it otherwise, so the gateway forwards `/airflow`-prefixed requests unrewritten, the chart's un-prefixed health probes keep working, and the chart derives `execution_api_server_url` from the same path. Because the UI router's basename is that path, the Airflow URL keeps the `/airflow` suffix even on a dedicated host (dns mode).
- **Service identity.** Routes and ReferenceGrants target `airflow-api-server`. The FAB auth manager (chart default) keeps the `createUserJob` admin-user flow valid; the admin identity lives solely in the job's args.
- **Schema lifecycle.** The chart's migrate job (`airflow db migrate`, `useHelmHooks: false` so Flux runs it) initializes and upgrades the metadata schema.

Deliberately not carried over from cimpl-stack: the engine switch and all its residue, `airflow3-*` resource naming, Istio sidecar opt-outs (this stack's `platform` namespace has no sidecar injection), and runtime `_PIP_ADDITIONAL_REQUIREMENTS` DAG loading (this stack ships no DAGs).

### Workflow service

`OSDU_AIRFLOW_URL` points at `airflow-api-server` (the previous value referenced a nonexistent `airflow-web`). The Azure provider's Airflow 3 engine selection — `OSDU_AIRFLOW_VERSION=airflow3` plus `OSDU_AIRFLOW_AIRFLOW3_URL/USERNAME/PASSWORD` (the latter requiring the admin credential mirrored into the `osdu` namespace) — is not wired yet: those bindings exist only on an unmerged upstream branch, and this stack resolves workflow images from `master`. Wire them when upstream merges.

### Consequences

- Good, because the stack tracks the current Airflow major with one component and zero transition machinery.
- Good, because signing-key lifecycle is correct under Flux: nothing rotates on reconcile, and every key is CLI-owned.
- Bad, because workflow→Airflow API calls cannot work until the community workflow service ships its Airflow 3 client — accepted since that integration is inert today.
