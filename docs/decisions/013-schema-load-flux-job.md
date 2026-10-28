# ADR-013: Schema Load via a Flux-Managed Job

## Context

After Flux reconciles the OSDU core services, the schema-service Pod is `Ready` but its Cosmos DB container is empty. Any downstream record call that references a `kind` fails with `schema not found`. Loading the ~1,386 shared schemas that the OSDU community publishes is a mechanical, one-shot operation that has to happen exactly once per environment on fresh deploy.

Running it as a CLI tail reopens the problems ADR-011 already resolved for CA distribution: hidden dependency on the local Python, no re-run without the CLI, no visibility in `flux get kustomizations`.

## Decision

Load schemas with a Flux-managed Kubernetes Job, reusing the existing `workload-identity-sa` in the `osdu` namespace (ADR-005). The Job runs the loader image the schema service's canonical repository builds beside the service (`<package>-load`, for example `ghcr.io/azure/osdu-spi-schema-load`) against the in-cluster schema-service, POSTing schemas one by one.

Shape:

- New Kustomization `spi-osdu-schema-load` at `software/stacks/osdu/schema-load/`, wired into the core profile as Layer 5b (after `spi-osdu-init`, before `spi-osdu-reference`) per ADR-007.
- Single `Job` with `workload-identity-sa`, the workload-identity pod label, and the `SCHEMA_URL` and `DATA_PARTITION` values the loader's own entrypoint reads.
- The loader presents the OSDU identity, and the schema service authorizes its system-schema writes against `service.system-schema-service.editors` in the primary partition, which `entitlements-init` creates with that identity as OWNER (ADR-015).
- Kustomization `healthChecks` target the Job's `Complete` condition so `spi status` surfaces the Job alongside every other Flux resource.
- The loader image is resolved through `osdu-image-lock` from the same commit selected for schema-service, and resolution fails when that repository published no loader for the commit.

Rejected:
- **Authorizing the loader by its token issuer alone.** Needs no entitlements group, but the mesh accepts the management audience (ADR-016), so every service principal and managed identity in the tenant would pass the same check.
- **A long-running Deployment that loads once then sleeps.** Works, but lies to Kubernetes about what the workload is.
- **A Terraform `null_resource` with `local-exec`.** Runs at apply time with no cluster-side resource to manage, but re-introduces the CLI-tail problem inside Terraform instead of Python.
- **A home-grown Python loader that fetches schemas from Git.** Duplicates the schema repository's CI, which already publishes a loader at the same commit as the service.

## Consequences

- Fresh deploy reaches a usable schema-service with no CLI post-step.
- Schema loader upgrades move with the service image via the live image lock.
- Manual re-run is `spi load --dataset schemas --force` (ADR-040), which runs `kubectl delete job schema-load -n osdu` and then `flux reconcile kustomization spi-osdu-schema-load`. Flux re-applies the Job.
- The loader digest depends on GHCR retention of the schema repository's `sha-*` versions; the weekday canonical refresh keeps a running environment inside that window (ADR-033).
- Only the schema-service is seeded here; partition initialization, entitlements root-group provisioning, and the default legal tag run as a separate Flux-managed chart (ADR-015). Reference data loads through `spi load` (ADR-040).
