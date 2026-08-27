# ADR-030: Machine-Readable Status and the Deploy Record

## Context

Fork CI jobs and the environment ops workflows must answer two questions
without parsing Rich tables or raw `kubectl` output: is the environment ready
to receive a deploy, and what does it run. `spi status` renders human-only
output, and the deployed revision exists only in
`GitRepository.status.artifact.revision`, which no CLI surface reads. External
consumers that shell out to `kubectl` directly couple eight fork repositories
to object names and labels this repo is free to reshape.

## Decision

`spi status --json` emits a versioned envelope and typed exit codes; a deploy
record written at the end of `spi up` supplies the version fields.

- Envelope: `apiVersion: spi.osdu.dev/v1`, `ready` (true when each
  Kustomization reports `Ready=True`, the same predicate
  `scripts/wait_for_flux_ready.sh` polls), a typed `reason` naming the first
  blocking object when not ready, `suspended`, `maintenance`, Kustomization
  counts with a not-ready list, `stack` (ref, resolved commit, deploy
  timestamp, CLI version, profile), `images` (branch, resolved-at, count,
  pinned services), and `baseUrl`.
- Exit codes: 0 ready, 2 not ready with a reason, 1 unreachable or guard
  failure. CI gates on the exit code and reads the JSON only for detail.
- The deploy record is written twice, for two audiences: RG tags
  (`spi-stack-version`, `spi-deployed-utc`) readable with no cluster access,
  and a `spi-deploy-record` ConfigMap in `osdu-flux` (ADR-019) holding the
  ref, the resolved commit from `GitRepository.status.artifact.revision`, the
  CLI version, profile, and timestamp.
- The ConfigMap also carries a `maintenance` flag the lifecycle workflows set
  before an upgrade or reset and clear after; `spi service pin` (ADR-031)
  refuses while it is set, so a fork deploy fails fast with a reason instead
  of racing a lifecycle operation.
- Endpoints, partitions, and secret references stay in `spi info --json`,
  which gains the same `apiVersion` field plus `azure.tenant_id` and the
  data-plane application id that acceptance suites need to mint tokens.

Rejected: a separate `spi facts` command. A clean consumer-facing name, but a
third overlapping surface next to `status` and `info` with no content of its
own.

Rejected: endpoints inside the status envelope. One call instead of two for
consumers, but it duplicates `info`'s contract and drags secret-reference
rendering into what should stay a health probe.

Rejected: consumers read `kubectl get kustomizations -o json` directly. No CLI
change at all, but it freezes internal object names and labels into eight
external repositories.

## Consequences

- The envelope is a compatibility contract: renaming a field is a breaking
  change for fork CI, hence the `apiVersion` gate.
- A stuck `maintenance` flag blocks fork deploys until an operator clears it;
  the workflows must clear it on failure paths, not only on success.
- The renderer and the JSON path share one collector in `src/spi/status.py`,
  so the human table and the machine answer cannot disagree.
- `spi status --json` reports Flux convergence, not application correctness; a
  service can be Ready and still failing its acceptance suite. Probes remain
  the ops workflows' job.
