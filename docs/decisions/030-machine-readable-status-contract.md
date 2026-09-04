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
  list, `environment` (name, stack version, resolved commit, profile,
  deploy timestamp, CLI version), `images` (branch, resolved-at, count,
  pinned services), and `baseUrl`. `stack` repeats the version fields
  without the name and is kept one release for existing consumers.
- `environment` is built by one function from the deploy record and
  published unchanged by `spi info --json` too, so a fork job binding facts
  from `info` and gating on `status` reads one identity. The name is the
  `env` the environment was provisioned with (the declaration's `env` for a
  lifecycle-managed environment, the `--env` flag for a personal one); it is
  recorded because the kubectl context is client-side and renamable. Empty
  strings mean no deploy record. The human dashboards print the same block:
  `spi status` closes with it in the Summary panel, printed last so the
  verdict is what remains on a terminal after the tables scroll; `spi info`
  opens with name and profile beside the ingress mode; `spi service pin`,
  `verify` and `reset` name the environment in their confirmation.
- `ready` and `deployable` answer different questions. `ready` is Flux
  convergence: each gating Kustomization reports `Ready=True`, the same
  predicate `scripts/wait_for_flux_ready.sh` polls. Kustomizations labeled
  `spi-stack.gating: "false"` (seeding work such as `spi-osdu-legal`) stay
  visible in `kustomizations.notReady` with their typed reason but never
  flip `ready`: "ready" and "seeded" are separate signals (ADR-015).
  `kustomizations.total` and `kustomizations.ready` count every
  Kustomization, gating or not, so `ready` is not
  `kustomizations.ready == kustomizations.total`; read the boolean.
  `ready` is false when no gating Kustomization is visible at all, which
  reports `no_kustomizations` rather than vacuous success. `deployable` is `ready` with
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
  CLI version, profile, environment name, and timestamp.
- The ConfigMap also carries the `maintenance` flag. Status surfaces it and
  derives `deployable`; when it is set and cleared, and the fail-closed rules
  around it, are ADR-029's ruling.
- Endpoints, partitions, and non-secret Azure coordinates stay in `spi info --json`,
  which carries the same `apiVersion` field plus `azure.tenant_id`,
  `azure.data_plane_application_id` (the application id acceptance suites
  need to mint tokens), `azure.openid_issuer`, and `partitions[].legal_tag`.
- `azure.openid_issuer` is the OIDC v2.0 issuer URL, published explicitly
  rather than derived by consumers from the tenant id. It is an empty string
  until the cluster reports its tenant, so consumers treat present-but-empty
  as not yet available.
- `partitions[].legal_tag` is observed state and follows that same
  present-but-empty idiom: it names the default tag only once that
  partition's `legal-init` Job has succeeded, and is an empty string while
  seeding is pending, failed, or was never run. Because legal seeding is
  non-gating, an environment can be `ready` and `deployable` with the tag
  absent, so a consumer that needs a compliant tag gates on this field rather
  than on `deployable`. `partitions[].legal_tag_desired` always carries the
  configured name (ADR-015), for diagnosing a seed that has not landed.

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
