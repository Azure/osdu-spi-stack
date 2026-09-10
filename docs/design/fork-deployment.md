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

**Status.** `spi service pin --image --ephemeral`, `verify`, the
ownership-checked `reset --if-run`, the separate stale sweep
(`reset --ephemeral --stale-only`), and the trust path of `spi onboard`
(phases 1 to 3 below, `--list` and `--remove` for trust and projection,
roster-derived pin validation, repository-derived GHCR package validation)
are implemented.
Phase 4 source policy (`--canonical-source`, the `spi-source-<service>`
tags), declaration enforcement, `spi service refresh`, the refresh
workflow's backstop step, and the fork-side jobs are ahead of the code
(phases 4 and 5 of the roadmap in
[environment-lifecycle.md](environment-lifecycle.md)). Remove the marks as
they land.

## The sequence

Each job authenticates fresh (the OIDC JWT lives ~5 minutes; one
`azure/login` per job, the smoke pipeline's discipline) and installs the
`spi` wheel matching the environment's declared `stackVersion`, read from
`ops/environments/shared.yaml` on the stack's `main`, so the client never
skews ahead of the cluster contract (ADR-031).

1. **Authenticate.** The deploy and test jobs run in the fork's
   `spi-stack` GitHub environment, the subject of the federated credential
   `spi onboard` added to the environment's deploy identity (ADR-032);
   `azure/login@v3` uses `AZURE_CLIENT_ID` and the tenant and subscription
   variables, which are the same for every fork trusting that environment.
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
   publishes to `ghcr.io/<lowercase-owner>/<service>`, where the owner comes
   from the fork repository and `SERVICE` is its short `SERVICE_NAME`
   (ADR-033):

   ```bash
   IMAGE_OWNER=$(printf '%s' "$GITHUB_REPOSITORY_OWNER" | tr '[:upper:]' '[:lower:]')
   spi service pin "$SERVICE" \
     --image "ghcr.io/${IMAGE_OWNER}/${SERVICE}@${DIGEST}" \
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
   A verified envelope also carries `environment` (name, stack version,
   profile), the same block `spi status --json` and `spi info --json`
   publish, so the job's verdict can name the environment that proved it.
6. **Test.** The integration-test job re-runs the verify as a pre-flight
   (the cross-pipeline guard: a colliding deploy fails fast, naming the
   colliding run from the pin annotation), resolves endpoints from
   `spi info --json`, resolves the secret map from Key Vault, health-gates
   the declared dependencies, then runs the suite. Its callers are the two
   stack identities, minted per run and never stored; see the two-token
   recipe below.
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
  than any deploy-plus-test budget. The CLI requires `source_repo` to match
  the lock's projected credential roster and requires a commit and numeric
  `run_id` when writing an ephemeral pin. That membership check validates
  claimed provenance against the projected configuration, not the caller's
  authenticated repository: ADR-032 grants trusted writers patch access to
  the whole lock, including both the roster and pin annotation.
  The lookup independently enforces `<owner>/<repo>` path syntax and a
  numeric `run_id` under the fixed GitHub API host; `source_run_url` stays
  display-only and is never fetched. Roster membership replaces the
  `Azure/osdu-spi-*` naming convention for personal and customer forks, but
  does not prove that only onboarded repositories can be lookup targets.
- `spi service refresh` (unbuilt) per GitHub-origin service then advances
  the environment to the current retained canonical (ADR-033).

A pin swept mid-run cannot happen silently: the test job's pre-flight verify
fails with the pin's replacement named, and the re-run is the recovery.
Push-deployed pins are swept the same way; after a service's flip the
refresh resolves the same or a newer `main` image, so nothing regresses.

## Fork repository configuration

| Variable | Set by | Meaning |
|---|---|---|
| `AZURE_CLIENT_ID` (secret), `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | `spi onboard`, or by hand from `spi info` | the environment's deploy identity and its home; identical for every fork in an organization, so `--org` sets them once |
| `SPI_STACK_RESOURCE_GROUP`, `SPI_STACK_CLUSTER` | `spi onboard`, or by hand from `spi info` | environment coordinates for `spi connect` |
| `K8S_DEPLOYMENT_NAME`, `K8S_CONTAINER_NAME` | operator, rarely | verify targets; default to `osdu-<service>` |
| `ACCEPTANCE_TEST_DIR` | operator | Maven module path of the suite |
| `ACCEPTANCE_TEST_SECRET_MAP` | operator | `ENV_VAR=keyvault-secret-name` pairs; an unknown or unresolvable entry fails the job before Maven starts |
| `ACCEPTANCE_TEST_DEPENDENCIES` | operator | services whose health endpoints gate the suite; also absorbs a sibling's rolling restart |

The repository-to-package mapping is deterministic: onboarding `partition`
from `<owner>/<fork>` selects `ghcr.io/<lowercase-owner>/partition`, even
when the repository basename is not `partition`. The descriptor's
`SERVICE_NAME` must match the short service identifier. The build publishes
that public package, the deploy job pins it by digest, and canonical refresh
resolves its `main` line after promotion. No separate package-path state or
Azure namespace fallback is involved.

`require_ghcr_repository` in `src/spi/images.py` checks GHCR host, path,
and digest shape without naming an owner. An ephemeral pin additionally
must use the package derived from its `source_repo` for the requested
service, and that `source_repo` must equal, case included, the repository
the lock's roster projection (`spi-stack.osdu.dev/trusted-repos`) records
for the service. Operator pins keep their explicit-image path without
requiring onboarding (ADR-031).

`spi onboard <service> --repo <org>/<fork>` (`src/spi/onboard.py`)
activates one repository against the connected environment. The deploy
identity, its Azure roles, and the two Roles already exist from `spi up`
(ADR-032). The plan groups commands by system and numbers these execution
phases; source promotion is separate from enabling trust:

| Phase | Change | Needs |
|---|---|---|
| 1. Repository protection | Create the `spi-stack` environment, or open one restricted to a branch list, then stamp the five values | repository admin for environment rules; organization admin for organization values with `--org` |
| 2. Azure trust | Enable `fork-<service>` on `spi-stack-<env>-deployer` and on `spi-stack-<env>-noaccess` for `repo:<org>/<fork>:environment:spi-stack`, after reading back that the environment exists and admits every branch | write on both identities and read access to repository rules |
| 3. Cluster trust | Project the observed credential roster and existing source policy into `osdu-image-lock` without changing resolved images or pins | the operator's kube context |
| 4. Source policy | When requested, validate promotion preconditions, write `spi-source-<service>` on the RG, and update the lock's source projection (ADR-033) | RG tag write and the operator's kube context |

Planning first reads the environment profile from `spi info --json` and
refuses anything but `core`, since `minimal` and `bare` deploy no OSDU
services and no lock for phase 3 to project into (ADR-032). It then
resolves the repository through the GitHub API and carries its
canonical casing into every later write, since Entra matches the federated
subject exactly. It reads the credential roster next and refuses before
phase 1 when the repository already backs another service or either
identity holds twenty credentials (ADR-032); neither failure is recoverable
in phase 2. The no-access identity carries the same credentials and nothing
else, so a fork's 403 tests run as a caller entitlements has never met;
its client id is read from `deploy_identity.no_access_client_id` in
`spi info --json` at run time.
Without `--write` the command prints the `gh`, `az`, and `kubectl` commands
for each phase and changes nothing; the plan is the handoff for whoever holds
the rights on each side. `--write` applies the phases in order. `--skip-repo`
omits repository writes but still reads the environment before enabling
trust; a missing or unreadable environment, or one restricted to a branch
list, stops activation. The environment admits every branch on purpose: a
pull request from another repository carries no OIDC token, so write access
on the fork is what bounds who can mint, and a branch list would only keep
the lane off the fork's own pull requests (ADR-032).

The plan compares credential issuer, subject and audience, repository
protection rules, readable values, source tags, and lock projections. Rows
are correct, drifted, missing, or unverified. GitHub does not return secret
values, so a present `AZURE_CLIENT_ID` secret remains unverified until
re-stamped or proved by a workflow run. A failed phase exits nonzero and
reports what completed and what remains; re-running re-reads state and
repairs drift. Failure recovery does not roll back by deleting pre-existing
trust, and a failed prerequisite does not advance promotion. The operation
is resumable, not a transaction across GitHub, ARM, and Kubernetes.

Credential reconciliation is serial per deploy identity: await a write
before issuing the next, including removals. A provider conflict from a
competing invocation causes bounded backoff and an observed-roster re-read.
Neither onboarding nor the lifecycle ensure path fans out credential writes
(ADR-032).

On an undeclared environment, onboarding preserves the source tag, or uses
community when it is absent. `--canonical-source fork` explicitly selects
the service's trusted fork; `--canonical-source community` selects community
without removing trust. Schema's promotion requires the paired loader at the
selected schema commit. A missing loader refuses promotion without changing
the durable community policy; trust-only onboarding still works. A loader
published later does not itself trigger promotion.

The retained `spi-environment-declaration` RG tag identifies a declared
environment (ADR-032). Onboarding loads that reviewed file from `main` and
requires the requested service, repository, removal, and source to match;
an omitted source option takes the declaration's `canonicalSource`.
Conflicting intent is refused before writes, naming the file to change
through a reviewed PR. An unreadable declaration is an error, not permission
to use undeclared mode. Shared onboarding first declares a community source,
then enables and proves the fork jobs; a later reviewed change promotes it.

`spi onboard --list` reports trust, canonical-source policy, declaration
ownership, and projection drift separately. `spi onboard --remove <service>`
records community, deletes the credential, and updates the lock projections;
it requires removal from the declaration first when one owns the environment.
Interrupted removal reports the unfinished phases and can be re-run.

The credentials persist trust; `spi-source-<service>` RG tags persist source
policy, including community for a trusted repository. Both outlive
`spi down` (ADR-034). Fork CI cannot read these ARM records, so the lock
carries their separate projections. `spi up` loads authoritative intent
before resolving images, reconciles its durable records, then rebuilds the
projections during bootstrap. A standing environment changes its resolved
image only on refresh; policy changes and projection repairs preserve active
pins and their captured restore targets.

Once trust, the five values, and the descriptor are present, the template's
readiness tooling activates the reserved required checks `🚀 Deploy to
spi-stack` and `🧪 Integration Tests` on the fork; the jobs themselves live
in the template's workflows, not in this repo. The first run of those jobs
is the verification: onboard cannot mint the fork's OIDC token itself. The
shared environment's source promotion follows a successful deploy and test
run with those gates active.

## Recipes

Mint the two test callers in fork CI. The deploy identity is the positive
caller: the `entitlements-members` Job adds its client id to `users`,
`users.datalake.ops`, `users.datalake.admins`, and `users.data.root` for
every partition when the environment is built or the identity changes, and
`spi info --json` reports `entitlements_seeded.<partition>` once that Job
has completed. The no-access identity is the negative caller: it carries
the same federated credential and belongs to no group. Entitlements is
expected to answer such a caller with 403, distinct from the 401 an
unauthenticated request draws; if a partition answers 401 instead, the
members Job seeds the no-access identity into `users` alone so the
distinction holds. Both tokens are minted for `azure.token_audience`, read
per run because it depends on the environment: the management audience by
default, or an operator's app registration. The Istio identity filter
projects each token's own application id, so the two callers reach
entitlements as two different principals whatever audience they minted for.

Partition is not a witness for either caller. Its Azure provider admits any
app-only token from the tenant and refuses any token that names a user, so
the no-access identity gets 200 there and a human's own token gets 403.

```yaml
- id: facts
  run: |
    spi info --json > facts.json
    echo "audience=$(jq -r .azure.token_audience facts.json)" >> "$GITHUB_OUTPUT"
    echo "noaccess=$(jq -r .deploy_identity.no_access_client_id facts.json)" >> "$GITHUB_OUTPUT"
- uses: azure/login@v2            # the deploy identity, AZURE_CLIENT_ID from the repository
  with: { client-id: ${{ secrets.AZURE_CLIENT_ID }}, tenant-id: ..., subscription-id: ... }
- run: echo "TOKEN=$(az account get-access-token --resource ${{ steps.facts.outputs.audience }} --query accessToken -o tsv)" >> "$GITHUB_ENV"
- uses: azure/login@v2            # the no-access identity holds no subscription role
  with: { client-id: ${{ steps.facts.outputs.noaccess }}, tenant-id: ..., allow-no-subscriptions: true }
- run: echo "NO_ACCESS_TOKEN=$(az account get-access-token --resource ${{ steps.facts.outputs.audience }} --query accessToken -o tsv)" >> "$GITHUB_ENV"
```

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
- [ADR-032: Environment deploy identity and namespace RBAC](../decisions/032-environment-deploy-identity.md)
- [ADR-033: Canonical image source follows onboarding](../decisions/033-explicit-canonical-image-source-policy.md)
- [ADR-034: Managed identities survive `spi down`](../decisions/034-deploy-identity-survives-down.md)

## Source files

- `src/spi/pins.py`, `src/spi/images.py`, `src/spi/cli.py`, `src/spi/guard.py`
- `src/spi/onboard.py`
- `software/charts/osdu-spi-service/templates/deployment.yaml`
- The fork-side jobs: `Azure/osdu-spi` `.github/template-workflows/`
