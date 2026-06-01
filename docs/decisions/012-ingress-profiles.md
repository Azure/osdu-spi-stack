# ADR-012: Three Ingress Profiles (azure, dns, ip)

**Status**: Accepted

## Context

The SPI Stack has three distinct ingress audiences:

- **Fresh developer spin-up on a personal Azure subscription.** No DNS zone, no registered domain, wants a browsable URL fast.
- **A team running against an owned Azure DNS zone.** Needs per-service hostnames (osdu, kibana, airflow), real TLS certificates, and DNS records managed as workloads rotate.
- **A smoke test or skills harness.** Wants to reach an OSDU API by IP and does not care about TLS at all.

Gateway API plus managed Istio (from ADR-002) can serve all three, but the surrounding glue is materially different per mode: ACME issuers only exist where there is a hostname; ExternalDNS only makes sense where there is a zone to manage; the Gateway's HTTPS listener either has one certificate (single host) or three (multi-host).

Picking one of these at install time keeps the declared topology simple and avoids surprising partial configurations.

## Decision

Three ingress profiles, each a self-contained Flux Kustomization tree under `software/stacks/osdu/ingress/<mode>/`. The mode is selected by `--ingress-mode` on `spi up` (env var `SPI_INGRESS_MODE`, default `azure`) and plumbed into `infra/flux.bicep` as a parameter; the AKS Flux extension's `ingress` Kustomization points at the chosen path.

| Mode | Hostname source | TLS | DNS management | Use case |
|---|---|---|---|---|
| `azure` (default) | Azure-assigned `<label>.<region>.cloudapp.azure.com` | Let's Encrypt HTTP-01, single-host overlay | none | Dev spin-up; zero config |
| `dns` | `*.<user-zone>` (osdu, kibana, airflow subdomains) | Let's Encrypt HTTP-01, multi-host overlay | ExternalDNS to Azure DNS Zone | Team environments on an owned zone |
| `ip` | bare ingress IP | none | none | Smoke tests, skills, debugging |

Shared pieces (cert-manager install, Gateway resource, Istio ingress LB Service) live under `software/components/` and are brought in by all three profiles. The profile owns the variable surface:

- `azure` uses the AKS cloud controller's `azure-dns-label-name` Service annotation (propagated via the Gateway's infrastructure annotations) to pin an Azure FQDN; cert-manager issues one cert against that FQDN; Kibana is served under `/kibana` via a subpath overlay.
- `dns` provisions a second UAMI (`<cluster>-external-dns`, scoped `DNS Zone Contributor` on the target DNS zone, ADR-005). ExternalDNS reads HTTPRoute hostnames and writes A and TXT records. The Gateway has one HTTPS listener per hostname, each with its own certificate. Kibana is its own subdomain (no subpath).
- `ip` is the minimum surface: HTTPRoutes without hostnames bound to the HTTP:80 listener. No cert-manager issuers, no ExternalDNS, no Kibana routing, no TLS overlay.

Inputs per mode land in a single `spi-ingress-config` ConfigMap in `flux-system`, consumed by Flux `postBuild.substituteFrom`.

Rejected:
- **Single mode with conditional Helm or Kustomize overlays.** Produces a profile matrix that is hard to review and makes `what-if` diffs opaque. Three flat profiles are easier to read and swap.
- **Terraform-first DNS for `dns` mode.** ExternalDNS's continuous reconciliation matches HTTPRoute changes as services are added; Terraform drift-reconciles only on apply.
- **Always provision an Azure DNS zone.** Forces users to own a zone they may not have; `azure` mode works against any Azure subscription out of the box.

## Consequences

- Switching modes is a Bicep parameter change plus one Flux reconcile; no hand-edits to Kustomizations.
- Each mode's surface is self-describing. A reader sees the full topology in one `stack.yaml` plus the overlays it references.
- `dns` mode introduces a second UAMI and a DNS Zone Contributor role assignment. Both are conditional in `infra/main.bicep` on a non-empty `dnsZoneName` parameter.
- `ip` mode is intentionally low-fidelity; endpoints lose HTTPS and middleware UIs (Kibana, Airflow) are not routed. It is documented as debug-only.
- Adding a fourth mode is adding a fourth subdirectory under `software/stacks/osdu/ingress/` and a fourth enum value; no core Flux surgery.
