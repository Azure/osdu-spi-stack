# Fork Deployment Loop

**What this explains.** The sequence a fork's CI runs to deploy a just-built
image into the shared environment and test against it: authenticate, connect,
pin, verify, test, restore, and the recovery path for pins nothing restored.
The durable rulings behind it are ADR-031 (the pin seam), ADR-032 (identity),
and ADR-033 (canonical source).

**Why it matters.** Eight fork repositories consume this contract from
workflow YAML they do not own (the `Azure/osdu-spi` template syncs it to
them). When a deploy misbehaves, the operator debugging it needs the exact
sequence, what each step asserts, and which recovery path applies.

**Status.** `spi service pin --image --ephemeral`, `verify`, and the
ownership-checked `reset` (with `--ephemeral --stale-only`) are implemented.
`spi onboard`, `spi service refresh`, the refresh workflow's backstop step,
and the fork-side jobs are ahead of the code (phases 1 and 4 of the roadmap
in [environment-lifecycle.md](environment-lifecycle.md)). Remove the marks as
they land.

## The sequence

Each job authenticates fresh (the OIDC JWT lives ~5 minutes; one
`azure/login` per job, the smoke pipeline's discipline) and installs the
`spi` wheel matching the environment's declared `stackVersion`, read from
`ops/environments/shared.yaml` on the stack's `main`, so the client never
skews ahead of the cluster contract (ADR-031).

1. **Authenticate.** The deploy and test jobs run in the fork's protected
   `spi-stack` GitHub environment, the subject of the UAMI's federated
   credential (ADR-032); `azure/login@v3` uses the fork's `AZURE_CLIENT_ID`
   and the tenant and subscription variables.
2. **Connect.** `spi connect --resource-group $SPI_STACK_RESOURCE_GROUP
   --cluster $SPI_STACK_CLUSTER` wraps the hardened kubeconfig
   sequence living in `src/spi/azure_infra.py`: `az aks get-credentials`,
   `kubelogin convert-kubeconfig -l azurecli`, tenant-pinned exec
   environment. The context name carries the `spi-stack` prefix, so
   `guard.verify_spi_cluster()` passes without `SPI_SKIP_GUARD`.
3. **Gate.** `spi status --json`; exit 0 means deployable and the
   job proceeds, exit 2 names the blocker: a convergence failure, the
   `maintenance` flag, or a missing deploy record (ADR-029, ADR-030).
4. **Deploy.** PR and push events run the same command; the fork build
   publishes to `ghcr.io/azure/<service>` under the short `SERVICE_NAME`:

   ```bash
   spi service pin "$SERVICE" \
     --image "ghcr.io/azure/${SERVICE}@${DIGEST}" \
     --ephemeral --run-id "$GITHUB_RUN_ID" \
     --source-repo "$GITHUB_REPOSITORY" --source-sha "$GITHUB_SHA" \
     --source-run-url "$RUN_URL"
   ```

   Push events skip the restore job; the weekday refresh converges the
   canonical afterward, forward to the fork's `main` once the service has
   flipped (ADR-031, ADR-033).
5. **Verify.** `spi service verify "$SERVICE" --image <ref>`
   asserts the Deployment's pod template and a running pod's `imageID` carry
   the digest and the rollout is complete. Deployment and container names
   default to `osdu-<service>`, the Flux Helm release name;
   `K8S_DEPLOYMENT_NAME` and `K8S_CONTAINER_NAME` cover deviants. With
   `--json` the last stdout line is a `{outcome, code, detail}` envelope;
   exit 2 carries the typed code, exit 1 means the cluster was unreachable.
6. **Test.** The integration-test job re-runs the verify as a pre-flight
   (the cross-pipeline guard: a colliding deploy fails fast, naming the
   colliding run from the pin annotation), resolves endpoints from
   `spi info --json`, resolves the secret map from Key Vault, health-gates
   the declared dependencies, then runs the suite.
7. **Restore.** An always-run job on PR pipelines:
   `spi service reset "$SERVICE" --if-run "$GITHUB_RUN_ID"`. The reset is
   conditional on ownership: it acts only while the live pin's `run_id` still
   matches, so a newer run's pin is left standing. Exit 0 means restored;
   exit 2 is the typed no-op refusal (`run_mismatch` when another pin owns
   the slot, `not_pinned` when nothing remains), which the restore job
   treats as success; exit 1 is a real failure. `--json` emits the same
   final-line `{outcome, code, detail}` envelope.

## Pin annotation schema

The pin rides the `spi-stack.osdu.dev/pins` annotation on the
`osdu-image-lock` ConfigMap (ADR-017), one record per service:

| Field | Content |
|---|---|
| `origin` | `gitlab-mr` or `github` |
| `repository`, `tag`, `digest` | the pinned image |
| `source_repo`, `source_sha` | what built it |
| `source_run_url`, `run_id` | the owning workflow run; `run_id` drives ownership checks and the stale-run lookup, `source_run_url` is display-only and never fetched |
| `ephemeral` | true when CI placed it; the only pins automation may sweep |
| `applied_at` | pin time |
| `canonical_*` | the restore target, recorded atomically with the overwrite |

Older annotations without the new fields keep decoding; the new fields
default empty, which reads as a non-ephemeral operator pin.

## Stale-pin recovery

A cancelled run, an expired token, or a lost runner strands a pin the restore
job never returns. The weekday refresh workflow runs the backstop (the
workflow step is unbuilt; the sweep verb exists):

- `spi service reset --ephemeral --stale-only` sweeps an ephemeral
  pin only when its owning workflow run reports a terminal state or, when
  that state is unreachable, when the pin's age exceeds a threshold longer
  than any deploy-plus-test budget. The lookup builds a fixed GitHub API URL
  from the validated `source_repo` (it must match the `Azure/osdu-spi-*`
  allow-list) and the numeric `run_id`; the fork-written `source_run_url` is
  display-only and never fetched, since a fork identity controls its value.
  An ephemeral pin cannot be written without an allow-listed `source_repo`,
  a commit, and a numeric `run_id`, so the lookup inputs always exist.
- `spi service refresh` (unbuilt) per GitHub-origin service then advances
  the environment to the current retained canonical (ADR-033).

A pin swept mid-run cannot happen silently: the test job's pre-flight verify
fails with the pin's replacement named, and the re-run is the recovery.
Push-deployed pins are swept the same way; after a service's flip the
refresh resolves the same or a newer `main` image, so nothing regresses.

## Fork repository configuration

| Variable | Set by | Meaning |
|---|---|---|
| `AZURE_CLIENT_ID` (secret), `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | `spi onboard` | the fork's UAMI and its home |
| `SPI_STACK_RESOURCE_GROUP`, `SPI_STACK_CLUSTER` | `spi onboard` | environment coordinates for `spi connect` |
| `K8S_DEPLOYMENT_NAME`, `K8S_CONTAINER_NAME` | `spi onboard` | verify targets; default to `osdu-<service>` |
| `ACCEPTANCE_TEST_DIR` | operator | Maven module path of the suite |
| `ACCEPTANCE_TEST_SECRET_MAP` | operator | `ENV_VAR=keyvault-secret-name` pairs; an unknown or unresolvable entry fails the job before Maven starts |
| `ACCEPTANCE_TEST_DEPENDENCIES` | operator | services whose health endpoints gate the suite; also absorbs a sibling's rolling restart |

`spi onboard <service> --repo Azure/osdu-spi-<service>` (unbuilt,
`src/spi/onboard.py`) provisions the UAMI, its federated credential for the
fork's protected `spi-stack` environment, and the Azure role assignments
(ADR-032), stamps the variables above via `gh`, and emits the RoleBinding
subject line whose stack PR completes cluster access. It is idempotent and
supports `--dry-run`. Once the
configuration is present, the template's readiness tooling activates the
reserved required checks `🚀 Deploy to spi-stack` and `🧪 Integration Tests`
on the fork; the jobs themselves live in the template's workflows, not in
this repo.

## Recipes

Hand-pin a fork image against a standing environment and return it:

```bash
spi service pin partition \
  --image ghcr.io/azure/partition@sha256:<digest>
spi service verify partition --image ghcr.io/azure/partition@sha256:<digest>
spi service list        # shows the pin without the ephemeral marker
spi service reset partition
```

An operator pin placed this way carries no `ephemeral` marker, so the weekday
backstop leaves it alone until the reset.

Inspect what a stranded pin belongs to:

```bash
kubectl get cm osdu-image-lock -n osdu-flux \
  -o jsonpath='{.metadata.annotations.spi-stack\.osdu\.dev/pins}' | jq
```

## Related ADRs

- [ADR-017: Per-deploy image lock](../decisions/017-osdu-image-lock.md)
- [ADR-030: Machine-readable status and the deploy record](../decisions/030-machine-readable-status-contract.md)
- [ADR-031: Fork-built images deploy as ephemeral lock pins](../decisions/031-fork-image-deploys-as-ephemeral-pins.md)
- [ADR-032: Per-fork deploy identity and namespace RBAC](../decisions/032-per-fork-deploy-identity.md)
- [ADR-033: Canonical image source follows onboarding](../decisions/033-canonical-image-source-follows-onboarding.md)

## Source files

- `src/spi/pins.py`, `src/spi/images.py`, `src/spi/cli.py`, `src/spi/guard.py`
- `src/spi/onboard.py` (planned)
- `software/charts/osdu-spi-service/templates/deployment.yaml`
- The fork-side jobs: `Azure/osdu-spi` `.github/template-workflows/`
