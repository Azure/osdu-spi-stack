---
applyTo: "software/charts/**"
---

`osdu-spi-service` is consumed by every OSDU service HelmRelease, so a change
to it is cross-cutting. The chart bakes AKS Safeguards compliance into its
templates (see the local Helm chart record in `docs/decisions/`). Check that
these survive: `runAsNonRoot`, `seccompProfile.type: RuntimeDefault`,
`allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, resource
requests and limits, liveness and readiness probes. Init containers must
carry the same security context as the main container.
