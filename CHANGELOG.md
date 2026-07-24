# Changelog

All notable changes to the `spi` CLI are documented here. Versions follow
[Semantic Versioning](https://semver.org/). Release notes for each tag are
also auto-generated from conventional commits and attached to the
corresponding [GitHub Release](https://github.com/Azure/osdu-spi-stack/releases).

## [Unreleased]

## [0.2.0] - 2026-07-24

### Added
- `--profile minimal`: deploys the middleware substrate only (operators,
  cert-manager, trust-manager, Gateway, Elasticsearch, Redis, PostgreSQL,
  Airflow, CA bundles) and stops below layer 5, so no OSDU services are
  installed. Layers 0a-4b are identical to `core` (ADR-024).
- `<mode>-minimal` ingress trees for each `--ingress-mode`, selected
  automatically by the `minimal` profile.
- `tests/test_profiles.py`: asserts every `Profile` x `IngressMode` pairing
  resolves to real trees with no unsatisfiable `dependsOn`.

### Changed
- HTTPRoutes split by scope: `software/stacks/osdu/routes/<tree>/` now has
  `middleware/` and `osdu/` subdirectories, reconciled as separate
  `spi-middleware-routes` and `spi-osdu-routes` Kustomizations. Middleware
  routing no longer depends on the OSDU services being present.
- `spi-middleware-routes` now depends on `spi-airflow`, which the previous
  combined route Kustomization never waited for despite routing to
  `airflow-webserver`.
- `profile` and `ingressMode` in `infra/flux.bicep` carry `@allowed`
  constraints, so an unbacked value fails at template validation instead of
  stalling Flux after the infrastructure is provisioned.
- `spi up --profile minimal` skips OSDU image-lock resolution; nothing
  consumes the ConfigMap on that profile.

### Removed
- `--profile full`. The value was accepted by the CLI but had no manifests
  behind it: it pointed Flux at a nonexistent
  `software/stacks/osdu/profiles/full` path, so a deploy provisioned the full
  Azure estate and then failed to reconcile (ADR-024).

## [0.1.0] - 2026-07-24

### Added
- Per-environment 5-char random suffix on globally unique Azure resource
  names (storage, Key Vault, ACR, Cosmos, Service Bus). Persisted as the
  `spi-name-suffix` tag on the resource group so subsequent runs reuse it;
  legacy (pre-suffix) deployments keep unsuffixed names. (`ee45a65`)
- Cosmos SQL and Gremlin data-plane role assignments for the OSDU managed
  identity; `serviceBusDisableLocalAuth` parameter (default `true`) with
  `minimumTlsVersion: '1.2'` on Service Bus namespaces (ADR-021).
- `system-cosmos-*` Key Vault secrets for system services and a
  per-partition blob record container.
- Kubelet-identity AcrPull grant (`kubeletIdentityObjectId`) and
  `deployerPrincipalType` parameter for human-vs-service-principal
  deployers.
- Opt-in Application Insights + Log Analytics provisioning behind
  `enableApplicationInsights` (ADR-023).
- `availabilityZones` parameter on the system pool (default all three
  zones) for regions with zonal capacity constraints.
- Retry with backoff on OSDU image-tag resolution against the community
  registry.
- Deployment tenant pinned into the kubeconfig exec environment so an
  inherited `AZURE_TENANT_ID` cannot break cluster authentication.
- `CODE_OF_CONDUCT.md` and `SUPPORT.md`; `CODEOWNERS` and
  `.github/copilot-instructions.md` updated with ownership and deployment
  notes. (`836a623`)
- Prime Copilot skill. (`480a9d8`)

### Changed
- AKS Automatic clusters now require Kubernetes >= 1.36; managed Istio
  pinned to `asm-1-30` (ADR-019).
- SPI-owned GitOps objects (GitRepository, Kustomizations, seed
  ConfigMaps/Secrets, image lock) moved from `flux-system` to the dedicated
  `osdu-flux` namespace; Flux extension multi-tenancy enforcement disabled
  (ADR-020).
- Workload node-placement label renamed `agentpool` -> `spi-pool`
  (ADR-022).
- Default deployment region changed from `eastus2` to `westus3`.
- Chart pins raised: cert-manager `v1.18.*`, trust-manager `v0.22.*`,
  CloudNativePG `0.29.*` (PostgreSQL image pinned to major 17).
- cert-manager leader election moved out of the protected `kube-system`
  namespace.
- Elasticsearch endpoints use the certificate-SAN-valid short service form
  (`elasticsearch-es-http.platform.svc`) and partition records set
  `elastic-ssl-enabled: true`.
- `rich` minimum bumped to `>=15.0.0`. (`72c73cb`)
- `ruff` dev requirement updated. (`0d427ef`)

### Fixed
- `az group create` no longer re-PUTs existing resource groups (which
  cleared the `spi-name-suffix` tag and caused resumed deploys to mint new
  resource names).
- Cluster-admin role-assignment verification uses the ARM REST API
  directly, avoiding Microsoft Graph calls that Conditional Access
  policies can block.
- Istio ASM revision detection reads the istiod deployment name instead of
  a namespace label that AKS does not set.
- Bicep templates are now bundled inside the installed wheel
  (`spi/infra/`) via hatchling `force-include`, with a source-checkout
  fallback in `paths.py`. Earlier wheels resolved `INFRA_ROOT` to a
  nonexistent path under `lib/pythonX.Y/infra/`, breaking `spi up` for
  every `uv tool install` user. (`ee45a65`)

[Unreleased]: https://github.com/Azure/osdu-spi-stack/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Azure/osdu-spi-stack/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Azure/osdu-spi-stack/releases/tag/v0.1.0
