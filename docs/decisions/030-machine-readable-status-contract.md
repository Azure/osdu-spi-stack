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

- Envelope: `apiVersion: spi.osdu.dev/v1`, `ready`, `deployable`, a typed
  `reason` naming the first deployability blocker when `deployable` is false
  (a non-ready Kustomization, the `maintenance` flag, or a missing deploy
  record), `suspended`, `maintenance`, Kustomization counts with a not-ready
  list, `stack` (ref, resolved commit, deploy timestamp, CLI version,
  profile), `images` (branch, resolved-at, count, pinned services), and
  `baseUrl`.
- `ready` and `deployable` answer different questions. `ready` is Flux
  convergence: each Kustomization reports `Ready=True`, the same predicate
  `scripts/wait_for_flux_ready.sh` polls. `deployable` is `ready` with
  `maintenance` unset and a deploy record present; `spi service pin`
  (ADR-031) enforces the same rule itself, refusing while `maintenance` is
  set or the record is absent.
- Exit codes: 0 deployable, 2 not deployable with the typed `reason`, 1
  unreachable or guard failure. Fork CI gates on the exit code alone. The
  lifecycle workflows, which run while `maintenance` is set, read `ready`
  from the JSON instead of the exit code.
- The deploy record is written twice, for two audiences: RG tags
  (`spi-stack-version`, `spi-deployed-utc`) readable with no cluster access,
  and a `spi-deploy-record` ConfigMap in `osdu-flux` (ADR-019) holding the
  ref, the resolved commit from `GitRepository.status.artifact.revision`, the
  CLI version, profile, and timestamp.
- The ConfigMap also carries the `maintenance` flag. Status surfaces it and
  derives `deployable`; when it is set and cleared, and the fail-closed rules
  around it, are ADR-029's ruling.
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

Rejected: fold `maintenance` into `ready`. One field for consumers, but the
ops workflows need the convergence answer while `maintenance` is set, and
collapsing the two would take it from them.

## Consequences

- The envelope is a compatibility contract: renaming a field is a breaking
  change for fork CI, hence the `apiVersion` gate.
- Environments provisioned before the deploy record existed refuse pins until
  a re-run `spi up` writes one; fail-closed is chosen over a bypass flag.
- The renderer and the JSON path share one collector in `src/spi/status.py`,
  so the human table and the machine answer cannot disagree.
- `spi status --json` reports Flux convergence, not application correctness; a
  service can be Ready and still failing its acceptance suite. Probes remain
  the ops workflows' job.
