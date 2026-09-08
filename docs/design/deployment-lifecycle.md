# Deployment lifecycle

`spi up` provisions Azure resources, bootstraps Kubernetes, and starts Flux.
It can return before the OSDU APIs are ready. Use `spi status --watch` to follow
the remaining rollout.

Before returning, the CLI verifies the requested Git revision and suspends Git fetching. Flux continues
reconciling the cached revision; suspension does not stop workloads or cancel
the rollout.

## From invocation to CLI exit

The commands below create billable Azure resources in the active subscription:

```bash
spi check
spi up --env dev1
```

The CLI defaults to `core`, region `westus3`, partition `opendes`, and ingress
mode `azure`. It checks prerequisites, resolves the deployer identity and names,
and prepares infrastructure. For `core`, an explicit `--refresh-images` resolves
images before provisioning; otherwise bootstrap reads the existing lock and
resolves only when no lock exists. `minimal` and `bare` skip image resolution. The main orchestration is in
`deploy_azure()` in `src/spi/deploy.py`.

| Stage | Work performed | State available afterward |
|---|---|---|
| Subscription preflight | Resolve the system pool SKU's usable zones and ephemeral-disk capability | Invalid or restricted zone sets rejected before resource-group creation |
| Resource group | Verify Azure login, create or reuse the group, persist the naming suffix | Stable resource names for retries |
| AKS | Deploy `infra/aks.bicep`, obtain kubeconfig, enable Istio CNI chaining, grant the deployer cluster access | An accessible cluster and OIDC issuer |
| Azure PaaS | Recover a matching soft-deleted Key Vault if needed, then deploy `infra/main.bicep` | Data services, identities, role assignments, Azure-derived Key Vault values |
| Kubernetes bootstrap | Create namespaces, seed Secrets, StorageClasses, Gateway API CRDs, ServiceAccounts, ConfigMaps including `spi-cluster-config`, the trusted-repository projection, and Istio policies | Inputs in `osdu-flux`, `platform`, and `osdu` |
| Flux activation | Deploy `infra/flux.bicep` with the repository, branch or tag, profile, and ingress paths | Source fetching and workload reconciliation begin |
| Runtime Key Vault values | Write middleware passwords from the seed and derived endpoints | Service configuration available in Key Vault |
| Git-source finalization | Wait for the source, resume and reconcile it, verify the requested artifact revision, then suspend it and write the deploy record | Verified Git revision recorded; new Git revisions no longer fetched |

The runtime Key Vault writes **do not wait for Elasticsearch or Redis**.
Passwords were generated during bootstrap and are already available. The CLI
may instead wait for the deployer's Key Vault role assignment to propagate.

Flux runs concurrently with those final CLI stages. There is no single moment
when the CLI stops all work and Flux starts all work.

Source finalization waits up to ten minutes for the GitRepository to appear,
then runs a source reconcile with a ten-minute timeout. A missing, unready, or
wrong-ref artifact fails deployment. Failure during reconciliation or
verification attempts to suspend the source again before propagating the error.
Inspect the source if deployment stops at that stage:

```bash
kubectl get gitrepository osdu-spi-stack-system -n osdu-flux -o yaml
```

Check `spec.suspend`, `status.artifact.revision`, and Ready. The deploy record
stores the verified commit and CLI version. A new tag deployment starts with
`maintenance: true`; an existing record preserves its maintenance value.
Lifecycle workflows explicitly set maintenance before mutating a standing
environment and clear it only after readiness and probes pass. Follow the
[environment lifecycle](environment-lifecycle.md) for that workflow contract.

## What Flux finishes

The two root Kustomizations, `stack` and `ingress`, create child Kustomizations.
Their `dependsOn` entries form a dependency graph rather than one serial queue.

Operators and NodePools precede middleware. Elasticsearch, Redis, and
trust-manager must be ready before the CA-bundle bootstrap layer. Core OSDU
services follow that layer, then partition and entitlements initialization,
schema loading, and reference services. Legal-tag seeding follows initialization
on a separate, non-gating branch. Airflow follows PostgreSQL on a
separate branch. Ingress has its own certificate and route dependencies.

That sequence describes `core`. `minimal` stops before OSDU services; `bare`
activates empty workload trees. Neither profile skips Azure PaaS provisioning
or CLI credential bootstrap. The [profile reference](../architecture.md#stack-profiles)
defines the boundaries.

The [Flux guide](flux-reconciliation.md#dependency-ordering) owns the dependency
reference. Layer numbers are grouping labels; they are not global barriers.

## Timing and readiness

Existing observations recorded in the
[smoke workflow](../../.github/workflows/smoke.yml) put fresh provisioning in
`centralus` at roughly 45-50 minutes, including about 30 minutes for AKS and
10-15 minutes for the Flux extension. These are planning estimates from prior
runs, not measurements of the current release or guarantees for other regions.
The CLI's default region is `westus3`, not `centralus`.

Application readiness can take longer than CLI provisioning. Reconciliation
overlaps the end of provisioning, so adding separate phase estimates does not
give a reliable total. Image pulls, quota, node availability, certificate
issuance, and initialization Jobs all affect the remaining wait. CI allows
60 minutes for provisioning and a separate 230-minute Flux-readiness wait
inside a 270-minute verify job. These are ceilings, not expected durations.
The core schema-load Job alone has a 150-minute deadline, with a 155-minute
Kustomization timeout that includes cold-cluster scheduling and image pulls.

After `spi up` returns:

```bash
spi status --watch
```

Distinguish these milestones:

| Signal | What it establishes |
|---|---|
| CLI exits successfully | The orchestration completed without a fatal error |
| Git source has an artifact | Flux has manifests to reconcile |
| Kustomizations and HelmReleases are Ready | Declared resources passed their configured health checks |
| Initialization Jobs are Complete | Partition/entitlements bootstrap and schema loading finished |
| An authenticated API request succeeds | The particular request path is usable |

A pod in `Running` phase is not necessarily ready. Completed Jobs should not
be expected to remain Running. Use `spi info` to discover endpoints, then
exercise the API needed for your test.

## Retrying and previewing

If provisioning fails, inspect the failed ARM deployment or Kubernetes
condition before retrying. Re-running `spi up` reuses the resource group's
naming suffix and existing credential seed, but it is not a resume-from-step
operation: it resubmits infrastructure and reapplies bootstrap configuration.
Use the same partition list, location, repository/branch, and ingress settings.

For `core`, a retry preserves an existing image lock, including service pins.
Pass `--refresh-images` to resolve fresh canonical images while retaining active
pins. `--no-refresh-images` fails if the cluster has no image-lock ConfigMap;
it cannot bootstrap a new core deployment on its own.

`spi up --env dev1 --dry-run` previews the AKS and PaaS templates. It still
creates or updates the resource group and naming tag. It skips Key Vault
recovery, Kubernetes bootstrap, Flux activation, and runtime secret writes.
The [Bicep guide](bicep-architecture.md#previewing-changes) explains the
preview's missing OIDC-dependent resources.

Changing a profile is not a non-destructive retry. Moving to `bare` removes
middleware; Redis's PVC retention policy deletes volumes when its StatefulSets
are removed or scaled down.

## Steady state and teardown

After deployment, inspect changes before fetching another Git revision.
`spi reconcile --resume` enables polling, and `--suspend` disables it again.
`--refresh-images` is a separate image update and can roll services even when
Git fetching is suspended. See the [Flux guide](flux-reconciliation.md).

**Teardown deletes the environment's data and compute. Managed identities,
the resource group, and its tags survive ordinary `spi down`.**

```bash
spi down --env dev1
az resource list --resource-group spi-stack-dev1 --output table
```

Success means a fresh inventory contains only managed identities and the AKS
managed nodes group is gone. The command waits up to 45 minutes; an incomplete
delete exits nonzero with the remaining resources. Independent resources delete
concurrently, then subnet NAT associations, NAT gateway, public IP, and VNet
are removed in dependency order. An unhandled resource type blocks deletion;
authorization failures and resource locks stop the run. Re-run `spi down` to
continue from a partial inventory.

Retaining identities preserves client IDs and external grants. The naming suffix
also survives, so the next `spi up` reuses resource names and recovers the
matching soft-deleted Key Vault. Kubernetes seed Secrets are lost with the
cluster; a rebuild generates new middleware passwords.

To delete the resource group and its identities as well:

```bash
spi down --env dev1 --purge
az group exists --name spi-stack-dev1
```

Purge discovers the identities' external role assignments first. It removes and
confirms the stack-owned ExternalDNS zone grant; unknown grants, discovery
failures, or failed removals stop purge with the group intact. The external DNS
zone is never a deletion target. `false` from `az group exists` confirms the
group is gone. Purge waits for completion within the same 45-minute deadline.

Kubeconfig cleanup follows confirmed cluster deletion and checks the recorded
API-server FQDN before removing entries, so a same-named cluster in another
subscription keeps its context. Missing `kubectl` or an unverifiable server
skips cleanup. With a multi-file `KUBECONFIG`, the CLI re-reads surviving
contexts before removing unreferenced cluster or user entries.

[ADR-034](../decisions/034-deploy-identity-survives-down.md) owns retention and
purge boundaries. Scheduled reset and teardown workflows remain unbuilt; their
planned sequencing is in [environment lifecycle](environment-lifecycle.md).

## Decisions and implementation

- [ADR-008](../decisions/008-bicep-for-azure-provisioning.md): Bicep provisioning.
- [ADR-014](../decisions/014-suspend-gitops-after-deploy.md): default Git-source suspension.
- [CLI](../../src/spi/cli.py): options, prerequisite checks, naming suffix, completion output.
- [Deployment orchestration](../../src/spi/deploy.py): bootstrap, Key Vault writes, source suspension.
- [Teardown](../../src/spi/teardown.py): deletion inventory, retention, external-grant cleanup, and deadlines.
- [Deploy record](../../src/spi/deploy_record.py): verified revision and maintenance state.
- [Azure provisioning](../../src/spi/azure_infra.py): AKS, PaaS, recovery, and preview ordering.
- [Core stack](../../software/stacks/osdu/profiles/core/stack.yaml): workload dependencies.
