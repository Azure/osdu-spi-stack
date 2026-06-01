# Changelog

All notable changes to the `spi` CLI are documented here. Versions follow
[Semantic Versioning](https://semver.org/). Release notes for each tag are
also auto-generated from conventional commits and attached to the
corresponding [GitHub Release](https://github.com/Azure/osdu-spi-stack/releases).

## [0.1.0] - 2026-05-27

### Added
- Per-environment 5-char random suffix on globally unique Azure resource
  names (storage, Key Vault, ACR, Cosmos, Service Bus). Persisted as the
  `spi-name-suffix` tag on the resource group so subsequent runs reuse it;
  legacy (pre-suffix) deployments keep unsuffixed names. (`ee45a65`)
- `CODE_OF_CONDUCT.md` and `SUPPORT.md`; `CODEOWNERS` and
  `.github/copilot-instructions.md` updated with ownership and deployment
  notes. (`836a623`)
- Prime Copilot skill. (`480a9d8`)

### Fixed
- Bicep templates are now bundled inside the installed wheel
  (`spi/infra/`) via hatchling `force-include`, with a source-checkout
  fallback in `paths.py`. v0.0.1 wheels resolved `INFRA_ROOT` to a
  nonexistent path under `lib/pythonX.Y/infra/`, breaking `spi up` for
  every `uv tool install` user. (`ee45a65`)

### Changed
- `rich` minimum bumped to `>=15.0.0`. (`72c73cb`)
- `ruff` dev requirement updated. (`0d427ef`)

[0.1.0]: https://github.com/Azure/osdu-spi-stack/compare/v0.0.1...v0.1.0
