# ADR-013: Schema Load via a Flux-Managed Job

**Status**: Accepted

## Context

After Flux reconciles the OSDU core services, the schema-service Pod is `Ready` but its Cosmos DB container is empty. Any downstream record call that references a `kind` fails with `schema not found`. Loading the ~1,386 shared schemas that the OSDU community publishes is a mechanical, one-shot operation that has to happen exactly once per environment on fresh deploy.

Running it as a CLI tail reopens the problems ADR-011 already resolved for CA distribution: hidden dependency on the local Python, no re-run without the CLI, no visibility in `flux get kustomizations`.

## Decision

Load schemas with a Flux-managed Kubernetes Job, reusing the existing `workload-identity-sa` in the `osdu` namespace (ADR-005). The Job runs the OSDU community `schema-service-schema-load` image against the in-cluster schema-service, POSTing schemas one by one.

Shape:

- New Kustomization `spi-osdu-schema-load` at `software/stacks/osdu/schema-load/`, wired into the core profile as Layer 5b (after `spi-osdu-init`, before `spi-osdu-reference`) per ADR-007.
- Single `Job` with `workload-identity-sa`, the workload-identity pod label, and a mounted ConfigMap that provides `Token.py` and `bootstrap.sh` at the paths the loader image's entrypoint expects.
- Kustomization `healthChecks` target the Job's `Complete` condition so `spi status` surfaces the Job alongside every other Flux resource.
- The loader image tag is pinned to the same SHA as `schema.yaml`. `scripts/resolve-image-tags.py --update` advances both tags together.
- `bootstrap.sh` post-processes the loader's exit code: "already exists" failures are not fatal, so re-runs are idempotent.

Rejected:
- **A long-running Deployment that loads once then sleeps.** Works, but lies to Kubernetes about what the workload is.
- **A `null_resource + local-exec` in Terraform.** The approach in the sister `osdu-spi-infra` repo; re-introduces the CLI-tail problem inside Terraform instead of Python.
- **A home-grown Python loader that fetches schemas from Git.** Duplicates community CI, and the community image already publishes at the same SHA as the service.

## Consequences

- Fresh deploy reaches a usable schema-service with no CLI post-step.
- Schema loader upgrades move with the service image via the existing tag-resolver.
- Manual re-run is `kubectl delete job schema-load -n osdu` followed by `flux reconcile kustomization spi-osdu-schema-load --with-source`. Flux re-applies the Job.
- The loader tag depends on OSDU community registry retention. Mirroring the image to the SPI ACR (already provisioned) is an available follow-up if retention becomes a problem.
- Only the schema-service is seeded. Reference data, legal tags, entitlements root groups, and partition initialization are out of scope and remain future work.
