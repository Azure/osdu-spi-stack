# Secret Lifecycle

**What this explains.** The three classes of credentials in SPI Stack, which store owns each one, who writes them and when, and how trust-manager mirrors the in-cluster CA material from `platform` into `osdu`.

**Why it matters.** Three stores sound like complexity but they exist because three sets of credentials have three different threat models. Mixing them is how PaaS connection strings end up as Kubernetes Secrets, which defeats the point of Workload Identity. This doc maps each credential to its store so you can answer "where does this live, who wrote it, and when can I rotate it."

> **Companion docs.** [Workload Identity](workload-identity.md) covers how PaaS credentials become bearer tokens at runtime. [Bicep architecture](bicep-architecture.md) covers the modules that declare KV secrets.

## Three credential classes, three stores

| Class | Examples | Store | Access path |
|---|---|---|---|
| Azure PaaS credentials | Cosmos DB, Service Bus, Storage, Key Vault | Entra ID (token broker) | Workload Identity; no stored material |
| PaaS metadata + secret values | Cosmos endpoints, Storage account names, Service Bus namespace, tenant ID | Azure Key Vault | SDK reads via Workload Identity (or CSI) |
| In-cluster middleware passwords | Redis, Elasticsearch, PostgreSQL (Airflow) | Kubernetes Secrets in `platform` / `osdu` | CLI-generated seed (`spi-secrets`), consumed by the operators |

This split is the decision in [ADR-010](../decisions/010-keyvault-secret-management.md). The next sections walk each class.

## Class 1: Azure PaaS credentials (Workload Identity)

There are no Class 1 secrets. The OSDU services authenticate to Cosmos, Service Bus, Storage, and Key Vault using AAD bearer tokens minted via Workload Identity (see [workload-identity](workload-identity.md)). Tokens are short-lived, refreshed automatically by the Azure SDK, and never written to disk.

The one carve-out is `{partition}-sb-connection` for indexer-queue, which holds a real Service Bus SAS connection string because the current `core-lib-azure` `SubscriptionClientFactoryImpl` does not honor the Workload Identity flag. The value lives in Key Vault and is gated by the same UAMI's `Key Vault Secrets User` role; it never lands in a pod env var. See [ADR-005](../decisions/005-workload-identity.md) Consequences for the full rationale.

## Class 2: Key Vault secrets

Two writers contribute to Key Vault:

### Writer A: Bicep (deploy time)

Most KV secrets are declared in Bicep, where the source value is Azure itself. The top-level `infra/main.bicep` declares the account-wide secrets and `infra/modules/partition.bicep` declares the per-partition ones, all at `az deployment group create` time. (`infra/modules/keyvault.bicep` declares only the vault, not its secrets.)

| Secret pattern | Source | Declared in |
|---|---|---|
| `tenant-id`, `subscription-id`, `osdu-identity-id`, `keyvault-uri`, `system-storage` | `tenant()` / `subscription()` / resource outputs | `main.bicep` |
| `graph-db-endpoint` / `graph-db-primary-key` | Cosmos Gremlin endpoint / `listKeys()` | `main.bicep` / `cosmos-gremlin.bicep` |
| `{p}-cosmos-endpoint`, `{p}-storage`, `{p}-sb-namespace` | Resource outputs | `main.bicep` |
| `{p}-cosmos-primary-key`, `{p}-storage-account-blob-endpoint` | `listKeys()` / resource `.properties` | `partition.bicep` |
| `{p}-cosmos-connection`, `{p}-storage-account-key`, `{p}-sb-connection` | `"DISABLED"` placeholder by default, or real SAS for the indexer-queue carve-out | `partition.bicep` |

Bicep writes are atomic with the rest of the deploy: the KV secret either lands with the resource or the whole deploy fails. ARM is idempotent on secret writes (a re-deploy with the same value is a no-op).

### Writer B: the CLI (post-handoff)

A small set of KV secrets covers the in-cluster middleware. The CLI knows all of these as soon as infra is up: the passwords come from the generated seed (`spi-secrets`, see Class 3) and the endpoints are the fixed in-cluster service DNS names.

| Secret | Source |
|---|---|
| `{p}-elastic-endpoint`, `{p}-elastic-username`, `{p}-elastic-password` | Fixed ES service DNS + generated `elastic_password` |
| `redis-hostname`, `redis-password` | Fixed Redis service DNS + generated `redis_password` |

`src/spi/deploy.py` (`_write_keyvault_bootstrap_secrets`) writes these with `az keyvault secret set` during Phase 6 of `spi up`. Because every value is already known from the seed, there is no wait for middleware to reach `Ready`.

Re-running `spi up` against a live cluster re-runs these writes idempotently; KV is fine with rewrites of the same value.

### Reader: the services

Services read their KV secrets via the Azure SDK using Workload Identity. The OSDU `partition-azure` provider auto-prefixes the partition id onto every `sensitive: true` value at read time, so the partition record's `partition.json` template holds **bare** suffixes (`cosmos-endpoint`, not `opendes-cosmos-endpoint`). The ADR-015 amendment that originally got this wrong is now folded into the ADR body; the chart template uses bare values.

## Class 3: In-cluster middleware passwords

The CLI generates all three middleware passwords (`src/spi/secrets.py`, `_generate_password`), stores them in a seed Secret `spi-secrets` in `flux-system`, and pre-creates the Kubernetes Secrets the operators consume. The operators read these pre-created Secrets rather than minting their own:

- **Elasticsearch.** The CLI creates `elasticsearch-es-elastic-user` in `platform`; ECK adopts it as the elastic-user credential.
- **Redis.** The CLI creates `redis-credentials` in `platform`; the Bitnami chart consumes it via `existingSecret` (`software/components/redis/release.yaml`).
- **PostgreSQL (Airflow).** The CLI creates `postgresql-superuser-credentials` and `postgresql-airflow-credentials` in `platform`; CNPG consumes them via `superuserSecret` / the owner secret.

The same generated passwords are mirrored into Key Vault by the CLI (Writer B above), so OSDU services in `osdu` read Elasticsearch and Redis credentials through the same Workload Identity path as everything else.

## CA distribution via trust-manager

A separate concern. Redis and Elasticsearch terminate TLS with self-signed CAs:

- Redis: cert-manager-issued, CA Secret in `platform`.
- Elasticsearch: ECK-managed CA, Secret in `platform`.

OSDU services need both CAs in `osdu` so the local Helm chart's `import-ca-certs` init container can fold them into the Java truststore. Kubernetes Secrets do not cross namespaces.

The SPI Stack runs **trust-manager** in `foundation`. `software/stacks/osdu/bootstrap/ca-bundles.yaml` declares two `Bundle` CRs that source from the platform-namespace CA Secrets and target Secret outputs in the `osdu` namespace (`redis-ca-cert`, `elastic-ca-cert`). trust-manager watches the sources and re-syncs the targets continuously, so a cert-manager renewal or an ECK CA rotation refreshes the truststore-input Secrets without any imperative step. See [ADR-011](../decisions/011-trust-manager-ca-distribution.md).

trust-manager is locked down: `secretTargets.authorizedSecrets` is pinned to the two names above and its default-CAs package is disabled.

The same Kustomization (`spi-bootstrap`, Layer 4b) also applies a static Istio `DestinationRule` (`redis-disable-mtls`) that turns off Istio mTLS to the in-cluster Redis master. Lettuce already speaks TLS directly to Redis, and Istio's automatic mTLS wraps that connection in a second TLS layer Lettuce cannot unwind.

## Worked example: rotating the Redis password

The Redis password is CLI-generated, stored in the seed (`spi-secrets`) and the `redis-credentials` Secret the chart consumes, and mirrored into Key Vault. Rotating it means updating those copies in sync.

1. **Update the Secret the chart consumes** with a new password, then restart Redis.
   ```bash
   NEW=$(openssl rand -base64 18)
   kubectl create secret generic redis-credentials -n platform \
     --from-literal=password="$NEW" --dry-run=client -o yaml | kubectl apply -f -
   flux reconcile helmrelease redis -n platform   # Redis restarts with the new password
   ```
2. **Write the new value to Key Vault** so services resolve it (`az keyvault secret set --name redis-password ...`), and update the seed so future `spi up` runs stay consistent.
3. **Roll the consumers.** `kubectl rollout restart deploy -n osdu`. Each service re-reads its credentials from KV at start.

The same shape works for the Elasticsearch credentials (`elasticsearch-es-elastic-user` / `{p}-elastic-password`).

## Worked example: trace where `cosmos-endpoint` for partition `opendes` lives

```bash
# In Bicep (declared):
$ grep -r 'cosmos-endpoint' infra/modules/partition.bicep

# In Key Vault (the actual secret, prefixed by partition-azure):
$ az keyvault secret show --vault-name <kv> --name opendes-cosmos-endpoint --query value -o tsv

# In the partition record (bare suffix, partition-azure prefixes at read time):
$ kubectl exec -n osdu deploy/partition -- \
    curl -s -H "Authorization: Bearer <token>" \
    -H "data-partition-id: opendes" \
    http://localhost:8080/api/partition/v1/partitions/opendes | jq '.properties["cosmos-endpoint"]'
{
  "value": "cosmos-endpoint",
  "sensitive": true
}

# What the service actually resolves at runtime (after partition-azure prefixes):
# → KV secret name "opendes-cosmos-endpoint", value resolved via Workload Identity.
```

The fact that the partition record value is the bare suffix and the KV secret name is the prefixed form is the bare-vs-prefixed nuance from ADR-015.

## Related ADRs

- [ADR-005](../decisions/005-workload-identity.md) -- Workload Identity for Azure PaaS Access
- [ADR-010](../decisions/010-keyvault-secret-management.md) -- Key Vault + ConfigMap Secret Model
- [ADR-011](../decisions/011-trust-manager-ca-distribution.md) -- Cross-Namespace CA Distribution via trust-manager
- [ADR-015](../decisions/015-partition-entitlements-bootstrap.md) -- Partition + Entitlements Bootstrap (bare KV suffix in partition records)

## Source files

- `infra/main.bicep` -- account-wide static KV secrets
- `infra/modules/keyvault.bicep` -- KV resource only
- `infra/modules/partition.bicep` -- per-partition KV secrets
- `src/spi/secrets.py` -- generates middleware passwords (seed + `platform`/`osdu` K8s Secrets)
- `src/spi/deploy.py` -- runtime KV writes (`_write_keyvault_bootstrap_secrets`), `osdu-config` ConfigMap, and workload-identity ServiceAccounts
- `software/stacks/osdu/bootstrap/ca-bundles.yaml` -- trust-manager Bundles + Redis DestinationRule
- `software/charts/osdu-spi-service/templates/deployment.yaml` -- `import-ca-certs` init container
- `software/charts/osdu-spi-init/templates/partition-record.yaml` -- the bare-suffix template
