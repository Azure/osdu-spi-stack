# CI Setup

One-time setup required to run the GitHub Actions workflows in this repo.
The workflows themselves are version-controlled; the infrastructure they
depend on (Azure identity, branch protection) is not, and must be applied
out-of-band.

## Azure OIDC federation

GitHub Actions workflows in this repo authenticate to Azure via OpenID
Connect (OIDC) federated credentials — no client secrets are stored in
GitHub. Two credential scopes are intentionally separate:

- Repository secrets serve pull-request/main validation against the existing
  CI subscription.
- `azure-smoke` environment secrets serve Smoke + Sweeper against the backing
  smoke subscription.

This separation lets Smoke move subscriptions without repointing PR validation.

### Already configured

The repository-level validation identity has:

| Resource | Value |
|---|---|
| App / Client ID | `<APP_CLIENT_ID>` |
| Federated subject (PR builds) | `repo:Azure/osdu-spi-stack:pull_request` |
| Federated subject (main builds) | `repo:Azure/osdu-spi-stack:ref:refs/heads/main` |
| RBAC | Contributor access to the validation subscription/RG |

GitHub repo secrets:
- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`

The Smoke/Sweeper identity has:

| Resource | Value |
|---|---|
| App / Client ID | `<SMOKE_APP_CLIENT_ID>` |
| Federated subject | The exact immutable `environment:azure-smoke` subject emitted by GitHub |
| RBAC | `Contributor` + `User Access Administrator` on the smoke subscription |

GitHub environment secrets on `azure-smoke`:
- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`

Azure resources used by CI:
- Resource group `spi-ci-whatif` (in `centralus`) — read-only target for the
  `bicep-whatif` validation job.

### To reproduce the Smoke/Sweeper identity

```bash
# 1. Create App Registration + Service Principal
APP_ID=$(az ad app create --display-name "osdu-spi-stack-smoke-github" --query appId -o tsv)
az ad sp create --id "$APP_ID"

# 2. Add one environment-scoped federated credential.
# SUBJECT must be the exact immutable subject emitted by GitHub, not a guessed
# name-based value. It has this shape:
# repository_owner_id:<owner-id>:repository_id:<repo-id>:environment:azure-smoke
SUBJECT="<IMMUTABLE_AZURE_SMOKE_SUBJECT>"
az ad app federated-credential create --id "$APP_ID" --parameters "{
  \"name\": \"github-environment-azure-smoke\",
  \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"$SUBJECT\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"

# 3. RBAC at subscription scope (Contributor + UAA for smoke deploys)
SUB="/subscriptions/<SUBSCRIPTION_ID>"
az role assignment create --role "Contributor" --assignee "$APP_ID" --scope "$SUB"
az role assignment create --role "User Access Administrator" --assignee "$APP_ID" --scope "$SUB"

# 4. Ungated azure-smoke environment, restricted to protected branches.
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

# 5. Environment secrets (do not overwrite repository-level validation secrets)
gh secret set AZURE_CLIENT_ID --env azure-smoke --body "$APP_ID" --repo Azure/osdu-spi-stack
gh secret set AZURE_TENANT_ID --env azure-smoke --body "<TENANT_ID>" --repo Azure/osdu-spi-stack
gh secret set AZURE_SUBSCRIPTION_ID --env azure-smoke --body "<SUBSCRIPTION_ID>" --repo Azure/osdu-spi-stack
```

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
| Required status checks | `lint`, `typecheck`, `test`, `manifests`, `bicep-whatif` |
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

Both scheduled and manual runs use this same environment. Keeping the name
stable also keeps the existing federated subject valid:
`repo:Azure/osdu-spi-stack:environment:azure-smoke`.

The orphan-RG sweeper is the cleanup backstop for full-workflow cancellation.
