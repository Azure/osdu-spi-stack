# Gateway and Ingress

**What this explains.** What `--ingress-mode azure`, `--ingress-mode dns`, and `--ingress-mode ip` provision concretely, how to switch between them on an existing cluster, and how to debug a 404 or a TLS error.

**Why it matters.** Three modes look like three flags but they swap out cert-manager issuers, ExternalDNS, TLS overlays, and HTTPRoute hostnames. Knowing which mode is live tells you which moving parts are wired and which are deliberately absent.

> **Companion docs.** [Bicep architecture](bicep-architecture.md) explains the `external-dns-*` modules and how `--ingress-mode` is plumbed into `infra/flux.bicep`. [Flux reconciliation](flux-reconciliation.md) covers how the `ingress` Kustomization layers on top of the `stack` Kustomization.

## The three modes at a glance

| Mode | Hostname source | TLS | DNS management | Use case |
|---|---|---|---|---|
| `azure` (default) | Azure-assigned `<label>.<region>.cloudapp.azure.com` | Let's Encrypt HTTP-01, single-host | none | Dev spin-up, zero-config |
| `dns` | `*.<user-zone>` (osdu, kibana, airflow subdomains) | Let's Encrypt HTTP-01, multi-host | ExternalDNS to Azure DNS Zone | Team environments on an owned zone |
| `ip` | bare ingress IP, no hostname | none | none | Smoke tests, skills, debug |

Each mode is a self-contained Flux Kustomization tree under `software/stacks/osdu/ingress/<mode>/`. The mode is selected by `--ingress-mode` on `spi up` (env var `SPI_INGRESS_MODE`, default `azure`) and plumbed into `infra/flux.bicep` as the `ingress` Kustomization path. See [ADR-012](../decisions/012-ingress-profiles.md).

## Shared pieces (all three modes)

Some pieces are in every mode and live under `software/components/`:

- **Managed Istio** from AKS Automatic (ADR-002). Provides the Gateway API implementation and the ingress LoadBalancer service.
- **`Gateway` resource** in the `aks-istio-ingress` namespace. The Gateway listens on HTTP:80; the HTTPS listener is added by the mode-specific Kustomization (since it needs a cert that depends on the hostname).
- **cert-manager** for any mode that issues TLS (`azure`, `dns`).
- **`spi-ingress-config` ConfigMap** in `flux-system`, written by the CLI during K8s bootstrap. Carries `GATEWAY_HOSTNAME`, `GATEWAY_LABEL`, `DNS_ZONE`, and similar values consumed by Flux `postBuild.substituteFrom`.

## Mode: `azure` (default)

Two artifacts make this mode work end-to-end:

1. **`azure-dns-label-name` annotation on the Istio ingress LB.** The Gateway's `infrastructure.annotations` carries `service.beta.kubernetes.io/azure-dns-label-name: <label>`. The AKS cloud controller propagates this onto the LB Service, which gives the LB a `<label>.<region>.cloudapp.azure.com` FQDN.
2. **Single-host cert-manager `Certificate`.** A `Certificate` for `<label>.<region>.cloudapp.azure.com` issued by a `ClusterIssuer` that uses HTTP-01 against the Gateway. cert-manager handles the ACME dance; once the cert is issued, the Gateway's HTTPS listener is patched to use it.

Routing in this mode: every OSDU API is reached at `https://<label>.<region>.cloudapp.azure.com/api/<service>/v1/...`. Kibana is served at `https://<label>.<region>.cloudapp.azure.com/kibana` via a subpath overlay. Airflow is not externally routed in this mode (use `kubectl port-forward` if you need its UI).

What `software/stacks/osdu/ingress/azure/` lands:

- A `Kustomization` for cert-manager issuers (Let's Encrypt staging + prod).
- A `Kustomization` for the single-host `Certificate` and the HTTPS listener patch.
- HTTPRoutes for every OSDU service path, plus the Kibana subpath route.

This mode requires zero Azure outside the resource group: no DNS zone, no public IP outside the AKS LB, no extra UAMI.

## Mode: `dns`

Two more pieces in addition to `azure`'s setup:

1. **A second UAMI (`<cluster>-external-dns`)** scoped `DNS Zone Contributor` on the DNS zone (the role assignment binds to the zone; the module deploys into the zone's resource group). Provisioned by `infra/modules/external-dns-identity.bicep` and `infra/modules/external-dns-role.bicep`, conditional on a non-empty `dnsZoneName` parameter. The CLI requires `SPI_INGRESS_DNS_ZONE` (or `--dns-zone`) when mode is `dns`.
2. **ExternalDNS deployment** in `software/stacks/osdu/ingress/dns/`. Reads HTTPRoute hostnames and writes A and TXT records to the Azure DNS zone. Pod runs as the second UAMI via Workload Identity.

Hostname layout:

| Subdomain | Serves |
|---|---|
| `osdu.<zone>` | All OSDU service APIs |
| `kibana.<zone>` | Kibana UI |
| `airflow.<zone>` | Airflow UI (when enabled) |

The Gateway has one HTTPS listener per hostname, each with its own `Certificate`. cert-manager handles all three. ExternalDNS sees the HTTPRoute creation and writes the matching A record within ~60 seconds.

What `software/stacks/osdu/ingress/dns/` lands:

- cert-manager issuers (same as `azure`).
- ExternalDNS HelmRelease with the UAMI ServiceAccount.
- Three `Certificate` resources and three HTTPS listeners on the Gateway.
- HTTPRoutes scoped per subdomain.

## Mode: `ip`

Intentionally minimal. The Istio ingress LB has a public IP; no hostname, no cert-manager, no ExternalDNS, no HTTPS.

What `software/stacks/osdu/ingress/ip/` lands:

- HTTPRoutes bound to the HTTP:80 listener with no `hostnames` field.
- No cert issuer.
- No Kibana, no Airflow UI routing (the workloads still exist; you reach them via port-forward).

The CLI documents this as debug-only. You will hit "insecure HTTP" warnings in browsers and have no way to expose Kibana or Airflow without port-forwarding.

## Switching modes on an existing cluster

`--ingress-mode` is a Bicep parameter on `infra/flux.bicep`. Switching is one CLI invocation:

```bash
uv run spi up --env dev1 --ingress-mode dns --dns-zone example.com
```

The CLI:

1. Re-deploys `infra/flux.bicep` with the new `ingressMode` parameter. The `fluxConfigurations` resource updates the `ingress` Kustomization path to `./software/stacks/osdu/ingress/dns`.
2. Re-deploys `infra/main.bicep` to materialise `external-dns-identity` and `external-dns-role` if not already present.
3. Re-applies `spi-ingress-config` with the new values.
4. Reconciles. The old mode's Kustomization is pruned by Flux (its resources are deleted); the new mode's Kustomization installs.

`spi info` then shows the new endpoints.

## Worked example: debug a 404 in `azure` mode

You curl `https://<label>.<region>.cloudapp.azure.com/api/partition/v1/partitions/test` and get a 404 from the Gateway.

Five things to check in order:

1. **DNS resolves.** `dig <label>.<region>.cloudapp.azure.com`. If empty, the AKS LB Service does not have the DNS label annotation; check `kubectl get svc -n aks-istio-ingress -o yaml`.
2. **TLS handshake completes.** `curl -vI https://<label>...`. If TLS errors, cert-manager has not issued. `kubectl describe certificate -n aks-istio-ingress` shows the ACME state.
3. **The HTTPRoute exists and is accepted.** `kubectl get httproute -n osdu`. The `Accepted` condition should be `True`. If the Gateway rejected it (hostname mismatch), the message tells you which field is wrong.
4. **The backend Service has endpoints.** `kubectl get endpoints -n osdu`. If the service has no ready pods, the 404 is actually a 503 wearing 404 clothing.
5. **The path matches what the service expects.** OSDU APIs live under `/api/<service>/v1/...`. The HTTPRoute is path-prefix-based, not regex, so a typo in the path is a 404.

Most 404s are item 3 or item 5. Item 1 catches mode switches; item 2 catches Let's Encrypt rate limits.

## Worked example: debug a 404 in `dns` mode

Same drill, plus one: **ExternalDNS wrote the A record.** `kubectl logs deploy/external-dns -n foundation | tail` shows what it did. If it has not written anything, the HTTPRoute hostname is not in the form ExternalDNS expects (`<sub>.<zone>` with the zone exactly matching `--dns-zone`).

## Related ADRs

- [ADR-002](../decisions/002-aks-automatic.md) -- AKS Automatic (managed Istio + Gateway)
- [ADR-005](../decisions/005-workload-identity.md) -- Workload Identity (second UAMI for ExternalDNS)
- [ADR-006](../decisions/006-three-namespace-model.md) -- Three-namespace model (Gateway in `aks-istio-ingress`)
- [ADR-012](../decisions/012-ingress-profiles.md) -- Three Ingress Profiles

## Source files

- `software/stacks/osdu/ingress/azure/` -- the default mode
- `software/stacks/osdu/ingress/dns/` -- the multi-host mode
- `software/stacks/osdu/ingress/ip/` -- the debug mode
- `software/stacks/osdu/routes/single-host/`, `routes/multi-host/`, `routes/ip-only/` -- HTTPRoute overlays
- `software/components/gateway/` -- the shared Gateway resource
- `infra/modules/external-dns-identity.bicep`, `infra/modules/external-dns-role.bicep` -- the conditional UAMI + role
- `src/spi/ingress.py` -- CLI logic for `--ingress-mode`
- `infra/flux.bicep` -- carries `ingressMode` as a Bicep parameter
