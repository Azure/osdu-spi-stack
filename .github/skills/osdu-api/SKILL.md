---
name: osdu-api
description: "Call the OSDU APIs of the connected SPI Stack environment as the deploy, member, or no-access identity, or as yourself. Use to query records, search, list schemas or legal tags, check entitlements, compare what two identities may do, or probe service versions."
---

# OSDU API

`scripts/osdu.py` calls the OSDU APIs of the environment the current kubectl
context points at. It reads the base URL, service paths, and partitions from
`spi info --json` and gets every token from `spi token --json`, so it holds no
endpoint or credential logic of its own. Standard library only.

```bash
osdu() { uv run .github/skills/osdu-api/scripts/osdu.py "$@"; }

osdu connect                                  # read the environment, check the token
osdu call GET /api/legal/v1/legaltags
osdu call POST /api/search/v2/query -d '{"kind":"*:*:*:*","query":"*","limit":10}'
osdu services --probe                         # version and commit of each service
osdu disconnect                               # clear local state
```

`connect` is optional: `call`, `services`, and `token` connect on first use.
Run it again after `spi up` changes the ingress address or the partitions.
Point at another environment with `uv run spi connect`; state is kept per
kubectl context.

## Identities

Every command that sends a token takes `--as`, written after the subcommand.
`connect --as` sets the default for later commands and drops every cached
token; `call --as` overrides the default for one call.

| `--as` | Who | Holds |
|--------|-----|-------|
| `deploy` (default) | The environment's deploy identity | `users.datalake.ops`: every API, as an operator |
| `member` | The member test identity | `users`, the service user groups, storage admin; no schema or file access |
| `no-access` | The no-access test identity | Nothing; entitlements answers 401 |
| `me` | The person signed in to `az` | The role `spi users add` gave them |

Use `me` when the question is about a person's permissions. A person is a
member only after `spi users add`:

```bash
uv run spi users add --me --role viewer       # viewer, editor, admin (default)
osdu connect --as me
osdu call GET /api/entitlements/v2/groups
```

Compare identities by repeating one call:

```bash
osdu call GET /api/legal/v1/legaltags --as member
osdu call GET /api/legal/v1/legaltags --as no-access
```

`spi users add` changes entitlements membership. Run it only when the user
asks for a role change; reading with an identity never needs it.

## Making calls

```bash
osdu call <METHOD> <PATH> [options]
```

| Option | Meaning |
|--------|---------|
| `-d`, `--data` | JSON body, `@file.json` to read one, or `-` for stdin |
| `-q`, `--query` | Query string, for example `'limit=10&offset=0'` |
| `-p`, `--partition` | Partition for this call; defaults to the primary, or `$OSDU_DATA_PARTITION` |
| `--as` | Identity for this call |
| `--fail` | Exit 22 when the HTTP status is 400 or higher |

The output is one JSON document: `status`, `as`, `body`, and a `hint` when a
refusal has a known cause. An HTTP refusal exits 0 unless `--fail` is given;
read `status`.

## Environment values

Take these from the `connect` output; do not assume them.

- **Partition.** `partition` is the primary. An environment may declare more.
- **ACL groups.** `<group>@<partition>.<entitlements_domain>`, for example
  `data.default.viewers@opendes.dataservices.energy`.
- **Legal tag.** `partitions.<name>.legal_tag` is the tag seeded for sample
  data. It is empty until the legal init Job has succeeded.

## Service paths

`services` prints the paths this environment publishes. Any path under the
gateway works with `call`.

| Service | Base path | Common operations |
|---------|-----------|-------------------|
| Partition | `/api/partition/v1` | `GET /partitions`, `GET /partitions/{id}`; application tokens only |
| Entitlements | `/api/entitlements/v2` | `GET /groups`, `GET /groups/{email}/members` |
| Legal | `/api/legal/v1` | `GET /legaltags`, `GET /legaltags/{name}`, `POST /legaltags` |
| Schema | `/api/schema-service/v1` | `GET /schema`, `GET /schema/{id}` |
| Storage | `/api/storage/v2` | `GET /records/{id}`, `PUT /records`, `POST /query/records:batch` |
| Search | `/api/search/v2` | `POST /query`, `POST /query_with_cursor` |
| Indexer | `/api/indexer/v2` | `POST /reindex` |
| File | `/api/file/v2` | `POST /files/uploadURL`, `GET /files/{id}/metadata` |
| Workflow | `/api/workflow/v1` | `GET /workflow` |
| Unit | `/api/unit/v3` | `GET /unit` |
| CRS Catalog | `/api/crs/catalog/v2` | `GET /info` |
| CRS Conversion | `/api/crs/converter/v2` | `GET /info` |

Every service except `indexer-queue` answers `GET <base>/info`; `services
--probe` reports that one as `no info endpoint`.

## Refusals

| Answer | Layer | What it means |
|--------|-------|---------------|
| 401, plain text body | Mesh | The token was refused before any service saw it. Check its audience against `azure.token_audience` in `spi info --json`. |
| 401, JSON body | Service | No group in this partition allows the call. When `GET /api/entitlements/v2/groups` answers 401 too, the caller is in no group at all: `spi users add --me` for a person, expected for `no-access`. |
| 403 "Access Denied" from partition | Partition | The partition API admits application tokens only. It answers a person 403 whatever their role, and answers every seeded identity, `no-access` included. Use `--as deploy`. |
| 403 elsewhere | Service | The caller is known but lacks what the call requires. `spi users list` shows roles. |
| 404 on a record | Storage | Absent, or the caller is in none of the record's ACL groups. |

Script errors arrive on stderr as `{"error": "..."}` with exit 1:

| Error | Fix |
|-------|-----|
| "kubectl has no current context" | `uv run spi connect` |
| "no ingress address yet" | The gateway is still provisioning; `uv run spi status` |
| "`spi token --me` failed" | `az login` as a person in the environment's tenant |
| "`spi token` failed: ServiceAccount ... not found" | `spi up` on a release that provisions the test identities |

## Local state

`~/.cache/spi/osdu-api-<context>.json` (mode 0600 on Linux and macOS) holds the environment
facts and the bearer tokens minted so far, each reused until two minutes
before it expires. `disconnect` deletes it. Never print a token into a reply,
an issue, or a PR; `status` shows callers and lifetimes without them.

## References

- [API reference](references/api-reference.md): endpoints and request bodies per service
- [Search patterns](references/search-patterns.md): query syntax, sorting, paging, indexing delay
- [Record lifecycle](references/record-lifecycle.md): legal tag, create, read, update, delete
