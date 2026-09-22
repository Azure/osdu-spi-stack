# ADR-035: Deployer Permission Model and Preflight Check

## Context

`spi up` creates resources and assigns eleven built-in roles to six
identities: ten in the environment group and DNS Zone Contributor in the DNS
zone's group, which is often a different group. Contributor creates every
resource but excludes `Microsoft.Authorization/*/Write` and
`Microsoft.Authorization/*/Delete`, so a Contributor-only deployer fails at the
first Bicep role assignment after the resource group already exists. Most
recipients are managed identities the deployment creates, so their object ids
do not exist when an administrator grants access.

## Decision

The deployer holds two assignments at subscription scope: Contributor, and Role
Based Access Control Administrator with an ABAC condition that allows
`roleAssignments/write` and `roleAssignments/delete` only when the role
definition is one of the stack's eleven. The condition does not restrict
recipients or resources. Subscription scope covers the DNS zone's group in
`dns` ingress mode and survives `spi down`.

- `STACK_ROLES` in `src/spi/permissions.py` is the one list of the eleven role
  ids. The CLI builds the condition from it, and
  `tests/test_permissions.py` fails when a template under `infra/` assigns a
  role the list omits.
- The check reads `GET {scope}/providers/Microsoft.Authorization/permissions`,
  which Reader can call and which avoids Microsoft Graph. It evaluates four
  actions against each entry's `actions` and `notActions`: resource group
  write, managed cluster write, role assignment write, and role assignment
  delete.
- `spi check` runs it at subscription scope whenever `az` is signed in. `spi up`
  runs it after resolving the deployer and before the first change to Azure: at
  the environment group when it exists, where inherited grants are included,
  and at the subscription otherwise. `--dry-run` runs the same check.
  `spi down --purge` checks delete permission at each stack-owned external
  grant's scope before removing any.
- A denial stops the command and prints the missing `az role assignment create`
  commands with the deployer's object id filled in. No flag skips it. A failed
  permissions read prints a warning and the command continues.

Rejected: User Access Administrator at subscription scope. One built-in role
with no condition to maintain, but it permits assigning any role, Owner
included, to anyone in the subscription.

Rejected: Owner on a pre-created resource group. Works for one deploy without a
subscription-level grant, but `spi down --purge` deletes the group and the
grant with it, the DNS zone's group stays out of reach, and Owner permits
assigning any role inside the group.

Rejected: resource group scope as a documented configuration. Narrower, but
creating the group needs Contributor at subscription scope anyway, and
supporting it adds per-environment options to `spi check` and a redeploy
caveat for a small population.

## Consequences

- The check evaluates role-based permissions only. Assignment conditions and
  deny assignments are not evaluated, so a grant conditioned to fewer than the
  eleven roles passes and fails at the first template outside its list. The
  permissions API does not return assignment conditions, so an administrator
  compares an existing grant with the role table in `docs/install.md`.
- A new role in a template requires the same id in `STACK_ROLES` and a condition
  update by every administrator who granted the old set.
- A deployer learns the exact grant to request before any resource exists, and
  `spi check --json` carries the same commands for automation.
- The grant outlives every environment in the subscription; removing it is a
  manual administrator step.
