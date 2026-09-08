---
name: code-review
description: Review pull requests and diffs in osdu-spi-stack for correctness and deployment invariants. Include editorial feedback when requested.
---

# Code Review

Review correctness alongside the repository-specific checks below. Report
substantiated problems and their consequences, with a fix when clear. Use
`gh-voice` for GitHub comments.

CI runs ruff, `ty`, pytest, Bicep build, and the PR title check. Do not repeat
formatting or type-check diagnostics; still review behavior and contracts
that pass those checks.

## Repository checks

- **Chart versions.** Changes under `software/charts/` require a `version`
  bump in the affected `Chart.yaml`. Flux does not repackage a path chart
  until that version changes.
- **Safeguards.** Every OSDU service HelmRelease consumes `osdu-spi-service`.
  Preserve `runAsNonRoot`, `seccompProfile.type: RuntimeDefault`,
  `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, resource
  requests and limits, and liveness and readiness probes. Init containers
  need the same security context (ADR-004). Removing safeguards can cause
  admission failures across all services.
- **ADRs.** Read the governing record in `docs/decisions/` before evaluating
  changes to namespaces, identity, ingress, secret handling, Flux layering,
  image pinning, or environment lifecycle. A contradictory change needs the
  ADR amended or superseded in the same PR. Do not relitigate accepted
  decisions; flag implementations that violate them.
- **Visible state changes.** Mutating `az` and `kubectl` calls use
  `run_command` in `src/spi/shell.py` so the user sees them before execution.
  Bare `subprocess` or `run_process` calls must not bypass that display.
  Read-only queries may stay silent.
- **Credentials.** Azure data-plane access uses Workload Identity (ADR-023).
  Flag Azure key/SAS authentication, `disableLocalAuth: false`, committed
  credentials, or secret values exposed in logs. Runtime middleware Secrets
  and Key Vault writes follow ADR-010; their credential values are not
  themselves a defect.
- **Azure scope.** Keep implementation Azure-only. Upstream provider work
  is limited to `*-azure/` and shared `*-core/`.
- **Comments.** Keep cross-file coupling, external contracts, and reasons
  for non-obvious choices. Flag comments that restate code or narrate PRs
  and review rounds.
- **Documentation.** Apply `docs/STYLE.md` to prose under `docs/`, including
  its exceptions for quoted identifiers and explicit uncertainty.
- **PR descriptions.** Follow `CONTRIBUTING.md`: Summary, changes grouped
  by concern, and Notes only when needed. No validation checklist.

## Feedback

- Give file and line references, and name the failing input or behavior.
- Request tests, logging, error handling, or documentation only for a
  concrete gap; avoid generic requests for more coverage or docstrings.
- Skip stylistic preferences and meaning-preserving rewrites unless the
  user requests editorial feedback. Explicit house-style violations remain
  in scope.
- If no substantiated problems remain, say so and approve. When invoked as
  the reviewer, the review is the deliverable.

## Weight

One confirmed finding from the repository checks is worth more than ten
style notes. When nothing on that list applies, say so; do not fill the
review with nits.
