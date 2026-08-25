# ADR-004: Local Helm Chart for Safeguards Compliance

## Context

ADR-002's choice of AKS Automatic means every pod must pass a non-bypassable `ValidatingAdmissionPolicy`: non-root, seccomp `RuntimeDefault`, all capabilities dropped, `allowPrivilegeEscalation: false`, resource requests and limits set, liveness and readiness probes declared.

Upstream OSDU community Helm charts published to the OCI registry do not set these security contexts. Patching them at deploy time via `kustomize postrender` or HelmRelease `postRenderers` is brittle: it couples our deploy pipeline to each chart's template layout and breaks silently on chart upgrades.

## Decision

Ship one local Helm chart (`software/charts/osdu-spi-service/`) that every OSDU service HelmRelease consumes. The chart bakes Safeguards compliance into its templates at authoring time; per-service HelmReleases supply only image, env, and resource overrides.

Baked into the chart:

- Pod-level `securityContext`: `runAsNonRoot`, `seccompProfile.type: RuntimeDefault`, topology spread constraints.
- Container-level `securityContext`: `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `runAsUser: 1000`.
- Init containers (CA truststore build, schema fetch) inherit the same security context.
- Required probes, resources, and env plumbing.

Rejected: reuse upstream community charts and patch at render time. The failure mode (a chart bump silently drops compliance) is worse than the cost of one local chart.

## Consequences

- Compliance is guaranteed at authoring time. Admission rejections during reconcile are a drift bug in our chart, not a surprise from upstream.
- One chart covers all OSDU services; per-service differences live in HelmRelease `values`.
- Upstream chart changes do not affect our deployments. We follow upstream image tags through the CLI-generated `osdu-image-lock` ConfigMap, not chart versions.
- A chart change is a cross-cutting change; reviews must weigh the blast radius across every service HelmRelease.
