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
stackVersion: v0.6.0
profile: core
location: westus3
ingressMode: azure
imageBranch: master
nameSuffix: x7k2q
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

The file is flat and strict: no other keys are accepted, and every value is
validated before a lifecycle workflow acts on it (`scripts/export_environment.py`
exports the parsed result to `$GITHUB_OUTPUT`; no workflow step
shell-evaluates the YAML directly).

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

## What is still future work

- `env-reset` (cold rebuild) and `env-teardown` (protected manual deletion).
- Fork onboarding, per-fork identities, and canonical image source flips.

See `docs/design/environment-lifecycle.md` for the complete roadmap.
