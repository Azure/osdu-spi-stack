# Environment Declarations

Each file here is a reviewed pin naming exactly which stack release, profile,
and Azure placement a lifecycle workflow deploys onto one standing backing
environment. `src/spi/environment.py` owns the schema. `env-upgrade.yml` and
`env-refresh.yml` read it to drive a deployment; `release.yml`'s bump job
reads and validates it to open the version-bump PR, and is the only writer.

There is one declaration today, `shared.yaml`. Adding a declaration is a
reviewed PR of its own (see [Activation](#activation) below).

## Schema

```yaml
env: shared
stackVersion: v0.18.0
profile: core
location: westus3
ingressMode: azure
imageBranch: master
nameSuffix: a43c7
forks:
  - service: partition
    repo: Azure/osdu-spi-partition
  - service: legal
    repo: Azure/osdu-spi-legal
    canonicalSource: fork
```

| Key | Meaning |
|---|---|
| `env` | The `--env` value; also names the resource group and cluster, `spi-stack-<env>`. |
| `stackVersion` | The immutable release tag (`vX.Y.Z`) the environment is pinned to. Both the deployed Flux ref and the installed CLI wheel come from this tag. |
| `profile` | `core`, `minimal`, or `bare` (see `docs/architecture.md`). |
| `location` | Azure region, e.g. `westus3`. |
| `ingressMode` | `azure` or `dns` (see `docs/design/gateway-ingress.md`). |
| `imageBranch` | OSDU community registry branch canonical images resolve from. |
| `nameSuffix` | The five-character lowercase alphanumeric suffix that keeps Azure resource names, and the environment's hostname, stable across an upgrade or a future reset. |

`forks` is optional. Each entry trusts one repository to deploy one service
against the environment and says which image that service's canonical entry
follows:

| Entry field | Meaning |
|---|---|
| `service` | The service the repository builds; unique within the list. `forks` is valid only with `profile: core`. |
| `repo` | The `<org>/<fork>` the service's `fork-<service>` credential trusts; unique within the list, compared without case, at most nineteen entries (ADR-032). |
| `canonicalSource` | `community` or `fork`, default `community`; a fork is trusted first and promoted by a later reviewed change (ADR-033). |

The file is strict: no other keys are accepted, and every value is
validated before a lifecycle workflow acts on it (`scripts/export_environment.py`
exports the seven scalar keys and `declares_forks` to `$GITHUB_OUTPUT`; no
workflow step shell-evaluates the YAML directly).

## Fork ownership

A stack becomes declared when `spi up --declaration <owner>/<repo>:<path>`
runs against it, for example with the locator
`Azure/osdu-spi-stack:ops/environments/shared.yaml`. The CLI reads the file
on `main`, takes the seven keys as its provisioning options, refuses an
explicit option that disagrees, and records the locator in the resource-group
tag `spi-environment-declaration`. Later runs reuse the tag when the option is
omitted and refuse a different locator or a file they cannot load.

From then on the list owns the environment's trust and source policy. `spi up`
and the `env-refresh` step `spi onboard --reconcile --write` restore a
credential or `spi-source-<service>` tag the list names and revoke every
`fork-*` credential it does not, and `spi onboard` refuses a request that
disagrees with the file. A declaration with no `forks` therefore revokes all
fork trust, so `env-upgrade` passes `--declaration` only when the file lists
at least one fork; until then trust stays with `spi onboard`. Add every
repository the environment already trusts in the change that adds the first
entry. Mechanics are in `docs/design/fork-deployment.md`.

## Why the declaration is excluded from release parsing

`.release-please-config.json` excludes `ops/environments` from commit
parsing. A commit that only changes a declaration's `stackVersion` must never
itself become a release, or bumping the environment and releasing the stack
would loop. See `docs/design/environment-lifecycle.md` for the full pin and
bump flow.

## Activation

Adding a declaration is what turns the automation on:

1. `env-upgrade.yml` triggers on a push to `main` touching this file. If the
   push is the commit that creates the file for the first time, the workflow
   reports that manual activation is required and does not provision
   automatically.
2. An operator dispatches `env-upgrade` manually to perform the first
   provision.
3. From then on, merging a `stackVersion` bump PR (opened automatically by
   `.github/workflows/release.yml` after a release) triggers an upgrade the
   same way.

Before the first activation, the immutable `v*` tag ruleset
(`docs/tag-ruleset.json`) and the `azure-shared` GitHub environment must both
already be applied; see `docs/CI_SETUP.md`.

## What is implemented

- `env-upgrade`: first provision and later `stackVersion` upgrades.
- `env-refresh`: the weekday reconcile-and-probe schedule.

- `forks:` and the declaration locator, reconciled by `spi up` and by
  `env-refresh` (ADR-032, ADR-033).

## What is still future work

- `env-reset` (cold rebuild) and `env-teardown` (protected manual deletion).

See `docs/design/environment-lifecycle.md` for the complete roadmap.
