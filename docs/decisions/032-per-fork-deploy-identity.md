# ADR-032: Per-Fork Deploy Identity and Namespace RBAC

## Context

A fork's deploy and test jobs (ADR-031) authenticate to Azure with GitHub
OIDC and need exactly four capabilities: fetch a kubeconfig, write the image
lock and trigger reconciles, read workload state for verification, and read
acceptance-test secrets. Entra caps federated credentials at 20 per identity,
and the fleet needs 24 subjects (eight forks, three trusted refs each:
`pull_request`, `main`, `fork_integration`), so one shared identity cannot
carry the fleet. The control is credential-side, not network-side: the API
server is reachable from any runner, and what an untrusted job must lack is
the ability to authenticate.

## Decision

Each fork gets a user-assigned managed identity, and cluster authorization
binds to one Entra group so onboarding never edits cluster manifests.

- **Identity.** UAMI `spi-fork-<service>` with three federated credentials
  for the fork's trusted refs. `fork_upstream` gets none; it builds core-only
  and never deploys. Azure role assignments: Azure Kubernetes Service Cluster
  User Role on the cluster, Key Vault Secrets User on the environment vault.
- **Group-bound RBAC.** Each UAMI joins the Entra group
  `spi-stack-fork-deployers`. Two namespace-scoped Roles in the stack's
  platform manifests bind to the group:
  - `spi-fork-deployer` in `osdu-flux`: `configmaps` get/patch (the lock
    write and the reads behind `spi info`), `kustomizations`
    get/list/watch/patch (the reconcile trigger and wait),
    `gitrepositories` get (the guard's fingerprint check hard-fails without
    it), `helmreleases` get/list.
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
manage, but 24 subjects exceed the 20-credential cap, and a single leaked
credential would span eight repositories.

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
- Onboarding a fork is one `spi onboard` run; no cluster manifest changes,
  because authorization binds to the group.
- The two Roles are stack manifests under the single-owner rule (ADR-025) and
  version with the environment, so an RBAC change rides the ordinary
  release-and-upgrade path (ADR-028).
- The permission list is a contract with the CLI's implementation: a new
  `kubectl` call in the pin, verify, or info paths fails in CI until the Role
  grows with it.
