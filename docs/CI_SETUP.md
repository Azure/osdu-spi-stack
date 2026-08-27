# CI Setup

One-time setup required to run the GitHub Actions workflows in this repo.
The workflows themselves are version-controlled; the infrastructure they
depend on (Azure identity, branch protection) is not, and must be applied
out-of-band.

## Azure OIDC federation

GitHub Actions workflows in this repo authenticate to Azure via OpenID
Connect (OIDC) federated credentials — no client secrets are stored in
GitHub. The federation is one App Registration with three federated
credentials, one per OIDC context the workflows run as.

### Already configured

App Registration `osdu-spi-stack-github` exists in the
`<SUBSCRIPTION_NAME>` subscription with:

| Resource | Value |
|---|---|
| App / Client ID | `<APP_CLIENT_ID>` |
| Federated context (PR builds) | Pull request |
| Federated context (main builds) | `refs/heads/main` |
| Federated context (Smoke + Sweeper) | Environment `azure-smoke` |
| Federated context (env-upgrade + env-refresh) | Environment `azure-shared` |
| RBAC | `Contributor` + `User Access Administrator` at subscription scope |

The exact `sub` values are controlled by the repository's GitHub OIDC subject
customization and must match the Entra federated credentials exactly. Do not
infer or copy a subject shape from this document.

GitHub repo secrets:
- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`

Azure resources used by CI:
- Resource group `spi-ci-whatif` (in `centralus`) — read-only target for the
  `bicep-whatif` validation job.

### To reproduce from scratch

```bash
# 1. Create App Registration + Service Principal
APP_ID=$(az ad app create --display-name "osdu-spi-stack-github" --query appId -o tsv)
az ad sp create --id "$APP_ID"

# 2. Add one federated credential for each context. Replace every placeholder
# with the exact subject emitted by GitHub for this repository's current OIDC
# customization; the subject format itself is deliberately not prescribed here.
for ENTRY in \
  "pull-request:<PULL_REQUEST_SUBJECT>" \
  "main:<MAIN_BRANCH_SUBJECT>" \
  "azure-smoke:<AZURE_SMOKE_ENVIRONMENT_SUBJECT>" \
  "azure-shared:<AZURE_SHARED_ENVIRONMENT_SUBJECT>"; do
  NAME="${ENTRY%%:*}"
  SUBJECT="${ENTRY#*:}"
  az ad app federated-credential create --id "$APP_ID" --parameters "{
    \"name\": \"github-$NAME\",
    \"issuer\": \"https://token.actions.githubusercontent.com\",
    \"subject\": \"$SUBJECT\",
    \"audiences\": [\"api://AzureADTokenExchange\"]
  }"
done

# 3. RBAC at subscription scope (Contributor + UAA for smoke deploys)
SUB="/subscriptions/<SUBSCRIPTION_ID>"
az role assignment create --role "Contributor" --assignee "$APP_ID" --scope "$SUB"
az role assignment create --role "User Access Administrator" --assignee "$APP_ID" --scope "$SUB"

# 4. GitHub repository secrets
gh secret set AZURE_CLIENT_ID --body "$APP_ID" --repo Azure/osdu-spi-stack
gh secret set AZURE_TENANT_ID --body "<TENANT_ID>" --repo Azure/osdu-spi-stack
gh secret set AZURE_SUBSCRIPTION_ID --body "<SUBSCRIPTION_ID>" --repo Azure/osdu-spi-stack

# 5. Pre-create the bicep-whatif RG
az group create --name "spi-ci-whatif" --location "centralus" \
  --tags purpose=ci-whatif owner=osdu-spi-stack

# 6. Reviewer-free azure-smoke environment, restricted to protected branches
gh api -X PUT "repos/Azure/osdu-spi-stack/environments/azure-smoke" \
  --input - <<EOF
{
  "wait_timer": 0,
  "reviewers": [],
  "deployment_branch_policy": {
    "protected_branches": true,
    "custom_branch_policies": false
  },
  "can_admins_bypass": true
}
EOF
```

The environment-scoped OIDC subject is branch-agnostic. Restricting deployments
to protected branches prevents a workflow modified on an arbitrary branch from
obtaining the subscription-scoped identity, while scheduled runs from protected
`main` remain reviewer-free.

### Tightening the RBAC scope (follow-up)

`Contributor + UAA at subscription scope` is broad. The CI uses
sub-scope today only because `spi up` creates resource groups dynamically
under the subscription, and Workload Identity wiring requires `UAA`. A
follow-up could tighten this to a parent `spi-ci-sandbox` RG and have
`smoke.yml` create child RGs inside it.

## Branch protection on `main`

Applied via `gh api`:

```bash
gh api -X PUT repos/Azure/osdu-spi-stack/branches/main/protection \
  --input docs/branch-protection.json
```

The JSON spec at `docs/branch-protection.json` enforces:

| Setting | Value |
|---|---|
| Required status checks | `lint`, `typecheck`, `test`, `windows-shims`, `manifests`, `bicep-whatif`, `pr-title` |
| Strict status checks | Branches must be up-to-date before merging |
| Direct pushes | Blocked |
| Force pushes | Blocked |
| Branch deletion | Blocked |
| Linear history | Required (rebase or squash, no merge commits) |
| Conversation resolution | Required before merge |
| Stale reviews | Dismissed on new commits |
| CODEOWNERS review | Required |
| Admins | Bypass allowed (`enforce_admins: false`) |
| Required reviewers | 0 |

### Notes on the solo-maintainer configuration

- `required_approving_review_count: 0` because a single maintainer cannot
  approve their own PR. When the team grows past one maintainer, raise to
  `1` and require CODEOWNERS review will then have teeth.
- `enforce_admins: false` lets the maintainer self-merge their own PRs once
  CI is green, without needing a second human. When the team grows, set to
  `true`.
- `require_code_owner_reviews: true` is still useful in a solo configuration
  — it ensures CODEOWNERS file is honored if any additional reviewers are
  added later.

### To verify settings are applied

```bash
gh api repos/Azure/osdu-spi-stack/branches/main/protection \
  --jq '{
    checks: .required_status_checks.checks | map(.context),
    enforce_admins: .enforce_admins.enabled,
    code_owners: .required_pull_request_reviews.require_code_owner_reviews,
    linear_history: .required_linear_history.enabled
  }'
```

## GitHub Environment `azure-smoke`

Used by all three `smoke.yml` jobs. It deliberately has no protection rule:
scheduled smoke must provision, verify, and tear down without a human approval
gate. A required reviewer leaves the cron run in `waiting`; because Smoke also
uses one concurrency group, that waiting run blocks every later schedule and
turns the daily reliability signal into a series of silent cancellations.

Smoke and Sweeper use this same environment. With no environment-level
`AZURE_*` secrets configured, they inherit the existing repository secrets.
Environment secrets can override them later if these two workflows move to a
different subscription.

The orphan-RG sweeper is the cleanup backstop for full-workflow cancellation.

## GitHub Environment `azure-shared`

Used by every Azure-touching job in `env-upgrade.yml` and `env-refresh.yml`.
Same reviewer-free shape as `azure-smoke`, for the same reason: a scheduled
`env-refresh` run must reconcile and probe without a human approval gate, and
both lifecycle workflows share the `env-shared` concurrency group, so one
waiting run would block every later trigger.

```bash
gh api -X PUT "repos/Azure/osdu-spi-stack/environments/azure-shared" \
  --input - <<EOF
{
  "wait_timer": 0,
  "reviewers": [],
  "deployment_branch_policy": {
    "protected_branches": true,
    "custom_branch_policies": false
  },
  "can_admins_bypass": true
}
EOF
```

With no environment-level `AZURE_*` secrets configured, it inherits the
existing repository secrets. Add its exact OIDC subject to the same App
Registration as the other federated contexts (see
[Azure OIDC federation](#azure-oidc-federation) above) before dispatching
`env-upgrade` for the first time; a mismatched or missing subject fails
`azure/login@v3` with `AADSTS70021` rather than silently using another
identity.

### To verify

```bash
gh api "repos/Azure/osdu-spi-stack/environments/azure-shared" \
  --jq '{reviewers: .protection_rules, branch_policy: .deployment_branch_policy}'
```

## Tag protection (immutable release tags)

`docs/tag-ruleset.json` restricts `update` and `deletion` on any `v*` tag
while leaving `creation` unrestricted, so release-please can still create
the next release tag. This must be active before the first
`env-upgrade` dispatch: a stack version an operator has already deployed and
recorded must not be able to move to a different commit or disappear out
from under a standing environment.

```bash
gh api -X POST repos/Azure/osdu-spi-stack/rulesets \
  --input docs/tag-ruleset.json
```

### To verify

```bash
gh api repos/Azure/osdu-spi-stack/rulesets \
  --jq '.[] | select(.name == "immutable-release-tags") | {target, enforcement, conditions}'
gh api "repos/Azure/osdu-spi-stack/rulesets/$(gh api repos/Azure/osdu-spi-stack/rulesets --jq '.[] | select(.name=="immutable-release-tags") | .id')" \
  --jq '.rules | map(.type)'
```

The second command should print exactly `["update", "deletion"]`.

## Release automation (release-please)

`release.yml` runs release-please on every push to `main`. It maintains a
standing `chore: release X.Y.Z` PR; merging that PR creates a draft GitHub
Release, and the `assets` job builds the wheel, uploads it, and publishes.
After a real new release, a `bump-environment` job opens a PR bumping the
live `ops/environments/shared.yaml` declaration's `stackVersion` to match;
merging that PR triggers `env-upgrade`. `.release-please-config.json`
excludes `ops/environments` from commit parsing, so merging the bump PR
cannot itself start another release. One-time setup:

```bash
# 1. Add this repo to the osdu-spi-automation GitHub App installation.
#    Azure-org app installs are config-as-code: PR the repo name into
#    apps/azure/osdu-spi-automation.yaml in microsoft/github-operations
#    (OSPO reviews and merges; the merge performs the install).
#    Release automation uses: Contents write, Pull requests write,
#    Issues write (the autorelease:* labels go through the issues API).

# 2. App credentials as repo secrets
gh secret set RELEASE_APP_ID --repo Azure/osdu-spi-stack
gh secret set RELEASE_APP_PRIVATE_KEY --repo Azure/osdu-spi-stack

# 3. Squash-only merges; the PR title becomes the commit subject and the
#    body stays blank so PR descriptions cannot inject changelog entries.
gh api -X PATCH repos/Azure/osdu-spi-stack \
  -F allow_squash_merge=true -F allow_merge_commit=false -F allow_rebase_merge=false \
  -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=BLANK

# 4. Remove the labels from the retired label-driven release workflow
for l in release:patch release:minor release:major; do
  gh label delete "$l" --repo Azure/osdu-spi-stack --yes
done
```

Notes:

- PRs opened with `GITHUB_TOKEN` never trigger workflows; the App token is
  what lets the required checks report on the release PR.
- The `pr-title` check runs on `pull_request_target`, so it only exists once
  `pr-title.yml` is on `main`. Add it to branch protection after that merge,
  not before, or the PR introducing it deadlocks.
- Never switch the config to `release-type: python`: it would write the real
  version into `pyproject.toml`, breaking the `0.0.0+source` sentinel that
  `spi` uses to detect source checkouts. Stamping stays a build-time step.
- `bump-environment` reuses `RELEASE_APP_ID` and `RELEASE_APP_PRIVATE_KEY`
  (step 2 above) to open the `stackVersion` bump PR, generating its own App
  token rather than reusing release-please's.

## Activation ordering for the shared backing environment

The lifecycle workflows (`env-upgrade.yml`, `env-refresh.yml`) exist from the
moment this repository merges them, but they act on nothing until a live
declaration exists. Apply the prerequisites above in this order, once:

1. Apply and verify the immutable `v*` tag ruleset
   ([Tag protection](#tag-protection-immutable-release-tags)).
2. Create and verify the `azure-shared` GitHub environment, and add its exact
   OIDC subject to the App Registration
   ([GitHub Environment `azure-shared`](#github-environment-azure-shared)).
3. Merge the activation PR adding `ops/environments/shared.yaml` (see
   `ops/environments/README.md`). This is an environment-only commit;
   release-please's `exclude-paths` keeps it from starting a release.
4. Dispatch `env-upgrade` manually to perform the first provision; the
   push that created the declaration only reports that manual activation is
   required.
5. Dispatch `env-refresh` once to prove the scheduled path; later weekday
   runs use the same workflow unattended.

Doing this out of order is unsafe: a first provision before the tag ruleset
is active could deploy from a tag that is later moved or deleted out from
under the standing environment.
