# OSDU SPI — Changes Log: Dockerfile, MSFT‑Tenant Stack, Deploy Lane

Date: 2026-07-06

> Historical implementation journal. AKS Automatic 1.36 later resolved the
> webhook restriction described here. ADR-031 now makes Automatic the default
> and preserves Base + NAP as an explicit deployment mode.

This document is a comprehensive record of the changes made across the OSDU SPI
work, organized into three parts as requested:

1. **Dockerfile analysis + fix** (App Insights agent / non‑root readability).
2. **SPI Stack changes to run in a Microsoft tenant** (`osdu-spi-stack`).
3. **Deploy‑lane changes** (`osdu-spi` template workflows/actions + service forks).

A fourth part captures **service source & test changes** that don't fit the three
buckets, and a fifth is a **"did we miss anything?" open‑items checklist**. A short
section up front records the **current dev5 / AG1 deployment and the path to MSFT**.

Each change lists **What / Where / Why / Fix / Tests** so the validation done for
every change is explicit.

---

## Repository / environment map

| Thing | Value |
| --- | --- |
| Template repo | `yuchen-osdu/osdu-spi` (fork of `Azure/osdu-spi`) — canonical Dockerfile + workflows/actions (ADR‑037) |
| Stack repo | `yuchen-osdu/osdu-spi-stack` — Azure infra (Bicep) + `spi` Python CLI + Flux manifests |
| Service forks | `yuchen-osdu/{partition, entitlements, legal, storage}` |
| Public Flux mirror | `yuchen-wang99/osdu-spi-stack-aksauto` (Flux GitOps source; avoids private‑repo auth) |
| Live dev clusters | dev1/dev2/dev3 (canadacentral, validation), **dev5** (eastus2, deploy‑lane E2E) |
| Deploy sub / tenant | `4f9d8783…` (MCI‑ENERGY‑OSDU‑INTERNAL), tenant `58975fd3…` (azureglobal1, **non‑MSFT‑corp**) |
| ADME reference | `oep-deployment-resources/IntegrationTests/OneBranch/ServiceITsProd.yml`, `OSDU-Legal`, `OSDU-Entitlements`, `OEP-RP` |

---

# Current deployment: dev5 on the AzureGlobal1 (AG1) tenant → path to MSFT

**Where it runs today.** Everything in this document is currently deployed and being
validated on **dev5**, a live SPI Stack in the **AzureGlobal1 (AG1)** tenant:

| Fact | Value |
| --- | --- |
| Cluster / RG | `spi-stack-dev5` |
| Region | `eastus2` |
| Subscription | `4f9d8783…` (MCI‑ENERGY‑OSDU‑INTERNAL) |
| Tenant | `58975fd3…` (AzureGlobal1 / AG1, **non‑MSFT‑corp**) |
| Key Vault / ACR | `osdudev5b3637` / `osdudev5b3637.azurecr.io` |
| Gateway | `https://spi-stack-dev5-ingress.eastus2.cloudapp.azure.com` |
| Flux namespace | `osdu-flux` |

**Why AG1 and not an MSFT‑corp tenant (the only tenant‑specific reason).** The deploy
lane authenticates to Azure with **GitHub Actions OIDC** (federated, secret‑less). The
MSFT‑corp tenant enforces a Conditional Access policy that rejects OIDC tokens lacking
a GitHub **Enterprise** claim, failing with **AADSTS7002381**. The `yuchen-osdu` dev
org is a **free‑plan** GitHub org, so its OIDC tokens don't carry that claim → blocked
in MSFT‑corp. The AG1 tenant does **not** enforce that policy, so OIDC from
`yuchen-osdu` works and we can iterate end‑to‑end there. This is a **GitHub‑org**
property, not a code problem.

**Everything else is tenant‑agnostic.** All the stack changes (Part 2) and deploy‑lane
changes (Part 3) — Base+NAP, Cosmos/Service Bus Workload Identity, Gremlin MSI,
per‑identity auth, the ADME‑equivalent acceptance suites, CI‑mode automation — are
**not** AG1‑specific. The AADSTS7002381 OIDC‑claim gate is the single remaining
tenant‑specific blocker.

**Path to MSFT (no blockers).** Once this code is **merged into the `Azure` GitHub
org** (a GitHub Enterprise org), OIDC tokens from that org carry the Enterprise claim
the MSFT‑corp tenant's Conditional Access requires. At that point the **same** deploy
lane can target an **MSFT‑corp tenant with no blockers** — the AKS‑Automatic guardrail,
Cosmos/Service Bus local‑auth, and Gremlin‑key blockers are already solved in code
(Part 2), so AG1 → MSFT is purely a matter of running from the enterprise org. **dev5 /
AG1 is the interim validation environment, not the target.**

---

# Repositories: visibility, merge status, and access

_Audited 2026‑07‑06._

| Repo | Visibility | Purpose | Our changes merged to `main`? | Daniel access |
| --- | --- | --- | --- | --- |
| `yuchen-osdu/osdu-spi` | Public | Template — canonical Dockerfile + workflows/actions | **Yes** — #25, #26, #16 (ADR‑038) | Public (view/clone) |
| `yuchen-osdu/osdu-spi-stack` | **Private** | Azure infra (Bicep) + `spi` CLI + Flux manifests | **Yes** — #2–#6 **and #7 (full MSFT‑tenant infra)** | **Collaborator invite sent (write, pending accept)** |
| `yuchen-osdu/partition` | Public | Service fork | **#16 open (reconsider)** — its Dockerfile `--chmod=0755` fix is already in the template (reaches the fork via the sync bot); its Istio `x-payload` tweak is an **internal‑testing artifact** (partition is internal/controlled in ADME), so #16 carries nothing that must merge. Blocked by the fork ruleset regardless. | Public (view/clone) |
| `yuchen-osdu/legal` | Public | Service fork | **#15 closed** (obsolete revert); ADME test‑suite re‑raised clean as **#19** (`fix/adme-legal-test-azure` — federated token + skip COO blob, no revert) | Public |
| `yuchen-osdu/storage` | Public | Service fork | **#13 closed** (obsolete revert); **no test‑code to re‑raise** — storage's test alignment is via repo vars (`MAVEN_GOAL`) + already‑upstream `AzureTestUtils`; deploy‑lane/docker are in the template | Public |
| `yuchen-osdu/entitlements` | Public | Service fork | Yes (`main`) | Public |
| `yuchen-wang99/osdu-spi-stack-aksauto` | Public (personal acct) | Flux GitOps mirror / deploy source | n/a | Public |

**MSFT‑tenant infra merge (2026‑07‑06).** `feature/gremlin-msi-bicep` (18 commits — Base+NAP, Cosmos/Service Bus `disableLocalAuth` + Workload Identity, Gremlin MSI, the four record‑ingestion blockers, real App Insights) was merged into `osdu-spi-stack` `main` via **PR #7**. Conflicts were resolved by keeping `main`'s namespace‑agnostic `reconcile`/`guard` (ADR‑014/032) and **renumbering the feature branch's ADRs to 021–024** (021 aks‑base, 022 disable‑local‑auth, 023 app‑insights, 024 record‑ingestion) to preserve the already‑merged 019 (ADME tests) / 020 (deploy‑lane invariants); all cross‑references updated. Validated: `ruff` clean, **65 `pytest`**, `bicep build` (aks/main/flux) OK.

**Sharing the private repo with Daniel.** `danielscholl` (GitHub User) was invited as a **write collaborator** to `osdu-spi-stack` — invite pending his acceptance. The five public repos need no grant. Note the fork PRs (partition #16, legal #15, storage #13) require a **second, non‑pusher approver**; once Daniel accepts he can provide that review.

---

# Part 1 — Dockerfile analysis and the App Insights agent fix

## 1.1 Where do the Dockerfiles come from?

- **Each service fork has its own `build/Dockerfile`** (`partition/build/Dockerfile`,
  `storage/build/Dockerfile`, `legal/build/Dockerfile`, …). There is not a single
  shared Dockerfile in `osdu-spi` that services import at build time.
- **But the `osdu-spi` template is canonical (ADR‑037).** The template holds the
  reference Dockerfile; forks inherit/refresh it through the SPI sync mechanism.
  So a fix made in the **template** propagates to **all** forks on sync — that is
  the "fix that benefits all."
- Practical consequence discovered the hard way: a fix applied only to
  `partition/build/Dockerfile` did **not** reach `storage`/`legal` (they still
  carried the old line), because the template — not partition — is the source of
  truth. See 1.2.

## 1.2 The App Insights agent readability bug (three‑stage evolution)

This was one root problem that surfaced three times as the fix was refined.

### Stage A — `dockerfile-agent` (crashloop, permissions)
- **What:** Services crash‑looped at startup.
- **Why:** The Dockerfile pulls the App Insights Java agent with
  `ADD <url> /opt/agents/applicationinsights-agent.jar`. `ADD` from a URL defaults
  the file to **mode 0600, root‑owned**. Services run as **non‑root UID 1000**, so
  the process cannot read the jar → JVM `-javaagent` fails → crashloop.
- **Fix (first attempt):** add `--chmod=0644` to the `ADD`. *(partition, PR #16)*

### Stage B — `npe-500` (the real integration‑test blocker)
- **What:** After `--chmod=0644`, partition returned **HTTP 500** through the
  gateway (`LogCustomDimensionFilter` NPE), which was the actual acceptance‑test
  blocker.
- **Why (root cause):** `ADD --chmod=0644` also set the auto‑created **`/opt/agents`
  directory** to `0644` — **no execute/traverse bit**. UID 1000 cannot traverse the
  dir → entrypoint `[ -f .../agent.jar ]` is false → `-javaagent` is never attached
  → App Insights request‑telemetry context is null → `core-lib-azure`
  `LogCustomDimensionFilter` dereferences it with no null‑guard → NPE → 500.
- **Fix:** use **`--chmod=0755`** (directory traversable *and* jar readable).
- **Where:** `core-lib-azure` `LogCustomDimensionFilter:26` is the NPE site; the
  Dockerfile `ADD` is the real cause.

### Stage C — `storage-agent-chmod` (propagation gap)
- **What:** The SPI‑built **storage** image (`…/storage:a786732`) crash‑looped at
  JVM init: *"Error opening zip file or JAR manifest missing
  /opt/agents/applicationinsights-agent.jar."*
- **Why:** Partition had the `--chmod=0755` fix **locally**, but it was **never
  propagated to the template or the other forks**. Storage/legal still shipped the
  old `ADD`. Same class of non‑propagation as the deploy‑lane digest fix (Part 3).
- **Fix (the one that benefits all):** add `--chmod=0755` to the agent `ADD` in the
  **`osdu-spi` template** and in the **storage** and **legal** forks (partition
  already had it). Template is canonical, so future forks inherit it automatically.

### Tests / validation (Part 1)
- **Partition (Stage B):** after `--chmod=0755` — App Insights agent attached
  (`/opt/agents` 0755), the NPE disappeared, and **ListPartitions + HealthCheck +
  Swagger passed through the gateway**. A prior session (`ae6b083b`) had already
  proven that attaching the agent removes the NPE.
- **Storage (Stage C):** after the template/fork fix, the storage image starts
  cleanly (no zip/JAR‑manifest crash) and went on to run the full storage
  acceptance suite (see Part 3 E2E: 126/133).
- **Related root‑cause confirmation:** the App Insights NPE was independently
  reproduced and explained in the stack work (Part 2.7) — `core-lib-azure ≥ 2.5.6`
  ships `LogCustomDimensionFilter` with no null guard.

---

# Part 2 — SPI Stack changes to run in a Microsoft tenant

Making `osdu-spi-stack` deploy and run under Microsoft‑tenant Azure policy required
changes across five areas: **AKS platform**, **Cosmos/Service Bus local‑auth**,
**Gremlin MSI**, **per‑identity auth**, and a set of **deploy‑blocker fixes**.
Branch for stack infra work: `feature/gremlin-msi-bicep` (Flux source =
public mirror `yuchen-wang99/osdu-spi-stack-aksauto`).

## 2.1 AKS Automatic guardrails → pivot to Base SKU + NAP

- **What:** After an Azure‑side hardening wave (~2026‑06‑02), AKS **Automatic**
  clusters made **managed system node pools mandatory**, which enforces admission
  guardrails that broke the stack in four ways:
  1. **AKS create** rejected `userAssignedNATGateway` + managed hosted pool.
  2. Deployer **writes to protected `flux-system`** denied.
  3. Flux **`flux-applier` impersonation** denied (`multiTenancy.enforce=true`).
  4. **`MutatingWebhookConfiguration` create/modify blocked for ALL identities**
     (even cluster‑admin) — non‑bypassable. cert‑manager + CloudNativePG both
     *require* MWCs, so the full stack **cannot reconcile on AKS Automatic**
     post‑2026‑06‑02.
- **Proof (not assumed):** Daniel's same commit `6470bd5b` passed 2026‑06‑01 and
  failed 2026‑06‑02 (Azure regression, not code). Even
  `yuchenwang@microsoft.com` (cluster‑admin) got
  *"Creation or modification of MutatingWebhookConfiguration resources is not
  allowed."*
- **Fix:**
  - Pivot the cluster from **AKS Automatic → AKS Base SKU + Node Auto‑Provisioning
    (NAP)**, where the MWC guardrail does not apply. `infra/aks.bicep` rewritten as
    a **raw `Microsoft.ContainerService/managedClusters@2026-03-01`** with
    `hostedSystemProfile` (nodeSubnetID + systemNodeSubnetID); `infra/modules/vnet.bicep`
    adds a managed system node subnet.
  - `infra/flux.bicep`: `multiTenancy.enforce=false` (removes `flux-applier`
    impersonation).
  - All CLI/GitOps‑written config moved from protected `flux-system` → user
    namespace **`osdu-flux`** (touches `secrets.py`, `images.py`, `deploy.py`,
    `bootstrap.py`, `guard.py`, `ingress.py`, `templates.py`, `status.py`,
    `info.py`, `cli.py`, and `software/**`).
- **Note:** On the non‑corp tenant used for dev5 (Base+NAP), the MWC guardrail is
  gone — cert‑manager/CNPG/eck/trust‑manager all reconcile `Ready`.

## 2.2 Cosmos + Service Bus local‑auth disabled → Workload Identity

- **What:** MSFT‑tenant policy denies creating Cosmos/Service Bus with **local
  (key) auth enabled** (`… Denied When Local Auth Is Enabled`).
- **Fix (Bicep + manifests):**
  - Cosmos Gremlin: `disableLocalAuth: true`; stop writing a real
    `graph-db-primary-key`; assign **built‑in Gremlin Data Contributor** to the
    OSDU identity.
  - Service Bus namespace: `disableLocalAuth: true`, `minimumTlsVersion: '1.2'`;
    `{partition}-sb-connection` secret set to `DISABLED`; RBAC = **Azure Service Bus
    Data Owner**; Service‑Bus‑consuming service manifests set
    `AZURE_MSI_ISENABLED=true` (so `core-lib` picks the Workload‑Identity token
    path, not keys).
- **Dependency caught:** `indexer-queue` master pins `core-lib-azure 2.0.6`, whose
  Service Bus path is **not** Workload‑Identity‑aware → the hard blocker for SB
  MSI. (Fixed in the service via MR !474, Part 4.)

## 2.3 Gremlin MSI entitlements (custom image, ADR‑020)

- **What:** The community entitlements image reads `graph-db-primary-key`, which no
  longer exists once Cosmos local‑auth is disabled.
- **Fix:** deploy the **MSI‑Gremlin entitlements image**
  (`…/entitlements:gremlin-msi-direct-…`) — obtains a Cosmos token
  (`https://cosmos.azure.com/.default`) via Workload Identity and uses it as the
  Gremlin password. Swapped in by patching the `osdu-image-lock` ConfigMap
  (ns `osdu-flux`) **before** the `osdu-services` Kustomization renders.
- Proven end‑to‑end in prior sessions (WI token → MSI Gremlin connect →
  tenant‑provisioning 200).

## 2.4 Per‑identity auth model (Lua + entitlements seeding)

Two coupled changes so the **CI Managed Identity** can authenticate as a real,
entitled user (needed for integration tests through the mesh).

- **`lua-identity` (ADR‑016):** The Istio `EnvoyFilter` Lua mapped **all**
  `aud=management.azure.com` tokens to the single service‑account `entraClientId`,
  which bypasses entitlements. **Fix:** in the management branch, map by the token's
  **`payload[appid]`** (the real caller) instead of the service account. Bootstrap
  is unaffected (UAMI `appid == entraClientId`). *Committed in `templates.py`;
  source‑complete.*
- **`onboard-seed`:** The CI MSI `appid` was never granted entitlements membership.
  **Fix:** `spi onboard` gained `_ensure_entitlements_membership` (+ `--partition`
  flag) — runs a short in‑cluster Job as the tenant‑provisioning OWNER
  (`workload-identity-sa`) that POSTs the CI MSI `appid` into the four ADME groups
  (`users`, `users.datalake.ops`, `users.datalake.admins`, `users.data.root`) via
  the AddMember API; idempotent (409 = ok), dry‑run aware, self‑verifying. Mirrors
  ADME `InstanceInit.cs`.

## 2.5 `onboard` custom deploy‑role fix (`onboard-rbac`)

- **What:** The onboard‑created custom Azure role carried **invalid k8s
  `dataActions`** (`…/pods/log/read`, a Flux CRD action) that are not registered
  `Microsoft.ContainerService` operations, so `az role definition create` failed —
  and it was called with `check=False`, so it **failed silently** and blocked the
  deploy at the CI‑mode Flux‑suspend step.
- **Fix (codified, commit `6699117`, branch `feat/per-identity-entitlements`):**
  drop both invalid `dataActions`; set `check=True` (fail loud); add
  `_ensure_flux_read_rbac` — a **native k8s Role + RoleBinding** (subject =
  principalId) that grants the Flux read needed by the CI‑mode suspend pre‑check.

## 2.6 The four deploy‑blocker fixes (dev2, commit `8e73cd7`)

Found because prior sessions only ever exercised entitlements + indexer‑queue;
storage/legal/schema/search/workflow were never tested. All root‑caused, fixed
live, and codified:

1. **Cosmos SQL data‑plane RBAC:** per‑partition Cosmos SQL + `osdu-system-db`
   disable local auth but never granted the OSDU UAMI a data role → **403** on
   legal/storage/schema/workflow. Fix: `principalId` param + **Built‑in Data
   Contributor (`…0002`)** `sqlRoleAssignment` (`partition.bicep` + `main.bicep`).
2. **`system-cosmos-*` KV secrets missing:** schema/workflow ('system' services)
   need `system-cosmos-endpoint/-primary-key/-connection`. Fix: write the three
   secrets in the primary partition module, guarded on `isPrimaryPartition`.
3. **Storage record container missing:** storage‑azure writes record blobs to a
   container named after the partition id (`opendes`); `core-lib` BlobStore does not
   auto‑create → **404 ContainerNotFound**. Fix: `union(partitionStorageContainerNames,
   [partition])`.
4. **Elasticsearch TLS:** ECK serves HTTPS self‑signed (CA in truststore), but the
   partition record said `elastic-ssl-enabled=false` (→ plaintext ConnectionClosed)
   and used a `…svc.cluster.local` endpoint absent from the cert SANs
   (→ SSLPeerUnverifiedException). Fix: `elastic-ssl-enabled=true` +
   endpoint `elasticsearch-es-http.platform.svc` (`deploy.py` + `templates.py`).

## 2.7 App Insights NPE (real telemetry) — stack side

- **What:** `core-lib-azure ≥ 2.5.6` ships `LogCustomDimensionFilter`, which reads
  the AI request‑telemetry context on every request **with no null guard** → NPE →
  500 when App Insights is not initialized. AKS Automatic enabled AI by default;
  Base did not.
- **Fix:** provision **real App Insights** (`main.bicep`) + wire the connection
  string into `osdu-config` (`envFrom`, all services) with a dummy fallback;
  per‑service `APPLICATIONINSIGHTS_ROLE_NAME`. Removed the earlier
  entitlements‑specific dummy‑key hack. *(commits `6b3c2ff`, `94c7cb0`)*
- This is the stack‑side counterpart to the Dockerfile fix in Part 1 (both must be
  right for the agent to actually attach and the filter to have context).

## 2.8 Region / zone fix (`aks-zones`)

- **What:** eastus2 systempool rejects availability zone `2` (only `1,3`
  supported); canadacentral hit a Cosmos zonal‑redundant capacity shortage.
- **Fix:** `infra/aks.bicep` availability zones `['1','2','3']` → `['1','3']`;
  Cosmos already `isZoneRedundant:false`. **Open:** this edit is still a local
  uncommitted change on the feature branch — see Part 5.

### Tests / validation (Part 2)
- **dev1 (canadacentral, Base+NAP):** full stack reconciled — platform 14/14,
  foundation 6/6, ingress 3/3; both custom images Running+Ready; real App Insights
  telemetry flowing; external gateway 200.
- **dev2 (canadacentral) from‑scratch E2E:** **13/13 OSDU API smoke green**,
  including the full `storage → indexer-queue → indexer → search` data flow
  (`search totalCount=2`); all 9 services `/info` = 200 externally. This run
  surfaced + validated the four blocker fixes (2.6).
- **dev3 (canadacentral) from‑scratch:** **13/13 green**; proved all four codified
  blocker fixes land automatically on a clean deploy (verified on the PaaS layer
  before Flux reconciled: system‑cosmos secrets, opendes blob container, Cosmos SQL
  data role, App Insights, elastic endpoint).
- **dev5 (eastus2) new‑tenant deploy:** `spi up` completed exit 0 on the non‑corp
  tenant; infra + PaaS + Flux up; gateway live; entitlements custom image swapped.
- **Unit/build gates (every stack commit):** `bicep build`, `ruff`, `helm
  template/lint`, and **54 `pytest`** all green; onboard/CLI pytest green for the
  onboard changes (2.4/2.5); what‑if clean.

---

# Part 3 — Deploy lane (CI/CD)

The engineering system Daniel built (the three‑branch fork model) produces validated
Maven artifacts, but it stops there — no container image, no deployment, and no
integration‑test signal against a live cluster. **Part 3 is the deploy lane we added
on top of it**: a reusable pipeline that takes a merged change on a service fork,
builds and pushes a container image, deploys it to the shared `osdu-spi-stack`
cluster, and runs that service's ADME acceptance suite against the live gateway.

Design principles it's built on:

- **Reusable template workflow (ADR‑015).** The whole lane is authored once in the
  `osdu-spi` template (`.github/template-workflows/validate.yml` plus composite
  actions under `.github/actions/`) and synced to every fork. Per‑service specifics
  come from GitHub **repo variables** (`SERVICE_NAME`, `AKS_CLUSTER_NAME`,
  `K8S_NAMESPACE`, `K8S_DEPLOYMENT_NAME`, `K8S_CONTAINER_NAME`, `FLUX_NAMESPACE`,
  `GATEWAY_URL`, `KEYVAULT_NAME`, `AAD_CLIENT_ID`), so no fork edits the workflow.
- **Immutable digests (ADR‑032).** Every deploy references the image by `sha256:`
  digest, never a mutable tag, so the running image is provably the one the build
  produced and the tests exercised.
- **CI mode (ADR‑032).** The lane deploys imperatively into a Flux‑suspended cluster
  (§3.6), so GitOps never reverts a CI image mid‑test.
- **Secret‑less, per‑identity auth.** Every Azure step logs in with a federated
  **OIDC** token (`id-token: write`), and the tests authenticate as the CI Managed
  Identity — there is no service‑principal client secret anywhere in the lane.

## 3.1 Pipeline shape (`validate.yml`)

On a qualifying event the workflow runs a job graph:

```
check-init → check-repo-state → check-paths → java-build
          → docker-build → docker-push → deploy → integration-test
                                       (+ code-quality, cluster-health)
```

`deploy` and `integration-test` run only for same‑repo pushes — a PR from a fork
can't reach the cluster. A **per‑service concurrency group**
(`spi-stack-${SERVICE_NAME}`) serializes a service against itself while still letting
different services deploy in parallel.

## 3.2 Build and push

`java-build → docker-build → docker-push` compile the service, build the container
(multi‑arch), and push it to the registry. `docker-push` outputs the **image digest**,
which is the only image reference used from here on.

## 3.3 Deploy — the `aks-deploy` action

Given the cluster/namespace/deployment repo‑vars and the pushed digest, `aks-deploy`
runs these steps:

1. **OIDC login** to Azure (federated, secret‑less) and fetch AKS credentials.
2. **Validate the digest format.**
3. **CI‑mode pre‑flight assertion** — verify Flux is suspended (GitRepository,
   Kustomizations, *and* HelmReleases) and **fail loud** if anything is reconciling,
   pointing at `spi reconcile --suspend`. This is the guard that stops GitOps from
   racing the deploy (§3.6).
4. **Capture the previous digest**, so a bad deploy can be restored.
5. **`kubectl set image deployment/<name> <container>=<repo>@<digest>`** — the deploy
   itself: one imperative call, by digest.
6. **Wait for rollout**, then **capture the deployed digest** from the new
   **Running, non‑terminating, Ready** pod — deliberately not `.items[0]`, so a
   draining old pod can never be recorded as the new one. That digest is handed to
   the test job.

## 3.4 Integration test — the `integration-test` action

The test job re‑authenticates (OIDC), fetches AKS credentials, and then:

1. **Digest guard** — confirm the pod that will serve the tests is *still* running the
   exact digest `aks-deploy` recorded; if the cluster drifted (e.g. an overlapping
   run), it stops rather than testing the wrong image.
2. **Cross‑service health probe** (advisory) against the services this one depends on.
3. **Mint the acceptance‑test token with no secret** — a federated
   `INTEGRATION_TESTER_ACCESS_TOKEN` for the CI Managed Identity — and inject the
   non‑secret config from `env_map` (service URLs, `INTEGRATION_TESTER`, `AZURE_AD_*`).
4. **Load Key Vault secrets** named by `secret_map`, masked and multiline‑safe.
5. **Run the ADME `<svc>-test-azure` suite** through the gateway (`maven_goal`), with
   **retry** (`max_attempts`) and a tunable per‑attempt **timeout** (`timeout_minutes`)
   sized for the reactor build plus the live tests.
6. **Publish JUnit results** and a run summary.

The per‑service inputs it consumes — `test_dir`, `maven_goal`, `root_token_env`,
`env_map`, `secret_map`, `gateway_url`, `keyvault_name`, `aad_client_id` — are the
onboarding variables covered in §3.7.

Two robustness details were load‑bearing for getting stable green runs: the
`maven_goal` carries `-Dsurefire.failIfNoSpecifiedTests=false` so a
`-pl <svc>-test-azure -am` reactor build doesn't fail when a `-Dtest=!…` exclusion
leaves the *core* module with zero matching tests; and the digest guard together with
the per‑service concurrency group (§3.1) keep two overlapping runs from testing each
other's images.

## 3.5 Running the suites ADME‑equivalent and secret‑less

The suites are the real ADME `testing/<svc>-test-azure` modules, made to run
secret‑less on the per‑identity token:

- **Legal** — adopted ADME's `AzureLegalTagUtils` (its `accessToken()` prefers the
  pre‑minted `INTEGRATION_TESTER_ACCESS_TOKEN`), and — following ADME's `OSDU-Legal`
  m26 — left the COO‑blob `uploadTenantTestingConfigFile()` calls commented out (the
  deployed service already carries that config). That removed the last test needing a
  storage‑account key, so the **service principal + secret we had created was deleted**
  — the legal suite is now byte‑identical to ADME and fully secret‑less.
- **Storage** — ADME's `AzureTestUtils` already accepts the pre‑minted token, so no
  code change was needed.
- **Key Vault parity** — the only KV‑sourced values (`dataStorageAccount/Key`,
  `serviceBusConnectionString`) are a *delivery* difference, not an extra requirement:
  ADME injects the same values at runtime from its leased instance's Key Vault, and we
  map them identically via `secret_map` from the stack Key Vault.

## 3.6 CI‑mode automation (setter + checker)

CI mode — the Flux‑suspended steady state the lane deploys into (ADR‑032) — used to be
a manual `flux suspend` step, and the CLI only suspended Kustomizations, which wasn't
enough: the services are **HelmRelease‑managed**, so a HelmRelease reconcile would
still revert a `kubectl set image`. Two merged changes closed that gap:

- **Setter — `osdu-spi-stack #6`:** `spi reconcile --suspend` now performs a full
  freeze (**GitRepository + all Kustomizations + all HelmReleases**; `--resume`
  reverses), with the Flux namespace auto‑resolved from the live GitRepository (fixing
  a hardcoded `flux-system` that broke on the `osdu-flux` stack).
- **Checker — `osdu-spi #26`:** the `aks-deploy` pre‑flight (§3.3, step 3) asserts all
  of it is suspended and fails loud otherwise.

Net: entering CI mode is a single `spi reconcile --suspend`, and the deploy lane
refuses to run unless the cluster is in it.

## 3.7 Per‑service onboarding variables (ONBOARD‑INIT) — design only

The per‑service acceptance‑test inputs in §3.4 (`test_dir`, `maven_goal`,
`root_token_env`, `env_map`, `secret_map`, …) are today **hand‑set per fork**.
Research (no implementation) produced a recommended way to automate them:

- The variables split into **three tiers**: (1) universal auth (identical for every
  service), (2) derivable from stack facts + service name (service URLs, partition,
  domain, Key Vault), and (3) irreducibly service‑specific (test exclusions,
  cross‑service dependencies, feature tokens, module‑name exceptions).
- **Recommendation:** auto‑derive tiers 1 and 2 (the lane already mints the token and
  `spi onboard` already writes the stack facts), and supply tier 3 from a small
  per‑service **IT profile** translated once from that service's ADME
  `ServiceITsProd.yml` block, checked into the `osdu-spi` template and read by
  `setup-service-variables.sh`. Parsing ADME's pipeline at runtime was rejected
  (cross‑org coupling).
- Only **four profiles are needed now** (legal, storage, partition, entitlements — the
  forked services). Captured in the separate ONBOARD‑INIT research notes.
- **Blocked:** ONBOARD‑INIT‑B (#11) depends on ONBOARD‑INIT‑A (input‑mechanism spec)
  upstream.

### Validation — end‑to‑end results

- **Storage acceptance (dev5, per‑identity federated token + seeded MSI):**
  **133 run, 126 pass, 0 error, 1 skip, 7 fail.** All 7 failures are
  `PostFetchRecordsIntegrationTests.should_returnRecordsAfterCrsConversion__*` —
  **crs‑conversion is not deployed** in any namespace (out of scope for
  partition/entitlements/legal/storage); its route `/api/crs/converter/v3` 500s. Those
  7 methods are excluded ADME‑style; the **per‑identity auth model is validated by the
  126 passing tests.**
- **Legal acceptance (dev5):** green and **fully secret‑less** (federated CI‑MSI
  token); the COO‑blob test removed to match ADME (§3.5). `INTEGRATION_TESTER` is set
  to the legal CI MSI.
- **Partition acceptance (deploy‑lane E2E):** validate run `28210313734` (re‑run) →
  **Tests run: 11, Failures: 0** (`GetPartitionByIdApiTest` 4/4). An earlier 404 chain
  was root‑caused (workflow HelmRelease InProgress → osdu‑services health gate →
  bootstrap never ran → `opendes` record absent) and fixed by pinning workflow to a
  dev3 known‑good image (see Part 4).
- **CI‑mode automation:** `tests/test_reconcile.py` (5 tests) pass; the setter was
  live‑validated on dev5 (0/0 not‑suspended); the checker is merged.
- **Template/fork wiring:** the deploy/test actions, timeout/surefire settings, and
  the token standardization all live in the canonical template, so every fork inherits
  them on sync.

---

# Part 4 — Service source & test changes (outside the three buckets)

These are real code changes in the **service forks** that were needed to get the
deploy lane green but don't belong to Dockerfile / stack / lane.

## 4.1 Interim upstream‑regression workarounds (netty 4.1/4.2)

> **Framing — these were temporary measures to unblock testing, not permanent
> reverts.** At the time, the upstream community `*-master` service images had a
> genuine netty/lettuce regression that broke the services, so reverting/pinning to a
> known‑good baseline was the fastest way to keep the deploy lane moving. **Most of
> these upstream issues have since been fixed** (the related upstream packages were
> repaired and merged), so these interim reverts/pins can be dropped as the fixes
> propagate — they are not intended to live in the forks long‑term.
>
> **Update 2026‑07‑06:** the `legal #15` / `storage #13` revert PRs were **closed
> (abandoned)** as obsolete — upstream is fixed, so the forks pick up the corrected
> packages via the "⬆️ Sync with upstream" bot PR. Their branches are preserved for
> salvaging the small ADME test‑suite pieces (to be re‑done cleanly on `main`).

- **`legal-netty` / `workflow-netty`:** upstream community `*-master` images built
  ~2026‑06‑25 mix **netty 4.2.14 (common) vs 4.1.118 (buffer)** →
  `NoClassDefFoundError: io.netty.channel.MultiThreadIoEventLoopGroup` (from
  `lettuce-core 7.5.2`, the Redis client) → crash/500 on the entitlements‑cache
  lookup.
- **Interim workaround (legal/storage forks):** temporarily **revert** the "Migrate to os‑core‑common 7.1.0"
  MR (legal `5cc42bc`, parent `73355cb8`; storage `616525e7`, parent `5db25e7adf0b`).
  That MR bumped `os-core-common 6.0.0 → 7.1.0`, forcing `lettuce-core 7.5.2` (CVE)
  which needs netty 4.2, but runtime had netty 4.1. After revert: `os-core-common
  6.0.0`, `lettuce 6.8.1`, `netty 4.1.130` consistent; legal‑azure builds clean +
  dep‑tree verified. Whole‑commit revert (poms + tests together, since tests were
  migrated for the 7.1.0 API).
- **`dev5-pin-stability`:** pinned the non‑target dev5 services (file, indexer,
  schema, search) to **dev3 known‑good tags**; workflow pinned to `e4ae2ad4b7af`
  (Jun 17, pre‑regression). All Running 2/2, HR `UpgradeSucceeded`.

## 4.2 Storage no‑data‑access test skip (`storage-nodata-skip`)
- Only `TestRecordAccessAuthorization` uses `getNoDataAccessToken()`; it needs a
  **second identity** (`NO_DATA_ACCESS_TESTER`). Interim: `ENV_MAP
  NO_DATA_ACCESS_TESTER=<dummy>` + `MAVEN_GOAL -Dtest=!TestRecordAccessAuthorization`.
  **Open:** provisioning the real second identity is deferred (Part 5).

## 4.3 indexer‑queue Workload‑Identity core‑lib bump (MR !474)
- **What:** `indexer-queue-azure-enqueue` pins `core-lib-azure 2.0.6`, whose Service
  Bus subscription client can't use the Workload‑Identity token path. With SB local
  auth disabled (MSFT‑tenant policy) the queue consumer never subscribes to
  `recordstopic` → records aren't indexed.
- **Fix:** bump `core-lib-azure 2.0.6 → 2.5.10` (Workload‑Identity‑aware SB
  credential). MR **!474** on `feature/servicebus-workload-identity`. **Merged** —
  Jordan fixed the related upstream packages and merged the MR.

---

# Part 5 — Did we miss anything? Open items & status

| Item | Status | Note |
| --- | --- | --- |
| `aks-zones` eastus2 `['1','3']` | **codify** | `feature/gremlin-msi-bicep` is now merged to `main`, but `main`'s `infra/aks.bicep` still carries the default `['1','2','3']`; the eastus2 `['1','3']` tweak was **never committed**. Codify region‑aware (don't hardcode `['1','3']`). |
| Storage `NO_DATA_ACCESS_TESTER` 2nd identity | **open** | Currently a dummy + test exclusion; real second identity deferred (user: "fix later"). |
| ONBOARD‑INIT automation (per‑service vars) | **open / blocked** | Design done (3.6); implementation blocked on ONBOARD‑INIT‑A upstream. 4 profiles needed now. |
| `lua-identity` per‑identity auth | **partial** | Committed + deployed on dev5; the **positive path is already validated** — legal/storage pass E2E with the per‑identity federated token. Remaining: a targeted test isolating the token‑`appid` mapping to guard against a regression to the old service‑account bypass. |

**Merge state (`merge-state`, updated 2026‑07‑06):**
- **Merged to `main`:** `osdu-spi-stack` **#2–#6** (onboard, per‑identity ADR‑016/019/020, CI‑mode reconcile) **and #7** (full MSFT‑tenant infra — Base+NAP, Cosmos/SB WI, Gremlin MSI, blockers, App Insights; feature ADRs renumbered 021–024); `osdu-spi` **#25** (template: chmod, digest‑capture, token std, concurrency), **#26** (aks‑deploy suspend assertion), **#16** (ADR‑038, defer extra‑file Docker support).
- **Open (reconsider):** `partition #16` — Dockerfile chmod (already in the template) + an Istio `x-payload` tweak that turned out to be an **internal‑testing artifact** (partition is internal/controlled in ADME; only our external‑token test exposure hit it). Nothing here must merge; can be closed. Blocked by the fork ruleset regardless.
- **Closed (abandoned) 2026‑07‑06, test work re‑raised clean:** `legal #15`, `storage #13` — the os‑core‑common 7.1.0 reverts are obsolete (upstream fixed; forks get it via the "⬆️ Sync with upstream" bot PR). The **non‑revert test‑suite work was split out onto fresh branches off `main`**: legal → **PR #19** (`fix/adme-legal-test-azure`: federated `INTEGRATION_TESTER_ACCESS_TOKEN` + skip COO‑blob upload, cherry‑picked, no revert); storage had **no test‑code** to re‑raise (its alignment is via repo variables + already‑upstream `AzureTestUtils`; the deploy‑lane/docker commits are already canonical in the template). Original revert branches preserved.
- All other open PRs in the forks are **bots** (dependabot deps bumps, the `yuchen-osdu-spi-bot` template/upstream sync + release), not our changes.

**Tracking issues updated (`github-issues-updated`):** status comments posted on
`yuchen-osdu/osdu-spi` **#1** (epic), **#13** (STACK‑OPS), **#11** (ONBOARD‑INIT‑B).

**Things intentionally out of scope:** crs‑conversion (not deployed),
`osdu-spi-stack #1` (MSFT‑corp tenant), search/notification/register/dataset/etc.
forks (not yet created).

---

## Appendix — ADRs referenced

| ADR | Topic |
| --- | --- |
| ADR‑007 | Layered Kustomization ordering (entitlements → legal → storage → {file,indexer,search,workflow} → indexer‑queue) |
| ADR‑014 | Suspend semantics (freeze = `reconcile` command / GitRepository source) |
| ADR‑016 | Per‑identity auth (Lua maps token `appid`) |
| ADR‑020 | Entitlements Gremlin‑MSI image requirement |
| ADR‑032 | CI mode (all reconcilers suspended) |
| ADR‑037 | `osdu-spi` template is the canonical Dockerfile/workflow source |
