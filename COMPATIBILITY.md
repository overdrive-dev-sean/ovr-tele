# Compatibility Contract (Cloud ↔ Edge)

The cloud backend and the field nodes evolve independently, but **must remain interoperable**.
This document defines what "compatible" means and how we enforce it.

## Compatibility promise

### Cloud must support a window of edge versions
We support:
- **Latest edge MINOR** (all PATCHes)
- **Previous 2 edge MINORs** (all PATCHes)

Example: if latest stable edge is `1.8.x`, cloud must support `1.8.x`, `1.7.x`, and `1.6.x`.

### Edge must be able to safely upgrade/downgrade
Edge releases must allow:
- upgrade within same MAJOR without manual DB surgery
- rollback to the previous stable edge version

## Contracts we consider "public"

These are treated as APIs even if they are "just metrics":

1. **Remote write / metric schema**
   - Metric names and label keys relied on by:
     - `cloud/services/api` queries
     - Grafana dashboards
     - Fleet Map UI

2. **Event-service HTTP API** (if accessed remotely via Cloudflare tunnels)
   - `/api/*` endpoints used by operators or automation

3. **Report formats**
   - Report JSON/HTML schema when reports are uploaded or consumed centrally

## Rules

### 1) Metrics changes require a migration plan
- Do **not** rename metrics without providing a transition period.
- Prefer adding new metrics/labels, then deprecating old ones.

### 2) DB migrations use Expand/Contract
For cloud and edge services that evolve schemas (SQLite or otherwise):
- **Expand**: add new columns/paths in a backward-compatible way
- **Contract**: remove old fields only after the support window has passed

### 3) Version visibility
Every component must report version information:
- `events` `/health` includes `{ version, git_sha }`
- `api` `/api/health` includes `{ version, git_sha }`

## Enforcement mechanisms

### Soft enforcement (recommended)
- Cloud logs a warning when it sees an unknown/old edge version.
- Cloud UI displays "upgrade recommended" warnings.

### Hard enforcement (only for true incompatibility)
- Cloud may reject requests from edge versions below the supported window.
- The error must be explicit and actionable:
  - tells operator what minimum version is required
  - links to upgrade instructions

## Operational policy

### Deprecation timeline
- Deprecate for **2 MINOR releases** before removal.
- Document in `CHANGELOG.md` under **Deprecated** and later **Removed**.

### Incidents
If a cloud deploy breaks supported edge versions:
- treat as an incident
- rollback cloud or forward-fix immediately
- add an ADR if the root cause is systemic
