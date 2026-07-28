---
status: "accepted"
contact: "Yuchen Wang"
date: "2026-07-27"
deciders: "Yuchen Wang"
---

# Cluster-Side Onboarding for Service-Fork CI/CD

## Context and Problem Statement

The deploy lane (ADR-026, ADR-027) lets a service fork build an image and set it
on a running Deployment. That only works once the fork can actually reach the
cluster, and reaching it requires a set of coupled facts that live on both
sides: an Azure managed identity, federated credentials matching the exact OIDC
subjects the workflows run as, Azure RBAC for the cluster and Key Vault, read
access to the Flux objects the lane's CI-mode pre-flight inspects, membership in
the OSDU entitlements groups the acceptance tests authorize against, and the
repository secrets and variables that name all of it.

Established by hand, this is long, easy to get subtly wrong, and silent when it
is wrong: a missing entitlements membership or a federated credential with the
right subject but the wrong audience surfaces much later as an opaque CI
failure. It also has to be repeatable, because a fork is expected to move
between clusters as environments are rebuilt, and a half-moved fork authenticates
as a retired cluster's identity while every other setting points at the new one.

## Decision Drivers

- One command should establish everything a fork needs to deploy and test.
- Re-running it must be safe, and must repair partial or stale state.
- Moving a fork to a new cluster must be the same command, not a manual cleanup.
- Nothing may be granted that the lane does not actually need.
- The plan must be inspectable before anything is changed.

## Considered Options

- A single idempotent `spi onboard` command
- Documented manual steps
- A separate bootstrap tool outside the CLI

## Decision Outcome

Chosen option: "A single idempotent `spi onboard` command".

`spi onboard` owns the cluster-side half of CI/CD onboarding. It creates or
reuses the fork's managed identity and its federated credentials, assigns the
Azure roles the lane needs, grants read access to the Flux resources the
CI-mode pre-flight checks, seeds the CI identity into the entitlements groups
the acceptance suite authorizes against, and writes the resulting `AZURE_*`
secrets and repository/cluster link variables onto the target repository.

The command is idempotent by construction, and reconciles rather than assumes:
it compares what exists against what is required and repairs the difference, so
a re-run after a partial failure converges. Running it against a different
cluster re-homes the fork in one step, because the identity recorded on the
repository is what re-home detection reads. `--dry-run` prints the full plan,
including the Key Vault secrets it expects to be populated out of band, without
making any change.

Rejected: documented manual steps, because the coupling between Azure, the
cluster and the repository is exactly where hand-execution drifts, and the
failures are delayed and hard to attribute. Rejected: a separate tool, because
it would duplicate the CLI's existing cluster discovery, naming and
authentication.

### Consequences

- Good, because onboarding a fork is one reviewable command with a preview mode.
- Good, because re-running repairs drift instead of compounding it.
- Good, because re-homing a fork to a rebuilt cluster is the same command.
- Good, because the granted permissions are declared in one place and can be
  audited as a unit.
- Bad, because the command spans three systems, so its failure modes are
  correspondingly broad and it must be explicit about which side failed.
- Bad, because Key Vault secret *values* remain an out-of-band step; the command
  grants access to them but does not create them.
