# ADR-038: Local Builds Pin from the Environment Registry

## Context

A pin names a GHCR digest a fork's workflow published (ADR-031), so a change
reaches an environment only after a fork runs the template's build with a
GitHub App and its own package. A contributor holding a plain fork and a
personal environment has neither: the fork's workflows are off until adoption,
and a hand-pushed GHCR package starts private and unpullable. Each environment
already provisions a container registry (`infra/modules/acr.bicep`) that
nothing writes to, and the canonical service Dockerfile copies a JAR built
outside it, so no Maven runs in the image build.

## Decision

`spi build <service> --source <checkout>` builds the checkout into the
environment's own registry, and `spi service pin <service> --source <checkout>`
builds, pins, and waits for the rollout. The pin is an operator pin whose
origin is `local`.

- **One repository per service.** The image is
  `<registry>/local/<service>`, tagged `sha-<12>` or `sha-<12>-dirty` and
  pinned by digest. `pin_service_image` in `src/spi/pins.py` accepts that
  repository on the connected environment's registry and no other non-GHCR
  host; the registry is the one container registry in the resource group
  `spi-cluster-config` records.
- **The host builds the JAR; an ACR task builds the image.** `src/spi/build.py`
  runs Maven with the template's profiles (`clean install -P core,azure
  -DskipTests`), finds the Spring Boot JAR in the order the template's
  `resolve-jar.sh` searches, and stages a context holding `build/` and the JAR.
  `az acr run` executes a two-step task with `DOCKER_BUILDKIT=1`, because the
  `az acr build` quick build runs the classic builder and rejects the
  Dockerfile's `ADD --checksum`. The platform is `linux/amd64`.
- **The checkout is checked, not trusted.** The descriptor's `service.name`
  must equal the requested service, and the checkout must be a git tree. The
  pin records the commit in `source_sha`, suffixed `-dirty` when the tree had
  uncommitted changes before or after Maven ran and `-prebuilt` when the JAR
  is one Maven did not write in the same run, as under `--skip-maven`, since
  git cannot date it. Only a clean tree Maven built in the same run reads as
  its commit.
- **No new annotation field.** `origin`, `source_sha`, and the absent
  `ephemeral` marker carry the whole record. A CLI that predates local pins
  decodes one as an operator pin and leaves it standing.
- **The cluster pulls as its kubelet identity.** `infra/aks.bicep` outputs the
  kubelet identity and `infra/modules/rbac.bicep` grants it AcrPull on the
  registry. A pin refuses, before the lock changes, while that assignment is
  absent, and names `spi up` as the step that makes it.
- **A local pin never displaces a borrow.** The pin refuses while the
  service's live pin is ephemeral; a fork workflow's run owns the service
  until its restore. Reset, refresh, and the stale sweep treat a local pin as
  any operator pin: reset restores the captured canonical, refresh skips the
  service, and the sweep ignores it.
- **`spi test --source` labels against the build.** A checkout at the pinned
  commit with a clean tree is `matched`; a dirty or prebuilt build is
  `unmatched` for every checkout.

Rejected: push the hand-built image to the contributor's GHCR namespace. No
registry in Azure is involved, but the package must be made public by hand,
the push needs a token scope the contributor's CLI session does not carry,
and an arm64 laptop needs an emulated or cross build.

Rejected: build with the local Docker daemon and push to the registry. Reuses
the layer cache between builds, but requires Docker for a path that otherwise
needs only Maven and `az`, and reintroduces the cross-platform build.

Rejected: grant the kubelet identity from the CLI at the first local pin.
Works on a standing environment without a redeploy, but puts a role
assignment outside the templates ADR-035 audits, and a later `spi up` that
declares the same assignment under its own name fails on the duplicate.

Rejected: refuse local pins on a declared environment. Protects the shared
environment from an unreviewed image, but an operator there can already pin
any public GHCR digest (ADR-031), and a personal environment is declared too.

## Consequences

- An environment provisioned before the kubelet grant refuses local pins
  until `spi up` runs once on a release that carries it. Where the kubelet
  identity already holds AcrPull on the registry under another assignment
  name, as `az aks update --attach-acr` leaves it, that `spi up` fails on the
  duplicate until the earlier assignment is removed.
- Schema's loader is not built. A local schema pin releases a loader pinned
  by an earlier merge request or run and runs the canonical loader beside the
  local service image.
- Unit tests do not run in the default build, and the profiles are fixed at
  `core,azure`. Arguments after `--` on `spi build` replace the Maven
  arguments; `spi service pin --source` takes none.
- Nothing prunes `local/` repositories. A dirty rebuild moves its tag and
  leaves the earlier manifest untagged in a Basic registry.
- The build needs registry write access and a kube context, which the fork
  deploy identity lacks (ADR-032); fork CI cannot place a local pin.
- A contributor with a plain fork proves a change on a personal environment
  with `spi service pin --source` and `spi test --source` before the pull
  request, without adopting the fork.
