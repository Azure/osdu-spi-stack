# CI smoke pipeline

The smoke workflow creates a real Azure environment, waits for Flux readiness,
and requests resource-group deletion. It runs on a daily schedule or manual
dispatch, not on pull requests. Its default profile is `bare`, so a scheduled
pass establishes infrastructure and empty-GitOps readiness, not middleware
or OSDU readiness. Each job logs into Azure independently so
cleanup does not depend on the provisioning job's credential lifetime.

This is a deployment smoke test, not an end-to-end OSDU acceptance test.

## Job boundaries

| Job | Responsibility | Timeout |
|---|---|---|
| `provision` | Resolve an environment name, tag the resource group, run `spi up` | 60 minutes |
| `verify` | Obtain cluster access, wait for Kustomizations, run non-bare ingress probes, capture diagnostics on failure | 270 minutes; Flux wait allows 230 minutes |
| `teardown` | Request asynchronous resource-group deletion | 15 minutes |

`provision` publishes the resource group name as a job output. The other jobs
use that output rather than reconstructing the name. The current workflow
sanitizes the requested suffix and keeps its final six characters to fit Azure
Storage naming limits.

`verify` follows successful provisioning. `teardown` uses
`always() && needs.provision.result != 'skipped'` so it is eligible to run after
upstream failure or cancellation. It skips deletion when no resource-group
output is available.

The "gateway reachable" step requires a ready endpoint on the managed ingress
Service; on failure it lists Services and pods for diagnostics. A separate HTTPS probe
requests the first HTTPS listener's hostname and requires an HTTP response,
including a 4xx or 5xx response, after a successful TLS connection. It retries
up to 20 times and is skipped when no HTTPS listener exists.

Both ingress probes are skipped for `bare`. A green non-bare run establishes
the probed TLS path, not authenticated API acceptance. The long verify timeout
accommodates core schema loading; it is not an expected duration for `bare`.

## Authentication over a long deployment

GitHub's OIDC assertion is short-lived. Azure access tokens obtained with that
assertion can last longer, but a later token exchange or refresh can fail once
the original assertion has expired.

Each job starts with `azure/login`. Provisioning also pre-caches tokens for
ARM, Microsoft Graph, AKS, and Key Vault while the assertion is fresh, and
resolves the deployer object ID before the long AKS deployment. The verify job
pre-caches its ARM and AKS tokens.

Separate jobs give teardown a fresh login attempt; they do not guarantee
authentication or deletion will succeed. Provisioning estimates are maintained
in [deployment timing](deployment-lifecycle.md#timing-and-readiness), not as
another independent set of numbers here.

## Cleanup and its limits

The teardown command uses `az group delete --no-wait`. It requests deletion
rather than waiting for every resource to disappear. The current workflow
also appends `|| true`, so a green teardown job is not evidence that Azure
accepted the request.

Full-workflow cancellation can interrupt cleanup. The
[orphan sweeper](../../.github/workflows/sweeper.yml) runs independently at
04:00 UTC, before the smoke schedule at 08:00 UTC. By default, a group is
eligible only if all three conditions hold:

- Its name starts with `spi-stack-ci-`.
- It has `spi-ci-sweep-eligible=true`.
- Its parseable `spi-created-utc` tag is at least three hours old.

Provisioning writes those tags before `spi up`, so partially provisioned
environments can still be selected. The age threshold is an eligibility rule,
not a promise to clean up within three hours: the scheduled sweep is daily.
Missing tags, authentication failures, and deletion failures still require
operator attention.

The standing shared environment is outside the sweeper's selection: its name
and tags do not meet those criteria. Its upgrade and refresh workflows are
separate from smoke; reset and teardown workflows remain unbuilt. See
[environment lifecycle](environment-lifecycle.md).

## Running and inspecting smoke

**Dispatching smoke creates billable Azure resources.**

```bash
gh workflow run smoke.yml --ref main
gh run list --workflow smoke.yml --limit 5
gh run watch <run-id>
```

For a named run, pass `-f env_suffix=trial1`. To exercise middleware rather
than the default empty workload trees, choose `minimal`:

```bash
gh workflow run smoke.yml --ref main -f profile=minimal
```

Choose `core` to include OSDU services and initialization. `full` is not a
supported profile.

To inspect a failed run:

```bash
gh run view <run-id> --log --job <job-id>
gh run download <run-id> --name smoke-diagnostics-<run-id>
```

Diagnostics artifacts are produced by verify-failure handling. A provisioning
failure may have only job logs, and cancellation can prevent artifact upload.
Review diagnostic content before sharing it.

To preview sweeper candidates without deleting them:

```bash
gh workflow run sweeper.yml --ref main -f dry_run=true
```

After teardown, confirm the resolved resource group is gone:

```bash
az group exists --name <resource-group-from-run>
```

`false` confirms deletion. If it remains, inspect Azure's deletion state and
the cleanup logs rather than assuming the next sweep will resolve the failure.

## Decisions and implementation

- [CI setup](../CI_SETUP.md): repository environment, identity, and permissions.
- [Deployment lifecycle](deployment-lifecycle.md): the deployment exercised by smoke.
- [Smoke workflow](../../.github/workflows/smoke.yml): job conditions, token caching, and timeouts.
- [Sweeper workflow](../../.github/workflows/sweeper.yml) and [script](../../scripts/sweep_orphan_rgs.sh): schedule and selection rules.
- [Readiness wait](../../scripts/wait_for_flux_ready.sh) and [diagnostic capture](../../scripts/capture_diagnostics.sh): verification and failure artifacts.
