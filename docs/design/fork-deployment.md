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
tags), declaration enforcement, `spi service refresh`, and the refresh
workflow's backstop step are ahead of the code
(phases 4 and 5 of the roadmap in
[environment-lifecycle.md](environment-lifecycle.md)). Remove the marks as
they land.

## The sequence

The template's [validation workflow](https://github.com/Azure/osdu-spi/blob/main/.github/template-workflows/validate.yml)
implements one credentialed `deploy-test` job, described by
[ADR-041](https://github.com/Azure/osdu-spi/blob/main/doc/src/adr/041-borrow-prove-restore-lane.md).
It runs for eligible same-repository pull requests and pushes to `main` or
`fork_integration`. A separate `deploy-gate` reports eligibility or a skip
reason, including missing onboarding values, descriptor, or published image,
and an acceptance build reporting that the descriptor declares no test suite.
Outside-repository PRs do not enter the credentialed lane.

1. **Authenticate and install.** The job uses `azure/login` as the deploy
   identity in the fork's `spi-stack` GitHub environment. It installs the latest
   released `spi` wheel to reach the environment.
2. **Connect and match versions.** `spi connect` uses the repository's resource
   group and cluster pointers. The job reads `environment.stackVersion` from
   `spi status --json` and installs that exact release when it names a release
   tag. Branch-based or unrecorded versions keep the latest release; the job
   does not read a hardcoded shared-environment declaration.
3. **Gate.** The job polls `spi status --json` every 20 seconds for up to ten
   minutes until `deployable` is true. On timeout it reports the status reason,
   including maintenance or convergence failures. It then reads environment
   facts with `spi info --json`.
4. **Borrow.** `spi service pin` writes the published service image by digest
   with `--ephemeral`, the workflow run id, and source provenance. Flux
   reconciles the image-lock change. A pin refusal while the environment is
   not deployable is retried for up to ten minutes; a refusal while deployable
   fails immediately.
5. **Verify.** The job polls `spi service verify` until the expected digest is
   live, with a 15-minute limit. `lock_mismatch` fails immediately. The CLI
   checks the Deployment's template, a running pod's `imageID`, and rollout
   completion. The lane does not perform another verify immediately before
   each suite.
6. **Prove.** The descriptor resolver binds each suite's inputs from environment
   facts and minted tokens. The job runs each declared suite from the acceptance
   image under its own timeout. Surefire and Failsafe reports establish the
   verdict: zero exit status, at least one non-skipped test, and no failures
   or errors. A successful container exit alone is insufficient.
7. **Restore.** After both PR and push tests, the Restore step runs even after
   earlier failure when CLI installation succeeded. It calls
   `spi service reset` with `--if-run` and the workflow run id. Reset changes
   the pin only while that run still owns it, preserving a newer run's pin.
   Exit 2 is an ownership/no-pin refusal treated as success by the lane;
   exit 1 is a restore failure. A lost runner can still strand a pin.

Key Vault binding materialization and pre-borrow checks of descriptor loads,
groups, and dependencies are not wired into the lane. The resolver accepts
those contract fields, but this does not establish that the environment meets
them. See [template #175](https://github.com/Azure/osdu-spi/issues/175) and
[template #176](https://github.com/Azure/osdu-spi/issues/176).

Both PR and push tests restore the captured canonical image. Advancing the
canonical image follows the environment's source policy and refresh lifecycle;
a successful push test does not leave its candidate image installed.

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

The post-pin verify detects a replacement observed during that step. There is
no second verification before each suite, so replacement after verification
is not covered by that check. A stranded push-test pin follows the same
recovery path as a stranded PR-test pin.

## Fork repository configuration

| Variable | Set by | Meaning |
|---|---|---|
| `AZURE_CLIENT_ID` (secret), `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | `spi onboard`, or by hand from `spi info` | the environment's deploy identity and its home; identical for every fork in an organization, so `--org` sets them once |
| `SPI_STACK_RESOURCE_GROUP`, `SPI_STACK_CLUSTER` | `spi onboard`, or by hand from `spi info` | environment coordinates for `spi connect` |
| `.spi/service.yaml` | service repository | versioned suite paths, Maven arguments, bindings, and declared requirements |

Suite configuration lives in the descriptor. The current lane does not consume
`ACCEPTANCE_TEST_DIR`, `ACCEPTANCE_TEST_SECRET_MAP`, or
`ACCEPTANCE_TEST_DEPENDENCIES` repository variables. The CLI supports
`K8S_DEPLOYMENT_NAME` and `K8S_CONTAINER_NAME` environment overrides, but the
lane does not export those repository variables.

The repository-to-package mapping is deterministic: onboarding `partition`
from `<owner>/<fork>` selects `ghcr.io/<lowercase-owner>/partition`, even
when the repository basename is not `partition`. The descriptor's
`service.name` must match the workflow's effective service identifier:
`SERVICE_NAME` when set, otherwise the repository name. A repository named
`osdu-spi-partition` therefore needs `SERVICE_NAME=partition` to target the
`partition` service and package; the workflow does not read the descriptor
to choose this fallback. The build publishes
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
| 2. Azure trust | Enable `fork-<service>` on `spi-stack-<env>-deployer`, `spi-stack-<env>-member`, and `spi-stack-<env>-noaccess` for `repo:<org>/<fork>:environment:spi-stack`, after reading back that the environment exists and admits every branch | write on the three identities and read access to repository rules |
| 3. Cluster trust | Project the observed credential roster and existing source policy into `osdu-image-lock` without changing resolved images or pins | the operator's kube context |
| 4. Source policy | When requested, validate promotion preconditions, write `spi-source-<service>` on the RG, and update the lock's source projection (ADR-033) | RG tag write and the operator's kube context |

Planning first reads the environment profile from `spi info --json` and
refuses anything but `core`, since `minimal` and `bare` deploy no OSDU
services and no lock for phase 3 to project into (ADR-032). It then
resolves the repository through the GitHub API and carries its
canonical casing into every later write, since Entra matches the federated
subject exactly. It reads the credential roster next and refuses before
phase 1 when the repository already backs another service or any of the
identities holds twenty credentials (ADR-032); neither failure is recoverable
in phase 2. The member and no-access identities carry the same credentials
and no Azure role, so a fork's non-admin tests run as a member entitlements
knows and its 401 tests as a caller it has never met; their client ids are
read from `deploy_identity.member_client_id` and
`deploy_identity.no_access_client_id` in `spi info --json` at run time.
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

Once trust, the five values, and the descriptor are present, the next eligible
build can enter `deploy-test`. The template's `validation-summary` job reports
build, push, and deploy results through the required Validation Summary check.
A skipped deploy lane can leave that summary green, so inspect the gate reason
and suite results to establish live acceptance coverage. Separate reserved
`Deploy to spi-stack` and `Integration Tests` checks are not the shipped lane's
required-check model. A successful lane run proves the fork can authenticate,
borrow, test, and attempt restoration; onboarding cannot mint the fork's OIDC
token to prove those steps itself.

## Recipes

The stack provisions three test identities and the CLI can mint each with
`spi token`, `spi token --member`, or `spi token --no-access`. The shipped
  (`RESOLVER_NO_ACCESS_TOKEN`) and, when provisioned, member-token
  (`RESOLVER_MEMBER_TOKEN`) bindings. Template PR #190 is merged, so suites
  requiring the member caller can use it with a template revision that includes
  that support.
includes that support.

The deploy identity is the positive caller: the `entitlements-members` Job
adds its client id to `users`,
`users.datalake.ops`, `users.datalake.admins`, `users.data.root`, and
`users.datalake.delegation` for every partition, creating the delegation group
and `users.datalake.impersonation` first when the Azure tenant bootstrap has
not. A new Job runs when the environment is built, the identity changes, or
the chart's seed generation is bumped. `spi info --json` reports
`entitlements_domain` from `SERVICE_DOMAIN_NAME` on the live
`osdu-entitlements` Deployment, with an empty value before that Deployment
exists. It reports `entitlements_seeded.<partition>` once the Job has completed.
Two identities carry the negative paths, and both hold the same federated
credential as the deployer. The member identity is the caller the OSDU
suites name `NO_ACCESS_USER`: the same Job seeds it into `users` and every
`service.<name>.user` group, so it may call each service and holds no admin
role, and entitlements answers its admin-only requests with 403. The
no-access identity belongs to no group at all, and entitlements answers it
with 401 before any group check; suites use it where they expect an
unknown caller. The CLI mints each caller for `azure.token_audience`, read
per run because it depends on the environment: the management audience by
default, or an operator's app registration. The Istio identity filter
projects each token's own application id, so the three callers reach
entitlements as three different principals whatever audience they minted for.

Partition is not a witness for either caller. Its Azure provider admits any
app-only token from the tenant and refuses any token that names a user, so
the no-access identity gets 200 there and a human's own token gets 403.

The template's Mint test callers step exchanges the run's OIDC token directly
with Entra for additional callers, preserving the deploy identity's Azure CLI
session for Restore. Switching the job's Azure CLI login to a test-only identity
would leave Restore without the deploy identity. Use the
[workflow implementation](https://github.com/Azure/osdu-spi/blob/main/.github/template-workflows/validate.yml)
and its resolver binding contract for the callers supported by the deployed
template revision. Provisioning a stack identity alone does not wire it into a
service's CI lane.

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
