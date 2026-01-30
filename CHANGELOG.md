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
- Cloud event manager + report storage endpoints (events, event nodes, aliases, report listing/aggregate).
- Fleet Map event controls (start/end/merge) and event report viewer with aggregate links.
- Tile budget aggregation endpoints with provider switching + satellite disable policy, including UI counters.
- Cloud event registry endpoints and VM-backed node assignment for events.

### Changed
- Edge events can auto-generate temp event IDs when none is provided and queue report uploads for retry.
- Fleet Map reports view now focuses on event reports (monthly UI removed).
- Fleet Map event creation now uses registry names and alive-node selection.
- Default cloud metric suffix set to `:10s_avg` to align with edge stream aggregation.

### Fixed
- Fleet Map mobile layout now scrolls to the map on small screens.
- Event lists now prefer friendly event names over UUIDs.
- Map tile totals fallback now checks unsuffixed metrics and remote write includes tile counters.
- Monthly reports endpoint now emits debug metadata when empty.

### Security

## [0.0.0] - 2026-01-18

### Added
- Initial stack import (edge + cloud + provisioning + fleet map)
