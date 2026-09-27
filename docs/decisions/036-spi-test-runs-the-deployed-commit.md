# ADR-036: `spi test` Runs the Deployed Commit's Own Suites

## Context

A service fork declares its suites in `.spi/service.yaml`, and the
`Azure/osdu-spi` template owns the resolver that binds a declaration to an
environment (template ADR-040) and the lane that runs every suite during a
borrow (template ADR-041). The template builds `<package>-acceptance:sha-<12>`
from the same commit as the service image, amd64 only, with the descriptor
baked in and the resolver left out. Running a suite against a standing
environment outside that lane needs one answer only the stack holds: which
commit each service runs. For a fork canonical the image lock records it as
the `sha-<12>` tag (ADR-033). Three constraints shape the command. The
template keeps resolution out of the CLI, because a descriptor and an
installed CLI release drift apart. GHCR retention prunes `sha-*` versions
after 30 days. The resolver's precedence lets an explicit process variable win
over every fact.

## Decision

`spi test <service>` runs one named suite of the commit the environment runs,
with the descriptor, resolver, and verdict script that commit shipped. The CLI
supplies facts, bearers, and the target; it never reads the descriptor itself.
The command lives in `src/spi/cli.py` with its engine in `src/spi/testing.py`.

- **The pair is captured at resolution.** When canonical resolution records
  a fork image (ADR-033), it also reads the digest of
  `<package>-acceptance:sha-<12>` at the same commit and records it as
  `<SERVICE>_ACCEPTANCE_DIGEST` in the lock, or leaves the key absent when
  the fork published no acceptance image. The `sha-<12>` tag is mutable and
  the template publishes the acceptance image without provenance or a
  revision label, so the recorded digest, read in the same pass that ties
  the service digest to its commit, is the pair's identity from then on, as
  the service digest is the service's (ADR-031). A rebuild of the same
  commit caught between the two reads can record the pair from another build
  of that commit; the pair promises the commit, not the build. Pins never
  write the key.
- **Paired mode is the default.** The suite image is the lock's repository
  with `-acceptance` appended, pulled by the recorded digest for
  `linux/amd64` and run with `docker run`. Pairing needs a fork canonical,
  the only lock entry that is both unborrowed and tied to a fork commit; a
  community canonical, an operator digest pin, and a fork canonical with no
  recorded acceptance digest refuse with `unpaired`, naming `--source`.
- **The commit's own machinery.** The CLI resolves the lock's short SHA to
  the full commit through the GitHub commits API and refuses unless the
  returned SHA starts with those 12 characters, since that endpoint also
  accepts a branch or tag of the same name. It then fetches
  `.spi/service.yaml`, `.github/actions/acceptance-resolver/resolve.py`, and
  `.github/actions/acceptance-image/suite-verdict.py` at the full SHA. The
  source repository is the service's entry in the `canonical-sources`
  projection, read through the same check against the trusted roster that
  canonical resolution applies (ADR-033). The resolver runs under the CLI's
  own interpreter, since it is standard library only, in `run` mode with the
  facts from `spi info`. The CLI reads the resolver's report
  (`report_schema` 1) for `test_dir`, `maven_arguments`, and
  `timeout_minutes`, and refuses any other schema.
- **Checkout mode, `--source <path>`.** The checkout's own descriptor,
  resolver, and verdict script bind the suite, and native `mvn` runs in
  `<path>/<test_dir>` with `--settings
  <path>/.mvn/community-maven.settings.xml` when that file exists, the same
  invocation as the image's entrypoint. The run is labelled `matched` when
  the checkout's HEAD equals the deployed commit and the tree is clean,
  `unmatched` otherwise, and `unpaired` when the environment runs a
  community image. In either mode a descriptor whose `service.name` is not
  the named service refuses the run. Checkout mode is the only mode for a community-sourced
  service and the native-speed loop on arm64.
- **Allowlisted environment.** The resolver process receives `PATH`, the
  three `RESOLVER_*` bearers, and each `--set NAME=VALUE` override, nothing
  else, so a variable exported for another environment never wins over a
  fact. Native `mvn` receives the resolved map over a fixed base (`PATH`,
  `HOME`, `JAVA_HOME`, `MAVEN_OPTS`, `LANG`, `TMPDIR`). On Windows both also
  receive the system variables a process needs to start, and `mvn` the user
  profile variables; the env file is
  parsed as `NAME=VALUE` data and never sourced. The container receives the
  env file and `SUITE_DIR`, set to the report's `test_dir`, which is how the
  image's entrypoint selects a baked suite; nothing else.
- **The lane's callers.** The CLI mints the deploy, member, and no-access
  bearers through the path `spi token` uses, for `azure.token_audience`. An
  identity the environment does not provision leaves its variable unset, and
  only a suite that binds it fails as env-not-ready, as in the lane.
- **Guards before, verdict after.** The run refuses while `spi status`
  reports the environment not deployable (ADR-030), and refuses with
  `service_borrowed`, naming the run id, while an ephemeral pin holds the
  service. `spi test` holds no lease, so a lane can pin the service mid-run:
  the CLI reads the service's lock entry, its running pods' digest, and its
  Deployment's rollout revision before the suite and again after it, and
  discards the result as `target_changed` when the entry or the revision
  changed or a pod was not running the lock's digest. A borrow and its
  restore leave the entry as it was, but each advances the revision. The
  suite runs under the descriptor's `timeoutMinutes`. The verdict is the
  commit's `suite-verdict.py` over the Surefire and Failsafe reports: a zero
  exit, at least one test not skipped, and no failures or errors. A
  selection that runs no test fails.
- **Exit codes and envelope.** 0 passed; 3 the suite ran and failed; 2 not
  run or discarded because the environment is not deployable, a binding is
  not published yet, the service is borrowed, or the target changed during
  the run; 1 not run for any other reason (descriptor rejected, no pair,
  pair pruned, commit mismatch, facts contradiction, Docker or Maven
  missing). `--json` prints the `{outcome, code, detail}` envelope other
  service commands print, with the suite, mode, image or commit, provenance
  label, and test counts beside it.
- **Arguments pass through.** `--suite <name>` selects a declared suite,
  `acceptance` by default. Tokens after `--` replace the suite's
  `mavenArguments` and reach Maven as argv, never as a shell string.
- **A pruned pair is reported, not repaired.** When the recorded acceptance
  digest no longer pulls, the run fails with `pair_pruned`, and the fixes
  are `spi service refresh <service>` onto a newer commit or `--source` at
  the deployed one.

Rejected: bake the resolver into the acceptance image. Resolution would run
inside the container and one pulled artifact would carry everything, but the
Maven base image has no Python runtime, the change waits on a template
release, and every image already published lacks it.

Rejected: a resolver vendored into the CLI. No GitHub read per run, but two
resolvers drift, and the CLI's release cadence would own test semantics
(template ADR-040).

Rejected: name the pair by its `sha-<12>` tag at run time. It needs no lock
change, but the tag is mutable and carries no provenance, so a repush would
put an image nothing ties to the commit behind a pass.

Rejected: hold a lease that pinning honors. It would keep a lane off the
service for the whole suite, but it adds a lock-side protocol every pinner
must follow, and a before-and-after comparison catches the same interleaving
at no cost to the lane.

Rejected: pass the developer's full environment to the resolver. It keeps the
resolver's explicit-variable escape free of flags, but a stale export silently
overrides a published fact.

Rejected: run the suite in-cluster as a Job. Cluster networking is closer to
production, but the deploy identity holds no create verbs (ADR-032) and a Job
needs its own log and cleanup handling.

## Consequences

- Paired mode on Apple Silicon runs the amd64 image under emulation. The
  partition acceptance image is 1.0 GB and pulled in 49 s; its 11 tests ran
  in 41 s under emulation against 24 s cold and 16 s warm natively from a
  checkout of the same commit. A larger suite scales that ratio. Checkout
  mode is the fast loop and trades the image's JDK and warmed repository for
  the host's.
- A paired run executes fork code on the developer's host: the resolver and
  verdict script, from the repository the environment trusts for that
  service, at the deployed commit. The same repository's image already runs
  in the cluster under the environment's workload identity.
- Each paired run reads GitHub: one commit lookup and three file fetches.
  Without `gh` authentication or `GH_TOKEN`, the anonymous limit is 60
  requests an hour.
- The template's retention job ages a package version from its first push,
  and an acceptance image whose suites did not change keeps one version
  across commits. Once `main-snapshot` moves off that version, the next
  weekly prune can delete it while the service image it pairs with is days
  old. A recorded digest keeps no pruned version alive; `pair_pruned` names
  the state.
- Canonicals recorded before the acceptance digest joined the lock have no
  pair until the next `spi service refresh` or `--refresh-images` resolves
  them again.
- Suites write into the environment (partitions, records, legal tags) as the
  lane's do. Only the target service's borrow is guarded; a lane borrowing a
  sibling service can run at the same time.
- The acceptance image pins suite source and a warmed local repository, not
  version-range metadata, so every run fetches range metadata (the
  `io.cucumber` ranges `os-core-test` pulls) from the repositories its
  settings name, the community Maven repository first. On a network where
  that host accepts the connection and never answers, Maven waits past its
  connect and request timeouts and no test starts before the suite's
  `timeoutMinutes` ends the run; `mvn -o` fails at once because the warmed
  repository holds no range metadata. The run reports the timeout; the CLI
  does not rewrite the suite's repositories.
- Descriptor, resolver, suites, and image come from one commit in the lane
  and in paired mode. The lane knows its image by the digest it pushed in
  that run; paired mode knows it by the digest resolution recorded, so both
  rest on what the registry reported at one moment for that commit's tag.
- Paired mode needs Docker and checkout mode needs a JDK 17 and Maven on the
  host; neither is part of `spi check`.
