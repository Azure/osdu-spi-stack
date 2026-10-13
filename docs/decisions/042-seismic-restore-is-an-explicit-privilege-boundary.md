# ADR-042: Seismic Restore Is an Explicit Privilege Boundary

## Context

The Seismic API writes dataset metadata and blobs through the ordinary OSDU
data plane. RestoreRunner also calls the Azure Resource Manager
`restoreBlobRanges` operation, undeletes containers, rewrites live Cosmos
metadata from archive snapshots, and consumes restore queue messages. Giving
those operations to `workload-identity-sa` would extend the privilege to every
OSDU service under ADR-005.

Blob point-in-time restore depends on account settings that change storage
retention and cost: blob versioning, change feed, blob soft delete, container
soft delete, and a restore policy. Enabling them for every deployment while
the worker is absent creates cost without a usable restore path. Starting the
worker without them accepts operations that cannot succeed.

## Decision

Seismic restore is one opt-in capability controlled by
`enableSeismicRestore`, default `false`. The flag owns the Azure prerequisites,
the dedicated identity, the API feature flag, and the worker replica count as
one unit.

- **RestoreRunner has its own identity.** Bicep creates
  `<cluster>-seismic-restore`, federated only with
  `system:serviceaccount:osdu:seismic-restore-sa`. The RestoreRunner
  HelmRelease uses that ServiceAccount. The Seismic API remains on
  `workload-identity-sa`.
- **The runner receives scoped data-plane roles.** Key Vault Secrets User is
  scoped to the environment vault, Storage Blob Data Contributor and Storage
  Queue Data Contributor are scoped to each partition storage account, and
  Cosmos DB Built-in Data Contributor is scoped to each partition Cosmos
  account. The API identity receives Storage Queue Data Contributor on the
  same accounts so it can enqueue restore work; it receives no restore
  management-plane action.
- **One custom role grants the management operation.** The subscription-level
  role definition `Seismic Blob Restore Operator`, id
  `b66df1f4-588e-4a1f-abce-d26fa97b6668`, contains only
  `Microsoft.Storage/storageAccounts/read` and
  `Microsoft.Storage/storageAccounts/restoreBlobRanges/action`. Its assignment
  to the RestoreRunner identity is scoped to each partition storage account.
- **Storage recovery settings are gated together.** When restore is enabled,
  each partition storage account enables blob versioning, change feed with
  30-day retention, blob soft delete with 31-day retention, container soft
  delete with 31-day retention, and point-in-time restore with 30-day
  retention. When disabled, the template omits those policies rather than
  writing disabled policy objects.
- **Restore state is gated with the worker.** Enabling restore creates
  `sdms-queue-restore`, plus the `RestoreOperationStatus` and
  `ArchiveDatasetMetadata` containers in `sdms-db`. It sets
  `FEATURE_FLAG_ENABLE_RESTORE=true`, supplies the restore lock and dequeue
  limits, and runs one RestoreRunner replica. Disabling it keeps the API flag
  false and the worker at zero replicas.
- **The CLI checks the permission expansion before Azure changes.** ADR-035's
  deployer preflight includes custom role definition write and delete, the
  fixed custom role id is part of `STACK_ROLES`, and the documented
  role-assignment condition admits that id. A deployer with the old condition
  is refused before Bicep starts.
- **Deployment validates the whole gate.** Tests require the identity,
  federation, roles, queues, Cosmos containers, policies, API feature flag, and
  worker replica to agree for both flag values. No individual restore
  prerequisite has a separate public toggle.

Rejected: add the restore action and data-plane roles to the shared OSDU
identity. This requires fewer identities and federations, but every service
could initiate account-level blob recovery and rewrite Seismic metadata.

Rejected: grant Contributor on partition storage accounts to RestoreRunner.
It includes the required restore action, but also permits storage account
configuration, key, networking, and deletion operations the worker does not
call.

Rejected: enable storage recovery policies by default while leaving the API
and worker disabled. A later enablement is faster, but every environment pays
the retention cost before it exposes restore.

Rejected: expose separate flags for storage policies, API routing, and worker
replicas. They permit states that accept unrecoverable work or retain data
without a consumer.

## Consequences

- RestoreRunner cannot use the shared API identity if its ServiceAccount or
  federation is misconfigured; token exchange fails instead of widening
  access.
- The Seismic API retains queue read and delete access when restore is
  enabled. Its queue client calls `createIfNotExists` before sending, so
  Storage Queue Data Contributor is required instead of the narrower Storage
  Queue Data Message Sender role.
- Blob restore is account-scoped in Azure even though the request carries one
  blob range. The custom role cannot narrow the action to a container or
  prefix.
- The 30-day recovery window costs change-feed, version, and soft-deleted data
  retention. Turning the flag off on a later deployment removes the policies;
  Azure controls expiry of data retained under the earlier policy.
- A change to a retention value or required restore resource is one
  deployment-model change under this record, not a service-manifest-only
  change.
