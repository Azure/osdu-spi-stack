# ADR-044: Core Profile DAGs

## Context

Workflow reaches Airflow 3.2.2 through its `osdu-workflow` login, but the
middleware profile loads no DAGs or OSDU Python libraries. Its
KubernetesExecutor has no task pod template. A core environment therefore
cannot run a manifest ingestion. Issue [#392](https://github.com/Azure/osdu-spi-stack/issues/392)
and its *Core Profile DAGs* design define the missing execution path.

## Decision

Core loads the DAGs that perform ingestion or have no DDMS dependency. Two
families ship: `spi-smoke` (`Echo_DAG`, `airflow_monitoring`) and
`core-ingestion` (`Osdu_ingest`, `manifest_ingestion`). The ingestion files
come from ingestion-dags at the full v0.30.0 commit. By-reference, CSV,
WITSML and EDS wait for their missing services or worker images; DDMS
families follow their service groups. No declaration or stack-file `dags:`
key is introduced.

- **One Airflow owner.** The core profile patches `spi-airflow`'s path to
  `software/stacks/osdu/airflow`, which includes the middleware component.
  The HelmRelease name and Flux inventory owner stay unchanged (ADR-025).
  Bare and minimal do not receive the overlay.
- **Local tasks.** Core uses LocalExecutor with parallelism 2. The scheduler
  uses the existing `platform/workload-identity-sa` and Workload Identity
  pod label, with `allowPodLaunching: false`. The DAG processor and API
  server retain chart-created ServiceAccounts without Workload Identity.
  `workers.persistence.enabled: false` keeps the scheduler a Deployment.
  Tasks share its CPU, memory and Azure authority. Logs stay on that pod;
  `airflow.utils.net.get_host_ip_address` makes them addressable by pod IP.
- **Core sizing.** Scheduler requests are 1 CPU and 2Gi, with 2 CPU and 4Gi
  limits. DAG processor requests are 250m and 512Mi, with 1 CPU and 2Gi
  limits. API server and triggerer values do not change. Non-empty
  `spi-sizing` placeholders carry these defaults. `sizing.py` models core,
  the only profile accepting sizing inputs (ADR-043); minimal retains its
  own defaults.
- **Reviewed inputs.** The catalog, requirements lock, loader and SDK smoke
  files live separately under `software/stacks/osdu/dags`. A local-config
  generator and kustomize replacements embed them in chart
  `extraConfigMaps`, not an independently applied ConfigMap. The chart's
  checksum rolls the scheduler and DAG processor when an input changes.
  Embedded files may not contain a Flux substitution opener.
- **Fail-closed delivery.** A loader init container on both pods validates
  unique family names and DAG IDs, full commits, required remote SHA-256
  hashes and family-relative paths before staging files. Invalid archive
  members and escaping paths are rejected before writes. Fetch or hash
  failure names the file and exits 1, holding the pod in init.
- **Locked libraries.** `osdu-api`, `osdu-airflow`, `osdu-ingestion` and their
  dependencies install at container start with `--require-hashes` and
  `--only-binary :all:`, resolved against the image's Airflow 3.2.2 Python
  3.13 constraints. OSDU GitLab package registries are `--find-links`, not
  indexes. No extras are installed. Startup and liveness budgets allow the
  install; a failed install never starts a scheduler or parser.
- **System registration.** Non-gating layer 6
  `spi-osdu-dag-registration` depends on `spi-airflow` and `spi-osdu-init`,
  not members. Its finite-deadline Job uses the OSDU identity in `platform`,
  the pinned Airflow image and a catalog-hashed name, without a TTL.
  Through the `osdu-workflow` login it waits for exactly the declared DAG
  set and zero import errors before any registration or unpause.
  System POSTs omit `data-partition-id`. A 409 requires a matching system
  record read back in each partition. Workflow hides its system flag, so
  a partition's combined workflow list must also contain exactly one
  exact-name match equal to the read-back. A second match is a shadow.
  Conflicting DAG names fail with
  `registration_conflict`; partition shadows are reported, never deleted
  or adopted. Declared IDs are unpaused; removed IDs are paused, and records
  are never deleted. Airflow creates new DAGs paused.
  A fresh `last_parsed_time` for each declared ID prevents an old catalog's
  parse from satisfying a new registration Job.
  Workflow and Partition authorization can lag init while tenant caches
  warm. Their 403s and 401s after one token refresh retry within the shared
  900-second request budget, with status-change progress and a bounded
  response excerpt on failure. This does not reset the 1800-second Job
  deadline or make other client errors retryable.
- **Caller provenance.** Tasks use the OSDU identity's token with the run's
  `userId` in `on-behalf-of`. `AZURE_RESOURCE_ID` uses
  `spi-cluster-config/OSDU_TOKEN_AUDIENCE`, resolved as for `spi token`
  (ADR-030): an application override, or the management audience when
  `osdu-config/AAD_CLIENT_ID` names the managed identity. Copying that
  managed-identity ID verbatim would fail with AADSTS100040. The identity already owns
  `users.datalake.delegation`; the members Job adds the deploy identity to
  `users.datalake.impersonation`. People gain impersonation through
  `spi users` editor, admin or ops, not viewer. Downgrade or removal takes
  it away. Existing people must have their intended preset reapplied;
  upgrading does not enumerate and enroll people. No operator is enrolled
  by running `spi up`.
- **Acceptance.** `spi test dags` exercises Echo through Workflow, then
  submits a Manifest containing one new UnitOfMeasure reference record.
  Storage must return that record with its unique data marker and
  `createUser` equal to the caller. `--as deploy` is the default; `--as me`
  uses the signed-in person's own token. `--partition` selects any declared
  partition, defaulting to the primary. The CLI supplies neither `userId`
  nor `createUser`; Workflow and Storage must derive them. The command
  reuses the Deployable and Workflow/Storage target guards from ADR-036,
  but is stack-owned, not a paired descriptor suite. It neither grants a
  role nor seeds a schema or legal tag. Preflight refuses a legal tag
  Legal's `legaltags:validate` rejects and an ACL outside the caller's group
  domain, the two rejections Osdu_ingest records as skips while its run
  still succeeds. `--dry-run` performs read-only preflight without starting
  a Workflow run. Runs and the record remain
  as evidence. Registration readiness is not proof of ingestion.

Rejected: a stack-built Airflow image. It removes package resolution from
pod startup, but adds a repository, release and image lifecycle before the
catalog settles. Revisit when source availability or upstream Azure library
support makes runtime installation untenable.

Rejected: a `dags:` selection key. It permits subsets, but duplicates the
service-group model and makes a core capability optional.

Rejected: KubernetesExecutor. It isolates task resources, but repeats the
loader, dependency installation, Workload Identity and admission-compliant
pod configuration in each task pod.

Rejected: git-sync or Airflow DAG bundles. They support repository refresh,
but OSDU GitLab requires git-sync authentication and a clone has no per-file
hash. Blob-synced DAGs add a writer and another copy that can drift.

Rejected: per-partition registration or mesh caller projection. The former
duplicates a system workflow; the latter adds identity-filter behavior that
membership management must mirror. Explicit impersonation membership uses
the existing Entitlements contract.

Rejected: warn and continue on failed delivery. It keeps a pod available
but hides an incomplete catalog. `valuesFrom` avoids inline payloads, but
inline Helm values override it; kustomize patches do not (ADR-043).

## Consequences

- Pod startup depends on community GitLab and package sources. Rolling
  Deployments keep the old pod until the new one is ready, but rescheduling
  during an outage has no old pod to retain. Karpenter consolidation has
  the same exposure as node loss.
- Upgrading a v0.30.x core environment changes executor, resources and DAG
  delivery. Airflow components restart, four workflows are registered and
  unpaused, and the changed members values rerun the impersonation seed.
  The CLI must populate the OSDU audience in `spi-cluster-config` before
  reconciliation; old environments cannot substitute an empty audience.
- Existing explicit `spi-sizing` overrides remain in effect. Revalidate
  partial Airflow overrides against the larger defaults: an old limit
  override can be below a new default request. The upgrade does not erase
  an operator's resource choices.
- A catalog update restarts the scheduler. Tasks still running when the
  old pod terminates stop with it; logs are not durable. Parallelism bounds
  task processes, not memory or simultaneous workflow runs.
- A catalog review approves code execution with the shared OSDU identity.
  Hashes provide immutability, not publisher authenticity. A new file needs
  a reviewed commit and hash; a changed requirements lock is also code.
- Registration failures remain visible in `spi status` without blocking
  Deployable. Ingestion remains unavailable until registration and caller
  membership succeed. No workflow record is overwritten or deleted.
- The initial resource values are starting limits, not measured capacity.
  Fresh-deploy and v0.30.x upgrade validation must run ingestion as both
  identities in two partitions with `createUser` equal to the caller.
