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
  (`infra/modules/rbac.bicep`). It carries no federated credential until a
  repository is activated, so it is inert on a stack that never onboards
  anything. `spi info --json` publishes its client id with the tenant,
  subscription, resource group, and cluster: the five values a fork holds.
- **Activated per repository.** `spi onboard <service> --repo <org>/<fork>`
  adds one federated credential for `repo:<org>/<fork>:environment:spi-stack`
  (`src/spi/onboard.py`). The credential list on the identity is the roster
  of trusted repositories; deleting one credential revokes one repository.
  The deploy and test jobs run in that protected environment; its protection
  rules restrict entry to `main`, `fork_integration`, and the PR runs the
  template's ADR-036 gate admits. `fork_upstream` is excluded: its builds are
  core-only, without the Azure provider. Declared environments record the
  roster as `forks:` in `ops/environments/<env>.yaml`, and the lifecycle
  workflows reconcile the identity's credentials to it.
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
    get/list, `pods/log` get, `events` list, `configmaps` get/list.
  No create or delete on anything, and no Kubernetes `secrets` verb in either
  namespace: acceptance secrets come from Key Vault.
- **Onboarding plans by default.** `spi onboard` prints the `az`, `spi`, and
  `gh` commands it would run, grouped by the system they touch, and changes
  nothing until `--write`. `--skip-repo` leaves the repository block out for
  operators who do not let the CLI touch GitHub; `--org` places the five
  values at organization level once. Re-running reports each row as existing
  or missing, which is the drift check. Command mechanics live in
  `docs/design/fork-deployment.md`.
- **CI passes the guard, never bypasses it.** Fork jobs acquire their
  kubeconfig through the CLI, which yields a context the guard's fingerprint
  check accepts; `SPI_SKIP_GUARD` stays out of CI.

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
  trust gating are the compensating controls, and a second deploy identity
  per trust level is the escalation if a fleet ever spans trust levels.
- One principal in the cluster audit log for every fork. Which repository
  acted is read from the pin annotation, not from the subject.
- The five values are identical across an organization's forks, so a
  customer sets them once at organization level; a personal stack's operator
  runs one command per fork against their own environment.
- The permission list is a contract with the CLI's implementation: a new
  `kubectl` call in the pin, verify, or info paths fails in CI until the Role
  grows with it.
- A repository can be trusted by hand, with the printed commands, by someone
  who never installs the CLI on the GitHub side.
