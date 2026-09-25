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

`spi status --json` emits a versioned envelope and typed exit codes; Flux's
applied state supplies the running version, and a deploy record written at the
end of `spi up` records how the environment was last provisioned.

- Envelope: `apiVersion: spi.osdu.dev/v1`, `ready`, `deployable`, a typed
  `reason` naming the first deployability blocker when `deployable` is false
  (a non-ready Kustomization, the `maintenance` flag, a missing deploy
  record, a failed `entitlements-members` Job as `bootstrap_failed`, or
  the current one not yet Complete as `bootstrap_pending`),
  `suspended`, `maintenance`, Kustomization counts with a not-ready
  list, `environment` (name, stack version, resolved commit, profile,
  deploy timestamp, CLI version), `images` (branch, resolved-at, count,
  pinned service names, and a `pins` map carrying each pin's provenance),
  and `baseUrl`. `stack` repeats the version fields
  without the name and is kept one release for existing consumers.
- `environment.running` is what Flux has applied, read on every call and
  never from the record: `ref` and `commit` from the `osdu-spi-stack-system`
  GitRepository's `status.artifact.revision`, `release` from the applied
  `spi-stack-version` stamp, `version` as `vX.Y.Z` on a tag and
  `X.Y.Z+<12-char commit>` on a branch, and `converged` once every gating
  Kustomization's `lastAppliedRevision` equals the source revision. A branch
  environment advances on `spi reconcile` without a new record, so the flat
  record fields describe the last `spi up` and `running` describes now; a
  consumer choosing a wheel for the environment reads `running.release`.
  The dashboards print `running.version` as the environment's version, mark
  a rollout that has not converged, show the record as "Last spi up", and
  warn when the executing CLI is older than `running.release`, because pin
  and reconcile logic ships in the CLI.
- `environment` is built by one function from the deploy record and the
  running version, and published unchanged by `spi info --json` too, so a fork job binding facts
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
  `maintenance` unset, a deploy record present, and the
  `entitlements-members` Job the live init values name Complete for every
  partition; `spi service pin` (ADR-031) enforces the same rule itself,
  refusing while `maintenance` is set, the record is absent, or the
  members bootstrap has failed or not finished. Values written before the
  CLI listed members name no Job, so older environments are not gated.
- Exit codes: 0 deployable, 2 not deployable with the typed `reason`, 1
  unreachable or guard failure. Fork CI gates on the exit code alone. The
  lifecycle workflows, which run while `maintenance` is set, read `ready`
  from the JSON instead of the exit code.
- The deploy record is written twice, for two audiences: RG tags
  (`spi-stack-version`, `spi-deployed-utc`) readable with no cluster access,
  and a `spi-deploy-record` ConfigMap in `osdu-flux` (ADR-019) holding the
  ref, the resolved commit from `GitRepository.status.artifact.revision`, the
  CLI version, profile, environment name, and timestamp.
- The stack release travels with the tree it describes.
  `software/components/stack-version` renders a `spi-stack-version`
  ConfigMap in `osdu-flux` whose `version` is the release, every profile
  applies it, and release-please rewrites that line on each release through
  `extra-files` in `.release-please-config.json`. A commit between releases
  carries the last release's number, so the stamp names the release a tree
  descends from; the applied commit tells whether it is that release's tag.
- `spi-deploy-record` also carries the `maintenance` flag. Status surfaces it and
  derives `deployable`; when it is set and cleared, and the fail-closed rules
  around it, are ADR-029's ruling.
- Endpoints, partitions, and non-secret Azure coordinates stay in `spi info --json`,
  which carries the same `apiVersion` field plus `azure.tenant_id`,
  `azure.data_plane_application_id` (the `AAD_CLIENT_ID` resource services
  request service-to-service tokens for), `azure.token_audience` (the
  resource acceptance suites mint tokens for), `azure.openid_issuer`, and
  `partitions[].legal_tag`.
- `azure.token_audience` is `https://management.azure.com` unless an operator
  overrode `AAD_CLIENT_ID` to an app registration, in which case it is that
  id. The application id is not mintable when it names the platform's managed
  identity, which is the default, so the two facts are separate: a consumer
  reads the audience per run and never derives it from the application id.
  The override is detected by comparing `AAD_CLIENT_ID` with the client id on
  the `osdu/workload-identity-sa` annotation; when that annotation cannot be
  read the fact is the management audience, which the v1 issuer rule accepts
  on every environment.
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

Rejected: resolve the release at read time by comparing the applied commit
with the repository's tags. Exact for any commit, but every status read then
needs GitHub access, and an environment deployed from another repository
would be compared against the wrong tags.

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
