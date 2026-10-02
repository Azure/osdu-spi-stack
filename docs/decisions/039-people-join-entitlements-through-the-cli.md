# ADR-039: People Join Entitlements Through the CLI

## Context

A person's own `az` token passes the mesh, because the v1 issuer rule accepts
the management audience (ADR-016), and then gets entitlements' 401: the
members Job seeds the workload, deploy, and member identities and nobody else
(ADR-015). Calling the APIs has meant borrowing a seeded identity with
`spi token` (ADR-032), so every caller is the same principal and a person
cannot exercise the role a real user would hold. Entitlements knows a caller
by the `x-user-id` the identity filter writes, and for a person's v1 token the
filter writes `unique_name`, a value that is neither the object id nor
reliably the sign-in name or the directory UPN.

## Decision

`spi users add`, `list`, and `remove` manage people's entitlements membership
from the CLI, and `spi token --me` prints the person's own bearer.

- **The member id is what the filter projects.** `src/spi/identity.py` gets
  the signed-in person's token for `azure.token_audience` from the
  environment's tenant and applies the claim order of the Lua in
  `istio_auth_resources()`. `add --me` stores that value, `unique_name` on a
  default stack. The filter is not changed.
- **An id for someone else is stored as typed.** `spi users add <id>` makes no
  directory lookup and verifies nothing; only the person holding the token
  can prove the id, by running `add --me`.
- **A role is one of four presets and is set, not accumulated.** `viewer`,
  `editor`, and `admin` are `users` plus `users.datalake.viewers`, `.editors`,
  or `.admins`; `ops` is `users`, `users.data.root`, and `users.datalake.ops`.
  The default is `admin`. Running `add` again with another role removes the
  other presets' groups, then adds that preset's groups; `users` stays. When
  an addition fails, the groups that run added in the partition are removed
  again, so the person holds neither the old role nor half of the new one.
  When a removal fails, the run stops before adding anything and the person
  keeps what is left of the old role. A rerun finishes either case. Partitions
  written before the failure stay written, and those after it are not touched.
- **The deploy identity writes.** `src/spi/users.py` calls the public
  entitlements API with the bearer `spi token` mints. The deploy identity sits
  in `users.datalake.ops`, which entitlements lets manage any group and which
  gates member deletion. Anyone who can mint that token can already call every
  API as an operator, so granting a person a role adds no authority.
- **`add --me` verifies with the person's token.** It calls the entitlements
  group listing in the first partition written for up to a minute. A JSON refusal
  after that means the stored id is not the one the mesh projects; a plain
  text refusal means the mesh refused the token and is reported at once.
- **The seeded identities are refused on `add` and `remove`.** Their client ids come
  from the cluster config and the workload ServiceAccount; `spi up` owns them.
  A group address is refused too, since entitlements would nest the group and
  give the role to every member of it.
- **`list` reads role groups, not every group.** Members of `users` are the
  roster; the role is the preset whose role groups the member holds directly,
  `custom` for another mix, `none` for `users` alone, and `seeded` for the
  three identities.

Rejected: store the Entra object id. It is the stable identifier, but the
filter writes `unique_name` for the token a person holds on a default stack,
so an object id matches nothing without changing the filter's Lua.

Rejected: resolve another person through Microsoft Graph. Guests and CI
identities often cannot make the lookup, and the directory's names do not
reliably equal the token's `unique_name`, so a resolved id is no more certain
than a typed one.

Rejected: declare people in the environment file and seed them with the
members Job. Membership would survive a reset, but it takes effect only after
a reconcile, puts personal identifiers in a reviewed file, and a Job cannot
verify with the person's token.

Rejected: write as the workload identity from an in-cluster Job. The deploy
identity already holds the authority and the CLI already mints its token, so
one command can write and verify.

## Consequences

- Membership lives only in entitlements. An environment reset or a new
  partition drops it, and each person runs `spi users add --me` again.
- The id belongs to one token shape. A person added with a v1 token is not a
  member when they present a v2 token, for which the filter writes `oid`;
  changing `AAD_CLIENT_ID` to an app registration means running `add --me`
  again.
- A mistyped id for someone else is stored and found out only when that
  person's call is refused.
- `identity.projected_user_id` duplicates the filter's claim order. A change
  to the Lua must change it too; `tests/test_identity.py` holds the cases.
- The entitlements `oid_validation` feature flag, which the stack does not
  set, rejects any member id that is not an object id or client id. Turning it
  on makes `add --me` fail with entitlements' 400 until the filter projects
  `oid` for people.
- `verify` proves entitlements admits the person in the first partition written. It
  calls no other service and says nothing about data whose ACL names groups
  the role does not hold.
