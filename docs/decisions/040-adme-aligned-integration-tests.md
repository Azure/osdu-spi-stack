# ADR-040: ADME-Aligned, Secret-Less Integration Tests on the Deploy Lane

**Status**: Accepted

## Context

The deploy lane (`validate.yml` → build → deploy → integration-test) needs a
real integration suite per service so a PR proves the service works against the
live SPI Stack, not just that it compiles. Two questions had to be answered: (1)
*which* tests run, and (2) *how* the suite authenticates, given the SPI Stack is
managed-identity-only and carries no test service-principal secret.

Microsoft's ADME (Azure Data Manager for Energy) runs the authoritative
Azure-provider OSDU suites. `oep-deployment-resources/IntegrationTests/OneBranch/ServiceITsProd.yml`
shows that for each service ADME builds the `testing/<svc>-test-core` reactor
module and runs `testing/<svc>-test-azure`, passing a single bearer token under
the canonical name `INTEGRATION_TESTER_ACCESS_TOKEN`. That token is minted
**without a secret** (`GenerateTokenWithoutSecret.yml` → `az account
get-access-token --resource <clientId>`), i.e. federated, exactly the mechanism
the SPI deploy lane already uses for its MSI.

The community/open-source `<svc>-test-azure` modules our forks sync are *older*
than ADME's internal forks and lack the federated-token support, so they fail on
a managed-identity-only stack.

## Decision

Run the same modules ADME runs (`testing/<svc>-test-azure`, with
`<svc>-test-core` built via the testing reactor) and authenticate every service
suite with the single federated `INTEGRATION_TESTER_ACCESS_TOKEN`, minted as the
service's CI managed identity (`azure/login` OIDC → `az account
get-access-token --resource $AAD_CLIENT_ID`). No test service-principal secret
is introduced; the stack stays secret-less and the suite exercises the
per-identity entitlements model (ADR-016) because the CI MSI is a real
entitlements member seeded by `spi onboard`.

Service-specific alignment, each verified byte-identical against the ADME fork
(`OpenEnergyPlatform/OSDU-Legal`, `OSDU-Storage`, …):

- **storage** — `storage-test-azure`'s `AzureTestUtils` consumes
  `INTEGRATION_TESTER_ACCESS_TOKEN` for its entitled caller and
  `NO_DATA_ACCESS_TESTER_ACCESS_TOKEN` for its negative authorization caller, so
  no source change is required. The unrelated
  `should_returnRecordsAfterCrsConversion*` cases remain independently scoped.
- **legal** — `legal-test-azure`'s `AzureLegalTagUtils.accessToken()` is
  forward-ported from ADME so it prefers `INTEGRATION_TESTER_ACCESS_TOKEN` and
  only falls back to the SP client-credentials flow when no token is supplied.
  ADME also comments out the `uploadTenantTestingConfigFile()` calls in
  `CreateLegalTagApiAcceptanceTests` ("the older config is not provided hence
  commenting"); the deployed legal service already carries the COO config, so the
  test needs no blob credential. Adopting both deltas removes the only dependency
  on a tester SP secret. The three tests ADME excludes (subscription-message
  delete, client-consent first-party create, invalid-expiration validate) are
  excluded identically.
- **maven invocation** — the lane runs a single reactor pass
  (`mvn -pl <svc>-test-azure -am verify`). Because `-am` also runs the upstream
  `<svc>-test-core` module's `test` phase, and a `-Dtest=!…` exclusion puts
  surefire into "specified tests" mode, the core module (abstract base classes,
  no concrete tests) fails with *"No tests matching pattern were executed"*. The
  goal therefore carries `-Dsurefire.failIfNoSpecifiedTests=false`
  (and `-Dmaven.surefire.useFile=false`, matching ADME's console output). ADME
  side-steps this by building core (`install`) and azure (`verify`) in separate
  invocations; the single-reactor pass is equivalent with the flag.

The token env var name, maven goal, maven profile, and per-attempt timeout are
all driven by repo variables (`ROOT_TOKEN_ENV`, `MAVEN_GOAL`, `MAVEN_PROFILE`,
`IT_TIMEOUT_MINUTES`/`IT_MAX_ATTEMPTS`) so a service is configured without
editing the workflow.

For negative authorization coverage, each Stack environment owns one shared
UAMI named `spi-ci-no-data-access`. ADME makes this token optional and wires it
only into services whose active test suites contain negative-access cases.
Current ADME pipelines opt in Register, Secret, EDS-DMS, Partition, File, and
Storage; Workflow uses the same identity concept under `NO_ACCESS_USER_TOKEN`.
SPI currently opts in Storage because its deployed profile is the one with a
skipped negative-access test. Other services opt in when their ADME-aligned
profile is enabled, either by an existing `NO_DATA_ACCESS_TOKEN_ENV` repository
variable or `spi onboard --no-data-access-token-env`.

`spi onboard` creates or reuses the identity only for an opted-in repository,
but does not assign Azure RBAC and does not seed it into any OSDU entitlements
group.

For an opted-in repository, onboarding writes the identity's client ID,
principal ID, name, and the target token environment variable as non-secret
repository variables. The default is
`NO_DATA_ACCESS_TESTER_ACCESS_TOKEN`; Workflow uses
`NO_ACCESS_USER_TOKEN`, matching ADME's current Workflow acceptance-test
contract. Other services neither receive a federated credential nor mint the
second token.

The shared identity uses the same exact GitHub OIDC subjects as the service
identity: pull request plus `main`, `fork_integration`, and `fork_upstream`.
`spi onboard` reads GitHub's actual `sub_claim_prefix`, including immutable
owner/repository IDs where applicable. Azure permits at most 20 federated
credentials per UAMI, so one shared identity currently supports at most five
opted-in repositories. That covers the current SPI need; a broader service fleet
must shard the identity or adopt a generally available flexible-federation
mechanism.

The integration-test action requests a second GitHub OIDC assertion and uses a
temporary `AZURE_CONFIG_DIR` to authenticate the shared UAMI without replacing
the service UAMI's Azure CLI session. It mints an ARM-audience token, masks it,
exports it only under the opted-in profile's token env, and deletes the
temporary Azure CLI state before Maven starts. No long-lived test credential is
stored.

## Consequences

- Both legal and storage run their full ADME suites green on the SPI Stack with
  no test secret: storage 126/0F/0E, legal 107/0F/0E/1-skip, every API call
  authorized via the federated CI-MSI token.
- Test source that we forward-port from ADME (legal `AzureLegalTagUtils`,
  `CreateLegalTagApiAcceptanceTests`) is a divergence from the community
  `fork_upstream`. It is kept byte-identical to the ADME fork, not custom, so a
  future upstream that adopts the same change merges cleanly; until then it must
  survive template/upstream sync.
- Excluding the crs-conversion storage cases means a regression in storage's
  frame-of-reference conversion path would not be caught here. The exclusion is
  documented as a scoped-stack dependency gap, not a permanent decision — once
  `crs-conversion` is deployed the exclusion should be removed.
- Storage's `TestRecordAccessAuthorization` now distinguishes authentication
  from authorization by requiring the shared identity's valid bearer token and
  asserting the service returns HTTP 403.
- The shared UAMI supports five opted-in repositories with the current four
  exact subjects per repository. Additional negative-test profiles require
  sharding or a generally available flexible-federation mechanism.

Rejected alternatives:

- **Provision a real tester service principal + secret for legal's COO blob.**
  Tried and discarded: it works but adds an SP secret and RBAC the rest of the
  stack deliberately avoids, and ADME itself does not do it — ADME comments the
  blob upload out instead. Matching ADME is both cleaner and secret-less.
- **Keep the SPI-custom acceptance harness instead of the ADME modules.** Less
  coverage and drifts from what ADME validates; contradicts the directive to run
  the same suites ADME runs.
