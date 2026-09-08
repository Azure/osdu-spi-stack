# Architecture

SPI Stack deploys the Azure provider of OSDU for development and testing. The
`spi` CLI provisions Azure infrastructure with Bicep and bootstraps Kubernetes;
Flux CD then reconciles the workload manifests.

Two defaults shape how to operate it:

- **Git updates are opt-in after deployment.** `spi up` verifies the requested revision and suspends the
  Git source before returning. Flux continues applying the cached revision, but
  does not fetch new commits while that source is suspended.
- **This is not a production configuration.** OSDU services share a managed
  identity and middleware credentials. Backup, disaster recovery, and
  per-service Azure access isolation are not provided by this stack.

## System boundaries

![CLI provisioning and Git reconciliation control workloads inside AKS; clients reach OSDU through the in-cluster gateway](diagrams/architecture.png)

The CLI runs on an engineer's machine or a CI runner. Git and Azure PaaS are
outside the cluster. Flux, the Istio ingress gateway, OSDU services, and middleware
run inside it. Provisioning and reconciliation are control paths, separate from
the API requests served by the workloads.

| Owner | Responsibility | Boundary |
|---|---|---|
| CLI and Bicep | Resource group, AKS, networking, identities, Azure PaaS, initial Kubernetes configuration, seed credentials, Flux activation | Re-run explicitly; Flux does not reconcile Azure infrastructure |
| Flux | Apply workload manifests and Helm releases in dependency order | Uses the cached Git revision when source fetching is suspended |
| Kubernetes controllers and operators | Schedule pods, maintain middleware clusters, issue certificates, distribute CA bundles | Continue running independently of Git polling |
| Operator | Decide when to fetch Git changes or refresh service images; diagnose failures and delete environments | A successful CLI exit is not an API-readiness check |

The bootstrap boundary has exceptions that matter during maintenance. The CLI
writes `osdu-config`, ingress and image-lock ConfigMaps, ServiceAccounts, seed
Secrets, and Istio identity policies. These are not all recreated from Git.
See the [deployment lifecycle](design/deployment-lifecycle.md) for the sequence
and [secret lifecycle](design/secret-lifecycle.md) for credential ownership.

## Where workloads run

There are three application namespaces, plus namespaces managed by AKS and Flux.

| Namespace | Workloads |
|---|---|
| `foundation` | ECK and CloudNativePG operators, cert-manager, trust-manager; ExternalDNS in `dns` mode |
| `platform` | Elasticsearch, Redis, PostgreSQL, Airflow, TLS certificates; Kibana in the TLS ingress profiles |
| `osdu` | OSDU APIs, partition/entitlements initialization, legal-tag seeding Jobs, schema loader |
| `osdu-flux` | SPI-owned Git source, Kustomizations, HelmReleases, bootstrap input ConfigMaps and credential seed |
| `flux-system` | AKS extension-owned Flux controllers |
| `aks-istio-system`, `aks-istio-ingress` | Managed mesh components and ingress; the `spi-gateway` Gateway is in `aks-istio-ingress` |

OSDU pods receive Istio sidecars. The platform middleware namespace does not.
The local [service Helm chart](../software/charts/osdu-spi-service/) supplies
security contexts, probes, and resource settings required by AKS Automatic
Deployment Safeguards. Karpenter NodePools separate platform and OSDU workloads.
SPI-owned GitOps objects live outside the protected `flux-system` namespace so
the CLI can seed their inputs before the extension starts. Flux applies
resources using its exempt controller identities, with multi-tenancy
enforcement disabled ([ADR-019](decisions/019-osdu-flux-gitops-namespace.md)).

## Stack profiles

Profiles select Kubernetes workloads, not a smaller Azure resource estate.
`spi up` still provisions the AKS and PaaS templates for each profile.

| Profile | Flux workload scope |
|---|---|
| `bare` | Empty stack and ingress trees; infrastructure and CLI bootstrap inputs remain |
| `minimal` | Operators, middleware, and CA bootstrap, without OSDU services or initialization Jobs |
| `core` | The middleware substrate plus OSDU services, partition/entitlements initialization, schema loading, and reference APIs |

`minimal` selects an ingress tree ending in `-minimal`; its `ip` combination
has no ingress routes or Gateway. Moving back to `bare` removes middleware
workloads and Redis volumes, but operator CRDs can remain. See
[ADR-021](decisions/021-middleware-only-minimal-profile.md).

## Data services and request flow

Azure PaaS holds OSDU operational data: Cosmos DB SQL for records and metadata,
Cosmos DB Gremlin for the entitlements graph, Storage for blobs and tables, and
Service Bus for events. Key Vault stores configuration values and credentials
that services resolve at runtime.

Elasticsearch, Redis, and Airflow's PostgreSQL database remain in-cluster for
different reasons. Azure AI Search is not compatible with the Elasticsearch APIs
used by OSDU. Managed Redis and PostgreSQL do exist; this dev/test stack chooses
co-location to limit latency and cost. Those trade-offs are recorded in
[ADR-003](decisions/003-in-cluster-middleware-scope.md).

A client request reaches the Istio gateway and is routed to an OSDU service.
The receiving sidecar validates the bearer token and projects the identity
headers expected by the Azure-provider service. Services resolve
partition-specific backends through the partition service. Asynchronous indexing
uses Service Bus: indexer-queue consumes events and invokes indexer, which writes
to Elasticsearch.

The core service set is partition, entitlements, legal, schema, storage, search,
indexer, indexer-queue, file, and workflow. The reference APIs are unit,
crs-conversion, and crs-catalog. Their configuration lives in
[`services/`](../software/stacks/osdu/services/) and
[`services-reference/`](../software/stacks/osdu/services-reference/).

![Partition and entitlements sit upstream of legal, schema, and storage; search, indexer, file, and workflow depend on storage; reference services stand alone](diagrams/service-dependencies.png)

Those runtime call dependencies are separate from the Flux rollout order,
which the [Flux guide](design/flux-reconciliation.md#dependency-ordering) owns.

## Environment and partition isolation

`--env` identifies an environment's resource group and cluster, for example
`spi-stack-dev1`. Globally named Azure resources also use a generated suffix
persisted in the resource group's `spi-name-suffix` tag, so repeat deployments
reuse the same names.

Each partition gets a Cosmos DB SQL account, Service Bus namespace, and Storage
account. The environment shares Gremlin, common Storage, Key Vault, and
in-cluster middleware. The first partition is the primary partition and hosts
the system database used by schema loading.

Partition-specific resources do not imply per-service access isolation. OSDU
workloads share one user-assigned managed identity. Workload Identity avoids
storing a client secret for that identity. Cosmos and Service Bus disable local
authentication, Storage disables shared-key access, and Bicep stores no usable
Azure keys or SAS connection strings. Community images that require those
credentials fail against the `DISABLED` placeholders until replaced by
Workload-Identity-capable images. Middleware passwords still exist in Kubernetes
and Key Vault. See
[identity](design/workload-identity.md) and [secrets](design/secret-lifecycle.md).

## Deployment and updates

The CLI submits the AKS and PaaS Bicep deployments, prepares Kubernetes inputs,
and activates the Flux extension. Flux starts reconciling before the CLI exits.
The dependency graph brings up operators and middleware before OSDU services,
then runs partition/entitlements initialization and schema loading. Airflow
follows its own PostgreSQL dependency.

Once the Git source is suspended, workloads and controllers keep running.
Suspension is not a full environment snapshot: live ConfigMaps can change, and
Helm repositories and other controllers have their own reconciliation behavior.

There are two separate update inputs:

| Input | How it changes |
|---|---|
| Workload configuration and local Helm charts | Fetch a new Git revision, then let Flux apply it |
| OSDU service and schema-loader images | Resolve a new `osdu-image-lock`; refreshes preserve explicit service image pins |

The [Flux guide](design/flux-reconciliation.md#fetching-a-new-git-revision)
describes a controlled resume-and-suspend sequence. The current `spi reconcile`
command requests reconciliation but does not temporarily resume a suspended
source, so it should not be treated as a guaranteed one-shot Git fetch.

### Shared environment and fork deployments

The shared environment runs this same stack at the `stackVersion` declared in
[`ops/environments/shared.yaml`](../ops/environments/shared.yaml). Upgrade and
refresh workflows use that release's CLI and gate mutations with maintenance.
`spi status --json` reports deployment health, maintenance, and a `deployable`
verdict; `spi connect` obtains access to an existing cluster.

`spi service pin --image --ephemeral` records a fork-built digest and its owning
run in the image lock. Verification checks rollout and digest; an
ownership-checked reset restores the captured canonical image. The environment
deploy identity and namespace Roles are provisioned. `spi onboard` plans and
applies repository protection, federation, and the trusted-repository projection;
source promotion, declaration enforcement, reset, and teardown workflows remain
unbuilt. See
[environment lifecycle](design/environment-lifecycle.md) and
[fork deployment](design/fork-deployment.md) for the implemented contract and
marked future work.

Ordinary `spi down` removes compute and data while retaining managed identities,
the resource group, and its tags. `--purge` removes the group after checking and
cleaning up external grants. The [deployment guide](design/deployment-lifecycle.md#steady-state-and-teardown)
owns deletion and retry behavior.

## Ingress profiles

The default `azure` ingress mode uses an Azure-assigned hostname and a
Let's Encrypt certificate. `dns` uses an existing Azure DNS zone and
environment-prefixed hostnames. `ip` exposes HTTP without TLS and is for
isolated debugging only.

The Gateway binds to AKS's existing managed ingress Service. Certificates live
in `platform`, with ReferenceGrants allowing the Gateway in
`aks-istio-ingress` to read them. The selected ingress tree is the Gateway's
only Flux inventory owner, including during mode changes.

The [ingress guide](design/gateway-ingress.md) describes the routes, certificate
ownership, and diagnostics. The [design index](design/README.md) links the other
subsystems; [ADRs](decisions/README.md) record the choices and alternatives.
