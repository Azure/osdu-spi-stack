# Core DAG registration

`spi-osdu-dag-registration` is non-gating layer 6, after `spi-airflow` and
`spi-osdu-init`. It does not depend on members: the OSDU identity already
owns the groups created by tenant provisioning. `spi status` discovers its
row and layer from the Kustomization labels. Failure leaves Deployable
unchanged.

The Job runs as `platform/workload-identity-sa` in the pinned Airflow image,
with a 100m CPU request, 1800-second deadline and no TTL. Its catalog
ConfigMap comes from the DAG directory's local-config generator.
`name-reference.yaml` makes the Job name a reference to that ConfigMap,
so kustomize appends the same content hash to both names. A catalog change
creates a new Job and prunes the old one. Script changes alter a separate
ConfigMap reference in the pod template; `force: true` replaces that
immutable template without inventing a new catalog hash.

## Registration gates

The script obtains an Airflow JWT at `/airflow/auth/token` using
`osdu-workflow` and `platform/airflow-api-credentials#workflow-password`.
It exchanges the projected Workload Identity assertion at Entra's v1
endpoint for the management audience, matching the init chart's `auth.py`.
No password or token is logged.
Bearer headers are not forwarded on an HTTP redirect.

Airflow 3.2.2 returns `is_stale`, not Airflow 2's `is_active`.
`GET /api/v2/dags?exclude_stale=false` includes retired metadata so the Job
can pause it. The non-stale set must equal the catalog union, with zero
`/api/v2/importErrors` and each declared DAG parsed since the Job started.
The freshness check prevents an old parse of unchanged IDs from satisfying
a new catalog's Job. The same checks run again before any pause or unpause.

Each system POST omits `data-partition-id`. After a successful POST or 409,
the Job reads `/api/workflow/v1/workflow/{name}` in each partition from
Partition's `/api/partition/v1/partitions` response. An incorrect `dagName`
fails with `registration_conflict`; it is never overwritten or unpaused.

The Workflow API does not serialize `isSystemWorkflow`, so a matching
`dagName` alone cannot prove that the read returned the system record.
The Job also reads `/workflow?prefix=<name>` in each partition and requires
exactly one exact-name match equal to the single-record read-back.
Workflow's list concatenates partition and system records, without
deduplication. Since POST success or 409 proves a system record exists,
a second exact-name entry proves a partition shadow, even when both point
at the same DAG. The outcome is `registration_shadowed`; no record is
deleted or adopted.

Only after those checks do declared IDs become unpaused. IDs outside the
catalog are paused, including stale metadata for removed files. The Job
never deletes DAGs or Workflow records. A failure after some successful
POSTs can leave new records present but paused; a rerun verifies them
through the same 409 path.

## Retry and diagnosis

Requests use finite timeouts and bounded retries for network failures,
429, server errors and the known partition-cache-lag response. A 401
refreshes a bearer once. The 900-second script wait fits within the Job
deadline with pod-start allowance; Flux waits 35 minutes.

Read the current Job name from `kubectl get jobs -n platform -l
app.kubernetes.io/name=osdu-spi-dag-registration`, then its `register`
container log. The last line is either
`dag-registration outcome: registered 4, unpaused 4, paused 0` or a typed
failure with the affected IDs or partition. After fixing a failed input,
delete that Job and reconcile `spi-osdu-dag-registration` to rerun it.
There is no TTL because Flux would recreate a TTL-deleted Job.

Upstream contracts:

- [System API](https://community.opengroup.org/osdu/platform/data-flow/ingestion/ingestion-workflow/-/blob/v0.30.0/workflow-core/src/main/java/org/opengroup/osdu/workflow/api/WorkflowSystemManagerApi.java)
  has a POST and DELETE, no system read endpoint.
- [WorkflowMetadata](https://community.opengroup.org/osdu/platform/data-flow/ingestion/ingestion-workflow/-/blob/v0.30.0/workflow-core/src/main/java/org/opengroup/osdu/workflow/model/WorkflowMetadata.java)
  hides the system flag with `JsonIgnore`.
- [WorkflowManagerServiceImpl](https://community.opengroup.org/osdu/platform/data-flow/ingestion/ingestion-workflow/-/blob/v0.30.0/workflow-core/src/main/java/org/opengroup/osdu/workflow/service/WorkflowManagerServiceImpl.java)
  reads partition records first and concatenates the two lists.

[ADR-044](../../../../docs/decisions/044-core-profile-dags.md) owns the
registration boundary. Ready registration proves routing and activation,
not successful ingestion.
