---
applyTo: "software/charts/**"
---

Any change under a chart directory must move `version` in that chart's
`Chart.yaml`. Flux repackages a path-sourced chart only when the version
changes, so an unbumped edit never reaches a cluster that is already running.
Flag a diff that edits templates or values without a version bump, and flag a
bump whose size does not match the change (a template behavior change is at
least a minor bump).

`osdu-spi-service` is consumed by every OSDU service HelmRelease, so a change
to it is cross-cutting. Check that the Safeguards settings ADR-004 bakes into
the templates survive: `runAsNonRoot`, `seccompProfile.type: RuntimeDefault`,
`allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, resource
requests and limits, liveness and readiness probes. Init containers must
carry the same security context as the main container.
