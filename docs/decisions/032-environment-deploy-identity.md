# ADR-032: Environment Deploy Identity and Namespace RBAC

## Context

A fork's deploy and test jobs (ADR-031) authenticate to Azure with GitHub
OIDC and need exactly four capabilities: fetch a kubeconfig, write the image
lock and trigger reconciles, read workload state for verification, and read
acceptance-test secrets. The generic `pull_request` federated subject is
minted for any pull-request job, untrusted fork PRs included, so it cannot
anchor a privileged credential; on GitHub the trustable unit is a protected
environment. The control is credential-side, not network-side: the API server
is reachable from any runner, and what an untrusted job must lack is the
ability to authenticate.

The same stack deploys as a shared automated environment, a developer's
personal stack, and a customer's own environment. Object ids and fork
sources differ per environment, so nothing per environment can live in the
released manifests or CLI code.

## Decision

Every environment owns one deploy identity, provisioned by `spi up`. A
repository gains access when a federated credential naming its protected
deployment environment is added to that identity, and cluster authorization
is two namespace-scoped Roles bound to the identity through the cluster
config.

- **Provisioned on every stack.** `infra/modules/identity.bicep` creates
  UAMI `spi-stack-<env>-deployer` in the environment resource group, next to
  the workload identity, with Azure Kubernetes Service Cluster User Role on
  the cluster and Key Vault Secrets User on the environment vault
  (`infra/modules/rbac.bicep`). Its only standing federated credential is
  the cluster's, for `spi token` below; no repository can act as it until
  one is activated. `spi info --json` publishes its client id with the
  tenant, subscription, resource group, and cluster: the five values a
  fork holds.
- **Activated per repository.** `spi onboard <service> --repo <org>/<fork>`
  adds one federated credential for `repo:<org>/<fork>:environment:spi-stack`
  (`src/spi/onboard.py`). The credential list on the identity is the roster
  of trusted repositories; deleting one credential revokes one repository.
  The `spi-stack` environment exists and admits every branch before the
  credential is enabled; the deploy and test jobs run there on pushes to
  `main` and `fork_integration` and on the fork's own pull requests.
  Write access is the boundary. A pull request from another repository
  runs without an OIDC token, so it cannot mint the deploy identity
  whatever the environment's branch policy says, and a branch list only
  keeps the lane off same-repo pull requests; onboard treats one as drift.
  A maintainer who wants a human pause before a borrow adds required
  reviewers to the environment, which holds the job before its first
  credentialed step without any workflow change. `fork_upstream` never
  enters: its builds are core-only, without the Azure provider, so no
  image exists to borrow. Trust does not select a canonical image source;
  ADR-033 owns that separate policy and its promotion.
- **Repository names are canonical before they are persisted.** GitHub
  resolves `<org>/<fork>` case-insensitively but mints the OIDC subject
  with the repository's stored casing, and Entra matches a federated
  subject exactly. Onboarding resolves `--repo` through the GitHub API and
  writes the returned `full_name` into the credential subject, the RG tags,
  and the lock's roster and `source_repo` fields; those fields compare
  exactly. A declaration entry matches its repository case-insensitively
  and is reported as drift, not as a different repository, when only the
  casing differs. The subject itself is what GitHub reports it will sign
  for the repository (`sub_claim_prefix` from the OIDC customization
  endpoint), which by default carries the owner and repository ids,
  `repo:<owner>@<id>/<name>@<id>`; onboard reads it rather than composing
  the classic form, refuses a repository with a custom template, and treats
  a credential in the other form as drift to rewrite. A repository deleted
  and recreated under the same name gets a new id and must be onboarded
  again.
- **The roster is keyed by repository and capped by Azure.** Azure keeps
  the issuer and subject pair unique on an identity and allows twenty
  federated credentials per UAMI. The cluster credential holds one slot,
  so one repository backs exactly one service and an environment trusts
  at most nineteen repositories. `repo` is unique across the roster and
  the declaration, and planning counts every credential on the identity
  and refuses a second service naming an already trusted repository, or an
  entry past the cap, before any phase writes. Growth past the cap is a
  new decision, since a second identity needs its own RoleBindings, not a
  retry.
- **Credential writes are serial per identity.** Onboarding and lifecycle
  reconciliation await each credential create, update, or delete before
  starting the next. Bicep loops use `@batchSize(1)`, matching
  `infra/modules/identity.bicep`; CLI reconciliation uses an ordered loop.
  The Managed Identity RP rejects concurrent writes on one UAMI. Conflicts
  from competing invocations trigger bounded backoff and a roster re-read,
  not parallel retries or an unbounded reconciliation loop.
- **Declared intent wins.** Declared environments record `service`, `repo`,
  and `canonicalSource` per entry in `forks:`. The first declared provision
  takes `spi up --declaration <owner>/<repo>:<path>`, loads the reviewed file
  on `main` before image resolution, and records that locator in the RG tag
  `spi-environment-declaration`. The declaration supplies provisioning
  fields as well as fork intent; conflicting explicit flags are refused.
  Later `spi up` runs can read the retained locator when the option is
  omitted, and an explicitly supplied locator must match it. `spi onboard`
  reads it and refuses an addition, removal, repository change, or source
  choice that disagrees with it; the declaration changes through a reviewed
  PR first. Lifecycle runs reconcile credentials and source policy from
  that intent, not from a stale retained roster. An unreadable declaration
  blocks mutation; it does not turn a declared environment into an
  undeclared one.
- **Explicit-subject RBAC, reads split from writes.** Two Roles in the
  platform manifests carry the verbs; their RoleBindings name the deploy
  identity's principal id as a `User` subject, substituted from
  `spi-cluster-config` the way the Istio revision is. Managed-identity tokens
  carry no group claims, so a `Group` binding would never match.
  - `spi-fork-deployer` in `osdu-flux`: `configmaps` get/list; `configmaps`
    patch restricted by `resourceNames` to `osdu-image-lock`, so a fork
    cannot touch `spi-deploy-record`, the `maintenance` flag, or any other
    GitOps input; `kustomizations` get/list/watch for the converge wait, with
    no patch, since the lock's watch label makes reconciliation follow the
    lock write (ADR-031); `gitrepositories` get for the guard's fingerprint
    check; `helmreleases` get/list.
  - `spi-fork-verifier` in `osdu`: `deployments` get/list/watch, `pods`
    get/list, `pods/log` get, `events` list, `configmaps` get/list, `jobs`
    get for the legal-tag observation `spi info --json` reports.
  No create or delete on anything, and no Kubernetes `secrets` verb in either
  namespace: acceptance secrets come from Key Vault.
- **Onboarding plans by default.** `spi onboard` prints the `az`, `spi`, and
  `gh` commands it would run, grouped by the system they touch, and changes
  nothing until `--write`. It requires the `core` profile, read from the
  environment block `spi info --json` publishes: `minimal` and `bare`
  deploy no OSDU services (ADR-021), so trust granted there has no deploy
  target, and the refusal happens before phase 1 writes. A declaration
  pairing `forks:` with another profile is invalid. The phases establish repository protection, enable
  trust, and then apply source policy and cluster projections. `--skip-repo`
  omits GitHub writes, not the read-only protection precondition; missing or
  unreadable rules block activation. `--org` places the five values at
  organization level once. Re-running compares values and rules, reporting
  rows as correct, drifted, missing, or unverified. An interrupted apply
  exits nonzero, names completed and pending phases, and resumes from
  observed state; a failed prerequisite never advances source promotion.
  Command mechanics live in `docs/design/fork-deployment.md`.
- **CI passes the guard, never bypasses it.** Fork jobs acquire their
  kubeconfig through the CLI, which yields a context the guard's fingerprint
  check accepts; `SPI_SKIP_GUARD` stays out of CI.
- **Two more identities prove the non-admin 403 path and the
  unknown-caller 401 path.** `spi up` also creates UAMI
  `spi-stack-<env>-member`, which the entitlements-members Job seeds into
  `users` and every `service.<name>.user` group and nothing else, and UAMI
  `spi-stack-<env>-noaccess` with no Azure role and no entitlements group.
  These are the two negative callers the OSDU acceptance suites declare: a
  member who may call a service but holds no admin role, and a caller
  entitlements does not know, who draws 401. `spi onboard` federates both on
  the same `fork-<service>` credential and subject as the deployer and
  revokes all three together. `spi info --json` publishes them as
  `deploy_identity.member_client_id` and `deploy_identity.no_access_client_id`;
  forks read them per run instead of holding more repository values.
- **The cluster is a second issuer for the same identities.**
  `infra/modules/identity.bicep` federates the deployer to
  `system:serviceaccount:spi-test:spi-deployer`, the member identity to
  `system:serviceaccount:spi-test:spi-member`, and the no-access identity
  to `system:serviceaccount:spi-test:spi-no-access` on the AKS OIDC issuer,
  and `spi up` applies the three ServiceAccounts, annotated for workload
  identity, in the `spi-test` namespace it creates outside the mesh. Every
  Azure-provider service admits app-only tokens alone, so a developer's own
  `az account get-access-token`, which carries `upn`, is refused on every
  endpoint. `spi token` requests a ten-minute projected token for the
  ServiceAccount and exchanges it at the Entra v1 endpoint for a bearer
  whose `appid` is the deploy identity, the same principal fork CI holds
  through GitHub federation; `--member` and `--no-access` mint the two
  negative-path bearers.
  Who may mint is who may create tokens for those ServiceAccounts, a
  Kubernetes RBAC question the fork Roles answer with no: CI stays on the
  GitHub path. A Job on the `spi-deployer` account mints in-cluster through
  the webhook with no CLI step. `spi onboard --list` shows the credential
  as the cluster issuer; it is never projected as a trusted repository.

Rejected: one managed identity per fork in a separate persistent resource
group. Distinct principal names in the cluster audit log, but the same
lock-object blast radius, ten identities and fifty repository values for a
customer with ten forks, and a second resource group whose only purpose was
surviving `spi down` (now ADR-034).

Rejected: the generic `pull_request` federated subject. No per-fork
environment to set up, but GitHub mints that subject for untrusted fork PRs
too, handing them a path to the credential.

Rejected: RoleBinding subjects written into the stack manifests by PR. One
reviewed line per fork, but the principal id is per environment, so the
released manifest would carry the shared environment's id into every
personal and customer stack.

Rejected: bind an Entra group instead of explicit subjects. Onboarding
without a cluster-state change, but managed-identity app-only tokens carry no
group claims, so the binding never authorizes anyone.

Rejected: `SPI_SKIP_GUARD=1` in CI with an arbitrary kube context. Removes a
CLI step from the job, but discards the fingerprint check that keeps a
mis-targeted kubeconfig from writing another cluster's lock.

Rejected: acceptance credentials as Kubernetes Secrets. Saves the Key Vault
round-trip, but grants fork CI a `secrets` verb the Roles otherwise never
carry.

## Consequences

- The enforced boundary is the lock object, not a service's keys within it:
  any trusted repository can rewrite a sibling service's lock entry, and the
  CLI's validation is convention rather than authorization. That is accepted
  for a single-trust-level environment; pin provenance and the template's
  trust gating are the compensating controls. Separate trust levels require
  separate writable lock objects with scoped authorization, mediated writes,
  or separate environments. Another identity with the same lock permission
  adds attribution, not isolation.
- One principal in the cluster audit log for every fork. The pin annotation
  records claimed repository provenance, not independently authenticated
  attribution; a trusted writer can change both the pin and roster
  projection in the lock.
- The five values are identical across an organization's forks, so a
  customer sets them once at organization level; a personal stack's operator
  runs one command per fork against their own environment.
- The permission list is a contract with the CLI's implementation: a new
  `kubectl` call in the pin, verify, or info paths fails in CI until the Role
  grows with it.
- A repository can be trusted by hand, with the printed commands, by someone
  who never installs the CLI on the GitHub side.
