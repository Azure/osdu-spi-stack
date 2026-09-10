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
stackVersion: v0.8.0
profile: core
location: westus3
ingressMode: azure
imageBranch: master
nameSuffix: a43c7
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
- `forks:` in the declaration, `spi onboard`, and intent reconciliation
  before image resolution (ADR-032, ADR-033).

The planned `forks:` entry has three fields; this is not accepted by the
implemented schema above yet:

| Entry field | Meaning |
|---|---|
| `service` | The service being onboarded; unique within the declaration. `forks:` is valid only with `profile: core`. |
| `repo` | The `<org>/<fork>` trusted by its `fork-<service>` credential; unique within the declaration, at most nineteen entries (ADR-032). |
| `canonicalSource` | `community` or `fork`, default `community`; trust-only onboarding precedes an explicit promotion. |

The declaration owns both trust and canonical-source policy. A reviewed PR
adds or removes an entry, changes a repository, or promotes a source before
`spi onboard` may apply that intent. Conflicting imperative requests are
refused; they do not create temporary overrides for the next lifecycle run
to undo.

The planned first-provision input is
`spi up --declaration <owner>/<repo>:<path>`, for example the locator
`Azure/osdu-spi-stack:ops/environments/shared.yaml`. The CLI loads the
reviewed file on `main`, not a copy bundled in the release wheel, and takes
its provisioning fields and fork intent; conflicting explicit flags are
refused. It records the locator in the retained RG tag
`spi-environment-declaration`. Later runs reuse that tag when the option is
omitted and reject a conflicting locator or an unreadable or invalid file.
Without an input or retained locator, a stack is undeclared.

The planned `env-upgrade` wiring exports `declaration_locator` from its
`declare` job and passes it to `spi up --declaration` in `provision`, gated
on a release that supports the option. This is part of onboarding, not an
argument accepted by the implemented CLI. Source intent is loaded before
image resolution. Credentials, `spi-source-<service>` RG tags, and lock
projections are reconciled copies, not competing owners.

See `docs/design/environment-lifecycle.md` for the complete roadmap.
