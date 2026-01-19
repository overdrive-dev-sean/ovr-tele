# Changelog

All notable changes to this project are documented in this file.

Format is inspired by "Keep a Changelog" and uses categories:
- Added
- Changed
- Fixed
- Deprecated
- Removed
- Security

## [Unreleased]

### Added

### Changed
- Cloud compose files use `/etc/ovr/secrets` and allow external VM data volumes via `CLOUD_VM_VOLUME_NAME`.
- Edge and provisioning paths now use `/etc/ovr` for config and secrets (deprecates `/etc/overdrive`).

### Fixed
- Cloud Caddyfile proxies the API on port 8089.

### Security

## [0.0.0] - 2026-01-18

### Added
- Initial stack import (edge + cloud + provisioning + fleet map)
