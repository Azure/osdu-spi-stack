# ADR-032: Per-Fork Deploy Identity and Namespace RBAC

## Context

A fork's deploy and test jobs (ADR-031) authenticate to Azure with GitHub
OIDC and need exactly four capabilities: fetch a kubeconfig, write the image
lock and trigger reconciles, read workload state for verification, and read
acceptance-test secrets. The generic `pull_request` federated subject is
minted for any pull-request job, untrusted fork PRs included, and client and
tenant ids are identifiers rather than secrets, so that subject cannot anchor
a privileged credential; on GitHub the trustable unit is a protected
environment. The control is credential-side, not network-side: the API server
is reachable from any runner, and what an untrusted job must lack is the
ability to authenticate.

## Decision

Each fork gets a user-assigned managed identity whose federated credential
trusts only the fork's protected deployment environment, and cluster
authorization names each identity explicitly.

- **Identity.** UAMI `spi-fork-<service>` with one federated credential for
  the fork's protected `spi-stack` GitHub environment
  (`repo:Azure/osdu-spi-<svc>:environment:spi-stack`). The deploy and test
  jobs run in that environment; its protection rules plus the template's
  ADR-036 trust gating decide which events reach it, and `fork_upstream`
  never does. Azure role assignments: Azure Kubernetes Service Cluster User
  Role on the cluster, Key Vault Secrets User on the environment vault.
- **Explicit-subject RBAC, reads split from writes.** Two namespace-scoped
  Roles in the stack's platform manifests carry the verbs, and their
  RoleBindings name each UAMI's service-principal object id as a `User`
  subject: managed-identity tokens carry no group membership claims, so a
  `Group` binding would never match. Onboarding adds one subject line to the
  stack-owned RoleBinding by PR, the same reviewed-line shape as the
  canonical flip (ADR-033).
  - `spi-fork-deployer` in `osdu-flux`: `configmaps` get/list; `configmaps`
    patch restricted by `resourceNames` to `osdu-image-lock`, so a fork
    cannot touch `spi-deploy-record`, the `maintenance` flag, or any other
    GitOps input; `kustomizations` get/list/watch/patch (the reconcile
    trigger and wait); `gitrepositories` get (the guard's fingerprint check
    hard-fails without it); `helmreleases` get/list.
  - `spi-fork-verifier` in `osdu`: `deployments` get/list/watch, `pods`
    get/list, `pods/log` get, `events` list, `configmaps` get/list.
  No create or delete on anything, and no Kubernetes `secrets` verb in either
  namespace: acceptance secrets come from Key Vault.
- **Onboarding a fork is one idempotent CLI operation.** `spi onboard`
  provisions the identity, credentials, role assignments, and group
  membership, and stamps the fork's repository configuration; re-running it
  repairs drift. The acceptance-suite variables stay operator-set. Command
  mechanics live in `docs/design/fork-deployment.md`.
- **CI passes the guard, never bypasses it.** Fork jobs acquire their
  kubeconfig through the CLI, which yields a context the guard's fingerprint
  check accepts; `SPI_SKIP_GUARD` stays out of CI.

Rejected: one shared app registration for the fleet. One credential to
manage, and environment-bound subjects would even fit under Entra's
20-federated-credential cap, but a single leaked credential would span eight
repositories and revocation would be all-or-nothing.

Rejected: the generic `pull_request` federated subject. No per-fork
environment to set up, but GitHub mints that subject for untrusted fork PRs
too, handing them a path to the credential.

Rejected: bind an Entra group instead of explicit subjects. Onboarding
without a cluster-state change, but managed-identity app-only tokens carry no
group claims, so the binding never authorizes anyone.

Rejected: `SPI_SKIP_GUARD=1` in CI with an arbitrary kube context. Removes a
CLI step from the job, but discards the fingerprint check that keeps a
mis-targeted kubeconfig from writing another cluster's lock.

Rejected: per-secret Key Vault scoping. Tighter least privilege, but RBAC
churn on each new test secret for a vault whose contents are one shared dev
environment's.

Rejected: acceptance credentials as Kubernetes Secrets. Saves the Key Vault
round-trip, but grants fork CI a `secrets` verb the Roles otherwise never
carry.

## Consequences

- Eight standing identities instead of one: more objects to audit, and
  per-repo revocation (delete one UAMI, one fork loses access, the rest keep
  working).
- Onboarding a fork is one `spi onboard` run plus one RoleBinding subject
  line in a stack PR, so a new fork's cluster access lands through the
  ordinary release-and-upgrade path rather than instantly.
- Each fork needs a protected `spi-stack` GitHub environment; environment
  protection becomes part of the trust surface the template's settings
  tooling has to assert.
- The two Roles are stack manifests under the single-owner rule (ADR-025) and
  version with the environment, so an RBAC change rides the ordinary
  release-and-upgrade path (ADR-028).
- The permission list is a contract with the CLI's implementation: a new
  `kubectl` call in the pin, verify, or info paths fails in CI until the Role
  grows with it.
