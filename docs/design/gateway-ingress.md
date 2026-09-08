# Gateway and ingress

`--ingress-mode` selects how clients reach the cluster. For `core`, all modes use the managed Istio gateway; they differ in hostnames,
certificates, DNS management, and routes. `minimal` with `ip` and every `bare`
deployment have no ingress. The default is `azure`.

| Mode | Address | Edge TLS | Use |
|---|---|---|---|
| `azure` | `<label>.<region>.cloudapp.azure.com` | Let's Encrypt, one hostname | Dev/test without an owned DNS zone |
| `dns` | Environment-prefixed names in an existing Azure DNS zone | Let's Encrypt, separate hostnames | Team environments with owned DNS |
| `ip` | Gateway public IP | None | Isolated debugging only |

**`ip` mode sends API traffic, including any bearer tokens, over HTTP.** Do not
use it for sensitive data or credentials over an untrusted network.

## Shared gateway and configuration

The Gateway is `aks-istio-ingress/spi-gateway`. Its Hostname address binds to
the AKS add-on's existing `aks-istio-ingressgateway-external` Service in the
same namespace; this managed configuration does not create a per-Gateway
`spi-gateway-istio` Service. HTTPRoutes live in `osdu`;
they reference that Gateway and route to OSDU services or, with ReferenceGrants,
middleware Services in `platform`.

The CLI writes `osdu-flux/spi-ingress-config`. Mode-specific values include
`INGRESS_FQDN` and `DNS_LABEL` for `azure`, or `DNS_ZONE` and the
`INGRESS_HOST_*` values for `dns`. Flux substitutes these into the selected
ingress manifests.

The core stack installs cert-manager even in `ip` mode because Redis needs
certificates. What `ip` omits is ingress certificate issuance and HTTPS, not
the cluster's certificate controller.

## Azure-assigned hostname

For `--env dev1 --location westus3`, the default hostname is
`spi-stack-dev1-ingress.westus3.cloudapp.azure.com`.

The `spi-ingress-dns-label` Kustomization applies a partial Service manifest
that owns only the DNS-label annotation. The Azure cloud controller assigns the
public-IP DNS name. The CLI computes the expected hostname; managed-namespace
policy prevents it from writing the Service directly. The Service manifest
disables pruning and its Kustomization has `prune: false`, so a mode switch
does not delete the AKS-owned ingress.

cert-manager issues the certificate through an HTTP-01 challenge. The
Certificate and TLS Secret live in `platform`, where cert-manager can update
status. A ReferenceGrant lets the Gateway in `aks-istio-ingress` read that
Secret; the listener's certificate reference names `platform` explicitly.
OSDU APIs use service-specific paths under `/api/`. The manifests also declare
Kibana at `/kibana` and Airflow at `/airflow`; successful UI access still depends
on the backend's readiness and subpath configuration.

## Owned DNS zone

In `dns` mode, the hostname prefix defaults to `--env` and can be overridden
with `--ingress-prefix`. For environment `dev1` and zone `example.com`:

| Hostname | Backend |
|---|---|
| `dev1.example.com` | OSDU APIs |
| `dev1-kibana.example.com` | Kibana |
| `dev1-airflow.example.com` | Airflow |

This mode adds ExternalDNS in `foundation`, with its own Workload Identity
ServiceAccount. The role module deploys into the zone's resource group and
grants DNS Zone Contributor on the zone itself. ExternalDNS watches HTTPRoute
hostnames and manages DNS records using a TXT ownership registry.

The gateway has a certificate and HTTPS listener for each hostname. HTTP
remains available for ACME challenges.

The zone must already exist in the active subscription. The CLI can discover
both its name and resource group when there is exactly one zone. Ensure the
zone's public delegation and the deployer's permissions are in place; creating
an Azure DNS zone alone does not establish public delegation.

**Current limitation:** explicitly passing `--dns-zone` sets the name but
skips discovery of the zone's resource group. The CLI has no corresponding
resource-group option, and Bicep needs both values. Use automatic discovery
when the subscription has one zone. Do not treat explicit selection among
multiple zones as a complete deployment path until that input handling is fixed.

## Bare IP

The `ip` profile creates OSDU API routes on the HTTP listener without hostname
matching. It does not add ingress certificates, ExternalDNS, or middleware UI
routes. Use port-forwarding for middleware access rather than treating this as
a production exposure mode. That describes `core`: `minimal` with `ip` has
no ingress, and `bare` selects an empty ingress tree regardless of mode.

## Choosing or changing a mode

For a new environment, these commands provision infrastructure and workloads:

```bash
spi up --env dev1 --ingress-mode azure
# Requires exactly one existing Azure DNS zone in the active subscription.
spi up --env team1 --ingress-mode dns
```

Changing an existing environment uses `spi up` again with the desired mode and
the original location, partition list, repository, and branch. This re-runs
infrastructure provisioning, rewrites the ingress ConfigMap, and updates the
Flux ingress path. It is not a dedicated, zero-downtime migration operation.

The selected ingress tree is the Gateway's sole inventory owner. Its
`spi-gateway-tls` Kustomization keeps the same name across modes, changing
paths rather than deleting and recreating the owner. The base stack's
`spi-gateway` is an empty, non-pruning handoff, not another Gateway renderer.
Do not remove that handoff without the sequence described in
[ADR-025](../decisions/025-single-flux-inventory-owner.md).

Route and certificate convergence still takes time during a switch.
Azure resources omitted by an incremental Bicep redeployment are not
automatically deleted. Inspect old DNS records and identity assignments after
leaving `dns` mode. Also inspect source suspension and the applied Git revision;
a CLI exit alone does not confirm the new ingress has converged.

Reconfiguring a live environment preserves its existing service image lock by
default; only an explicit `--refresh-images` re-resolves canonical entries. For independent tests of ingress
modes, separate environments avoid changing an active endpoint.

## Diagnosing an unreachable endpoint

Start with `spi info` and confirm you are using the expected hostname and mode.
Then identify which boundary fails:

| Symptom | Inspect | What to establish |
|---|---|---|
| DNS lookup fails | `dig <hostname>`; ingress Service annotations; ExternalDNS logs in `dns` mode | The hostname resolves to the intended gateway |
| Connection fails | LoadBalancer address and Gateway conditions | The address and listener are available |
| TLS handshake fails | Certificate, CertificateRequest, Challenge, and Gateway listener status | Certificate issuance and hostname match |
| Gateway route does not match | HTTPRoute parent conditions, hostname, path, and listener | `Accepted` and `ResolvedRefs` are true |
| Backend is unavailable or a 5xx is returned | Backend reference, Service ports, EndpointSlices, pod readiness | The route targets an available backend |
| Application returns 404 | Backend logs and requested API path | The request reached the service and uses a supported route |

A completed HTTPS request returning 404 has already passed DNS resolution and
the TLS handshake. Do not treat 404 and 503 as interchangeable, or assume every
404 originates at the gateway.

These read-only commands locate the relevant conditions:

```bash
kubectl get svc aks-istio-ingressgateway-external -n aks-istio-ingress
kubectl describe gateway spi-gateway -n aks-istio-ingress
kubectl get certificate,certificaterequest,challenge -n platform
kubectl describe httproute -n osdu
kubectl get endpointslice -n osdu
```

For `dns` mode:

```bash
kubectl logs -n foundation -l app.kubernetes.io/name=external-dns --tail=50
```

For middleware routes, inspect the target Service in `platform` and the
ReferenceGrant allowing that cross-namespace reference. A route can exist in
Kubernetes without being accepted by its parent Gateway.

## Decisions and implementation

- [ADR-012](../decisions/012-ingress-profiles.md): ingress choices.
- [ADR-022](../decisions/022-tls-certificates-in-platform.md): certificates outside managed namespaces.
- [ADR-026](../decisions/026-bind-managed-istio-ingress.md): managed Service binding and Flux-owned DNS annotation.
- [Ingress resolution](../../src/spi/ingress.py) and [configuration](../../src/spi/config.py): hostname rules and ConfigMap keys.
- [Gateway](../../software/components/gateway/gateway.yaml): namespace and base listener.
- [Ingress profiles](../../software/stacks/osdu/ingress/) and [routes](../../software/stacks/osdu/routes/): dependencies and backend references.
- [TLS overlays](../../software/overlays/): certificates and HTTPS listeners.
- [ExternalDNS](../../software/components/external-dns/release.yaml): namespace, ownership registry, and identity settings.
- [DNS role module](../../infra/modules/external-dns-role.bicep): Azure authorization scope.
