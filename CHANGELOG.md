# Changelog

All notable changes to the `spi` CLI are documented here. Versions follow
[Semantic Versioning](https://semver.org/). Release notes for each tag are
also auto-generated from conventional commits and attached to the
corresponding [GitHub Release](https://github.com/Azure/osdu-spi-stack/releases).

## [Unreleased]

### Added
- `spi service pin <service> --mr <iid>` pins one service to the image its
  OSDU merge-request pipeline built (source branch at the MR head commit),
  patched into the live `osdu-image-lock` with provenance and the canonical
  image recorded in a lock annotation. `spi service reset` restores the
  canonical image; `spi service list` shows active pins. Pinning `schema`
  pins the paired loader image when the MR built one, and
  `spi reconcile --refresh-images` and `spi up` preserve active pins and
  name them instead of silently reverting the experiment (ADR-017).
- `spi up --profile bare` deploys infrastructure and activates GitOps
  only: Flux reconciles empty stack and ingress trees while the CLI
  bootstrap seeds namespaces, secrets, the `osdu-config` ConfigMap, and
  the Workload Identity ServiceAccount. `--ingress-mode` and `--dns-zone`
  are rejected with `bare`. Use it for Bicep, Workload Identity, or RBAC
  iteration, then re-run `spi up` with `minimal` or `core` to add
  workloads (ADR-021, issue #42).

### Changed
- Local (key/SAS) authentication is now disabled on every Cosmos DB (Gremlin
  and per-partition SQL) and Service Bus account: `disableLocalAuth: true` is
  set in Bicep rather than left to a tenant policy (ADR-023, issue #44).
  Because `listKeys()` is rejected once local auth is off,
  `graph-db-primary-key` is no longer written, the per-partition key/connection
  Key Vault secrets (`{p}-cosmos-primary-key`, `system-cosmos-primary-key`,
  `{p}-cosmos-connection`, `{p}-sb-connection`) now carry the literal
  `DISABLED`, and the `serviceBusDisableLocalAuth` parameter is removed.
  Services reach these accounts through Workload Identity data-plane roles.
  Community OSDU images that still read these keys/SAS require
  Workload-Identity-capable custom images, tracked separately.
- Airflow 2.10.5 → 3.2.2 (chart 1.16.x → 1.22.x, single-engine).
  The webserver is replaced by `airflow-api-server` (UI + `/api/v2` + task
  execution API) and DAG parsing moves to a standalone dag-processor;
  routes and ReferenceGrants now target the new service. All Airflow
  signing material (`api-secret-key`, `jwt-secret`, `fernet-key`) is
  CLI-seeded in `airflow-api-credentials` so Flux reconciles never rotate
  keys. Deploy fresh (`spi down` / `spi up`); pre-Airflow-3 environments
  are not supported.

### Fixed
- `spi down` now removes the kubeconfig entries `spi up` merged in, instead of
  leaving a context pointing at a deleted cluster. Two gates guard the removal:
  the resource group must be confirmed gone, because `az group delete
  --no-wait` returns on acceptance and an accepted delete can still fail; and
  the context's server hostname must match the cluster's API server FQDN, read
  before the group goes away. Cluster names repeat across subscriptions, so
  both `spi up --env dev1` deployments write a `spi-stack-dev1` context, and
  matching on the name alone would strip the surviving cluster's credentials.
  The kubeconfig is re-read after the context is removed, so the cluster and
  user entries go only when no context that survives the delete references
  them, `current-context` is cleared only if it named the removed context and
  nothing took its place, and an identity that cannot be established leaves
  the kubeconfig untouched.
- Ingress address discovery now scans LoadBalancer Services in the supported
  Istio namespaces instead of requiring one hardcoded Service name. `spi up`
  therefore records the available address in `GATEWAY_IP`, and `spi info`
  reuses the same deterministic lookup (issue #85).
- Azure ingress now binds to the AKS managed Istio LoadBalancer Service and
  stamps the Azure DNS label onto it through Flux (`spi-ingress-dns-label`,
  a server-side-applied partial manifest owning only the annotation). The
  CLI's imperative `kubectl annotate` is removed: the
  `aks-managed-protect-system-namespaces` admission policy denies it, and
  the node-resource-group deny assignment blocks patching the public IP,
  so the exempt Flux controllers are the one permitted writer. This reuses
  the existing public IP so the deterministic FQDN resolves and ACME
  HTTP-01 can issue the certificate (ADR-026, issue #82).
- The Azure-mode DNS label derives from the env name plus the
  per-deployment name suffix (`<env>-ingress-<suffix>`), matching every
  other globally unique resource name. The prior `<cluster>-ingress` label
  is a region-global name that can sit reserved by an unreachable resource
  (`DnsRecordIsReserved`), leaving the FQDN permanently unassignable.
  Legacy unsuffixed deployments keep their existing label.
- The `spi-gateway` Gateway is rendered by exactly one Flux Kustomization.
  The profile-level `spi-gateway` and the ingress-level `spi-gateway-tls`
  both built `software/components/gateway`, so each reconcile reverted the
  other and the HTTPS listener plus `spec.infrastructure.annotations` never
  survived. The ingress tree is now the Gateway's sole renderer:
  every non-bare ingress mode declares one `spi-gateway-tls` Kustomization,
  pointed at its TLS overlay or, for `ip`, at the base component. Legacy
  inventories are retained as non-pruning orphan handoffs for one rollout
  (ADR-025, issue #81).
- The shared `bitnami` HelmRepository is no longer declared twice. It moved to
  `software/components/helm-sources`, owned by the new `spi-helm-sources`
  Kustomization, which Redis and ExternalDNS both depend on. ExternalDNS
  therefore waits on the source it needs instead of on Redis's runtime health
  (ADR-025).
- `spi update` now refuses the unsafe in-process `uv tool install --force`
  replacement path on Windows and prints the equivalent command to run from a
  new terminal, preventing orphaned `spi.exe` launchers with missing package
  environments (issue #70).
- Local `spi up` now resolves the deployer object ID from the cached ARM token
  before creating any Azure resource, so Key Vault Secrets Officer is granted
  without requiring `SPI_DEPLOYER_OID` or Microsoft Graph access.
- Returning from `minimal` or `core` to `bare` now deletes Redis cache PVCs
  and their `Delete`-policy Azure disks. Scaling replicas down also removes
  their unused claims; rolling upgrades retain active claims.
- Native Windows can run `spi` when CLIs such as Azure CLI are installed as
  `.cmd`/`.bat` batch shims (issue #49, ADR-024). Every process the CLI
  launches goes through `spi.shell.run_process`: the program resolves
  through `PATHEXT`, and a batch shim is launched via an explicit `cmd.exe`
  command line with every argument escaped, so values containing CMD
  metacharacters, `%NAME%` expansion syntax, quotes, or whitespace reach the
  tool exactly as written. The guarantee covers standard `%*`-forwarding
  shims; an argument containing a newline or NUL is reported as a normal
  command failure (without echoing the value) instead of being silently
  corrupted. `spi check` no longer needs `shell=True`, and the Bicep compile
  harness uses the same launcher.
- `spi info --show-secrets --json` no longer emits credential values
  (CodeQL `py/clear-text-logging-sensitive-data`): JSON output carries
  secret references (`namespace/name#key`) since it is the form most
  likely to end up in logs or CI artifacts; the interactive table remains
  the only place values render.
- `OSDU_AIRFLOW_URL` on the workflow service pointed at a nonexistent
  `airflow-web` service; it now targets `airflow-api-server`. (The
  workflow service's Airflow 3 API client is still pending upstream in
  the community `master` images.)
- HTTPS ingress never terminated on AKS Automatic: the gateway TLS overlays
  declared their cert-manager Certificates in `aks-istio-ingress`, where the
  AKS-managed protect-system-namespaces policy denies cert-manager's status
  writes; issuance stalled silently with every Kustomization Ready and :443
  refusing connections. Certificates now issue into `platform` and reach the
  Gateway's listeners via ReferenceGrants (ADR-022). The smoke workflow
  gained an HTTPS-handshake probe so a dead TLS path fails CI.

## [0.2.1] - 2026-07-24

### Fixed
- Deployer object-ID resolution no longer requires a Microsoft Graph token.
  The cluster-admin RBAC grant read the signed-in principal's object ID via
  `az ad signed-in-user show` / `az ad sp show`, both of which request Graph
  tokens that Conditional Access token protection can refuse to issue
  (AADSTS530084) even when ARM access works, failing `spi up` after AKS
  provisioning. The OID is now decoded from the `oid` claim of the cached
  ARM access token; the Graph lookups and the `SPI_DEPLOYER_OID` override
  remain as fallbacks.

## [0.2.0] - 2026-07-24

### Added
- `--profile minimal`: deploys the middleware substrate only (operators,
  cert-manager, trust-manager, Gateway, Elasticsearch, Redis, PostgreSQL,
  Airflow, CA bundles) and stops below layer 5, so no OSDU services are
  installed. Layers 0a-4b are identical to `core` (ADR-021).
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
  Azure estate and then failed to reconcile (ADR-021).

## [0.1.0] - 2026-07-24

### Added
- Per-environment 5-char random suffix on globally unique Azure resource
  names (storage, Key Vault, ACR, Cosmos, Service Bus). Persisted as the
  `spi-name-suffix` tag on the resource group so subsequent runs reuse it;
  legacy (pre-suffix) deployments keep unsuffixed names. (`ee45a65`)
- Cosmos SQL and Gremlin data-plane role assignments for the OSDU managed
  identity; `serviceBusDisableLocalAuth` parameter (default `true`) with
  `minimumTlsVersion: '1.2'` on Service Bus namespaces (ADR-023).
- `system-cosmos-*` Key Vault secrets for system services and a
  per-partition blob record container.
- Kubelet-identity AcrPull grant (`kubeletIdentityObjectId`) and
  `deployerPrincipalType` parameter for human-vs-service-principal
  deployers.
- Opt-in Application Insights + Log Analytics provisioning behind
  `enableApplicationInsights` (ADR-020).
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
  pinned to `asm-1-30` (ADR-002).
- SPI-owned GitOps objects (GitRepository, Kustomizations, seed
  ConfigMaps/Secrets, image lock) moved from `flux-system` to the dedicated
  `osdu-flux` namespace; Flux extension multi-tenancy enforcement disabled
  (ADR-019).
- Workload node-placement label renamed `agentpool` -> `spi-pool`
  (ADR-018).
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

[Unreleased]: https://github.com/Azure/osdu-spi-stack/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/Azure/osdu-spi-stack/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Azure/osdu-spi-stack/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Azure/osdu-spi-stack/releases/tag/v0.1.0
