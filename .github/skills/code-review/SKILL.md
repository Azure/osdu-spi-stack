---
name: code-review
description: Review rubric for pull requests in osdu-spi-stack. Use when reviewing a pull request or diff in this repo. Names the invariants a change can break without failing CI, and the classes of comment not to leave.
---

# Code Review

CI already runs ruff, `ty`, pytest, Bicep build, and the PR title check. A
review comment earns its place only when it names something those checks
cannot see. Write every comment in the `gh-voice` style: the defect, the
consequence, the fix if obvious.

## Flag

- **Chart edits without a version bump.** Any change under `software/charts/`
  must move `version` in that chart's `Chart.yaml`. Flux repackages a path
  chart only when the version changes, so an unbumped edit never reaches a
  running cluster.
- **Safeguards drift in `osdu-spi-service`.** Every OSDU service HelmRelease
  consumes this chart. A template change that drops `runAsNonRoot`,
  `RuntimeDefault` seccomp, `allowPrivilegeEscalation: false`,
  `capabilities.drop: [ALL]`, resource limits, or probes fails admission on
  every service at once.
- **Deployment-model changes with no ADR.** `docs/decisions/` governs how the
  stack is deployed. A change to namespaces, identity, ingress, secret
  handling, Flux layering, image pinning, or environment lifecycle that
  contradicts an accepted ADR needs that ADR amended or superseded in the
  same PR.
- **Silent state changes.** Every `az` or `kubectl` call that changes Azure or
  cluster state goes through `run_command` in `src/spi/shell.py` so the user
  sees it in a panel before it runs. A bare `subprocess` call or a
  `run_process` that mutates state bypasses this. Read-only queries may stay
  silent.
- **Stored credentials.** Workload Identity is the only data-plane path. Flag
  key or SAS authentication, connection strings with secrets, `disableLocalAuth`
  set to false, or any secret value written to a file, log, or manifest.
- **Non-Azure scope.** Code or manifests for other cloud providers, or
  references to `*-aws/`, `*-gc/`, `*-ibm/`, `*-core-plus/` upstream trees.
- **Comments that restate the code**, cite a PR or review round, or narrate
  what changed. Comments stay when they carry cross-file coupling, an external
  contract, or why the obvious approach was not taken.
- **Prose under `docs/`** that breaks `docs/STYLE.md`: dashes, hedge adverbs,
  recaps, claims with no artifact behind them.
- **PR description shape.** Missing Summary, a Changes list that walks files
  instead of concerns, a Notes section with nothing in it, or a validation
  checklist. The shape is in `CONTRIBUTING.md`.

## Do not flag

- Formatting, import order, line length, quote style, type annotations. Ruff
  and `ty` gate these.
- Requests for tests, a test plan, or "consider adding a test" without naming
  the input that would fail.
- Rewording that does not change meaning.
- Suggestions to add docstrings, logging, or error handling with no concrete
  failure named.
- Anything already covered by an accepted ADR. Read the ADR before suggesting
  the approach it rejected.

## Weight

One confirmed finding from the Flag list is worth more than ten style notes.
When nothing on the Flag list applies, say so and approve; do not fill the
review with nits.
