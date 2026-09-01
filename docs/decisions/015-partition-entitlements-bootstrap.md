# ADR-015: Partition, Entitlements, and Legal Bootstrap via a Flux Helm Chart

## Context

OSDU's Azure provider needs three pieces of state in place before any record, schema, or entitlements operation can succeed against a fresh cluster:

1. **A partition record.** Every service resolves partition-specific backend config (Cosmos endpoint, Service Bus namespace, storage account, elastic credentials) by calling `partition-service`. Without a record, those lookups return nothing.
2. **Entitlements root groups.** Authorization calls look up the caller's appid in `users.data.root@{partition}...` and siblings. With an empty entitlements DB every call returns 401, regardless of which identity the caller holds.
3. **A default legal tag.** Every stored record carries a legal tag, and legal validates a tag's `countryOfOrigin` against a country-of-origin catalog blob it reads from its configuration container. Without a compliant tag, ingestion and the fork acceptance suites have nothing to reference.

All three are mechanical, one-shot operations that have to happen exactly once per partition per environment. The same structural problem ADR-013 solved for schema-load applies here, scaled to an additional admin concern: per-partition identity provisioning.

## Decision

Bootstrap partitions, entitlements, and legal tags with a Flux-managed Helm chart that renders three one-shot Jobs per partition:

- `partition-init-{partition}` POSTs the partition record to `partition-service`.
- `entitlements-init-{partition}` POSTs `tenant-provisioning` so entitlements creates root groups with the caller's appid as OWNER.
- `legal-init-{partition}` stages the country-of-origin catalog into the partition's `legal-service-azure-configuration` container and POSTs the `{partition}-demo-legaltag` default tag.

Shape:

- Chart `software/charts/osdu-spi-init/` with `partition-record.yaml`, `scripts.yaml`, `partition-init.yaml`, `entitlements-init.yaml`, `legal-init.yaml` templates. The three Job templates all loop over `.Values.partitions` so a multi-partition deploy renders one Job per partition with no extra manifests.
- All three Jobs share the `workload-identity-sa` ServiceAccount (ADR-005) and the Token.py MSAL pattern already proven in `schema-load/script.yaml`. The caller's appid is the OSDU UAMI client id, which is what `partition-azure` stores as `TenantInfo.service-account` and what entitlements-azure authorizes as bootstrap admin on an empty DB.
- Partition list is injected via a `spi-init-values` ConfigMap the CLI writes (driven by `--partition` flags). The HelmRelease consumes it with `valuesFrom`, so adding a partition is a CLI argument change, not a Git edit.
- Partition record values use **bare** Key Vault secret suffixes (`cosmos-endpoint`, `sb-namespace`, etc.), not partition-prefixed names. `partition-azure` auto-prefixes the partition id onto every `sensitive: true` value at write time, so embedding the prefix in the chart double-prefixes the stored value and breaks downstream service lookups.
- New Kustomization `spi-osdu-init` at `software/stacks/osdu/init/`, wired into the core profile as Layer 5a (after `spi-osdu-services`, before `spi-osdu-schema-load`) per ADR-007. `schema-load` depends on `spi-osdu-init` so the schema POSTs see a tenant that is already provisioned.
- The Jobs start in parallel, so each gates on the state it needs rather than on Job ordering: `entitlements-init` waits for the partition record, and `legal-init` waits for both the partition record and an authenticated `GET /legaltags` returning 200, which succeeds only once tenant provisioning has created the groups legal authorizes against.
- `legal-init` resolves the partition's blob endpoint from the `{partition}-storage-account-blob-endpoint` Key Vault secret and PUTs the catalog into the container `partition.bicep` creates. legal-azure resolves that container from its own `AZURE_STORAGE_CONTAINER_NAME` env plus the partition record, so there is no bucket property to PATCH onto the partition and no Azure equivalent of that upstream step.
- `legal-init` prints one typed `legal-init outcome: <code>` line on every exit, so a failure is diagnosable from the Job log without re-running it. Seeding a legal tag is not a readiness gate: helm-controller waits for every Job in a release, so `legal-init` renders only in the separate `osdu-spi-legal` HelmRelease (same chart, `legalEnabled`/`coreEnabled` toggles) behind the non-gating `spi-osdu-legal` Kustomization at `software/stacks/osdu/legal/` (Layer 5c, `spi-stack.gating: "false"`, ADR-030), and `ready` and `seeded` stay separate signals.
- Idempotence: 201 and 409 from partition-service count as success; 200 and 409 from entitlements-tenant-provisioning count as success; the catalog upload overwrites, and 201 and 409 from legal-legaltags count as success. No `ttlSecondsAfterFinished` (same rationale as ADR-013: Flux would re-create an auto-deleted Job and turn one-shot into periodic).

Rejected:

- **Imperative CLI step.** Re-opens the problems ADR-011 and ADR-013 closed: hidden CLI dependency, no re-run from the cluster, invisible to `flux get kustomizations`.
- **Per-partition Flux Kustomization stamping.** One Kustomization per partition duplicates every wiring decision above. A single Kustomization holding a chart with a per-partition loop is the same outcome with less YAML.
- **Partition-prefixed values in the chart's `partition.json`.** Double-prefixes every stored value and surfaces as "Invalid data partition id" the first time a service dereferences the record. The bare-key pattern matches what `partition-azure` expects.

## Consequences

- Fresh deploy reaches a schema-loaded cluster with no CLI post-step and no manual entitlements provisioning.
- Multi-partition enables via `spi up --env dev1 --partition p1 --partition p2`; the chart renders one partition-init, one entitlements-init, and one legal-init Job per partition with no manifest changes.
- Four per-partition Key Vault secrets are declared in `partition.bicep` (`{p}-storage-account-blob-endpoint`, and `"DISABLED"` placeholders for `{p}-cosmos-connection`, `{p}-sb-connection`, `{p}-storage-account-key`) so the partition record resolves under Workload Identity without exposing real connection strings (ADR-023). The `{p}-sb-connection` placeholder leaves indexer-queue's community image unable to authenticate; the gap is covered in ADR-005.
- Manual re-run is `kubectl delete job -n osdu partition-init-{p} entitlements-init-{p}` followed by `flux reconcile kustomization spi-osdu-init`. Legal seeding re-runs on its own Kustomization: `kubectl delete job -n osdu legal-init-{p}` followed by `flux reconcile kustomization spi-osdu-legal`. 409 responses on re-run are treated as success.
- The default tag name is published as `partitions[].legal_tag` in `spi info --json` (ADR-030), so acceptance suites reference the tag the cluster created instead of reconstructing the name.
- Reference data and adding human users to `users.datalake.ops` remain out of scope; add later if the stack needs user-facing UI access or record ingestion.
