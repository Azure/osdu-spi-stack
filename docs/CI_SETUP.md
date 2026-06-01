# CI Setup

One-time setup required to run the GitHub Actions workflows in this repo.
The workflows themselves are version-controlled; the infrastructure they
depend on (Azure identity, branch protection) is not, and must be applied
out-of-band.

## Azure OIDC federation

GitHub Actions workflows in this repo authenticate to Azure via OpenID
Connect (OIDC) federated credentials — no client secrets are stored in
GitHub. The federation is one App Registration with three federated
credentials, one per OIDC subject the workflows run as.

### Already configured

App Registration `osdu-spi-stack-github` exists in the
`MCI-ENERGY-OSDU-DEVELOPER` subscription with:

| Resource | Value |
|---|---|
| App / Client ID | `d2ef60ef-a8e7-4755-8a36-33b98efe6851` |
| Federated subject (PR builds) | `repo:Azure/osdu-spi-stack:pull_request` |
| Federated subject (main builds) | `repo:Azure/osdu-spi-stack:ref:refs/heads/main` |
| Federated subject (smoke env) | `repo:Azure/osdu-spi-stack:environment:azure-smoke` |
| RBAC | `Contributor` + `User Access Administrator` at subscription scope |

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

# 2. Add federated credentials for each OIDC subject
for SUBJECT in \
  "repo:Azure/osdu-spi-stack:pull_request" \
  "repo:Azure/osdu-spi-stack:ref:refs/heads/main" \
  "repo:Azure/osdu-spi-stack:environment:azure-smoke"; do
  SHORT_NAME=$(echo "$SUBJECT" | sed 's|repo:Azure/osdu-spi-stack:||; s|[:/]|-|g')
  az ad app federated-credential create --id "$APP_ID" --parameters "{
    \"name\": \"github-$SHORT_NAME\",
    \"issuer\": \"https://token.actions.githubusercontent.com\",
    \"subject\": \"$SUBJECT\",
    \"audiences\": [\"api://AzureADTokenExchange\"]
  }"
done

# 3. RBAC at subscription scope (Contributor + UAA for smoke deploys)
SUB="/subscriptions/<SUBSCRIPTION_ID>"
az role assignment create --role "Contributor" --assignee "$APP_ID" --scope "$SUB"
az role assignment create --role "User Access Administrator" --assignee "$APP_ID" --scope "$SUB"

# 4. GitHub repo secrets
gh secret set AZURE_CLIENT_ID --body "$APP_ID" --repo Azure/osdu-spi-stack
gh secret set AZURE_TENANT_ID --body "<TENANT_ID>" --repo Azure/osdu-spi-stack
gh secret set AZURE_SUBSCRIPTION_ID --body "<SUBSCRIPTION_ID>" --repo Azure/osdu-spi-stack

# 5. Pre-create the bicep-whatif RG
az group create --name "spi-ci-whatif" --location "centralus" \
  --tags purpose=ci-whatif owner=osdu-spi-stack

# 6. azure-smoke environment with required reviewer
gh api -X PUT "repos/Azure/osdu-spi-stack/environments/azure-smoke" \
  --input - <<EOF
{
  "wait_timer": 0,
  "reviewers": [{"type": "User", "id": $(gh api user --jq .id)}],
  "deployment_branch_policy": null
}
EOF
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

Used by `smoke.yml` (and any other Azure-touching workflow that should be
gated). Required reviewer = `@danielscholl`.

**Limitation**: Required-reviewer gates apply only to `workflow_dispatch`
invocations, *not* to scheduled (cron) runs. Scheduled smoke runs proceed
without human approval — the orphan-RG sweeper is the safety net for that.
