# Release Playbook

This playbook covers **two** release streams:

1. **Cloud backend (VPS)**: central VictoriaMetrics + Grafana + Fleet API + Fleet Map UI
2. **Edge stack (field nodes)**: N100 host telemetry stack + events + UI

Key principle: **Cloud can ship often, edge must ship safely**.

---

## Roles

- **Release captain**: runs the checklist, makes go/no-go decisions
- **Triage owner**: ensures issues are prioritized and labeled
- **On-call**: monitors and verifies after deploy

---

# A) Cloud release (VPS)

Cloud releases are continuous by default: merge to `main` → build → deploy.

## Cloud deploy checklist
1. ✅ CI green on `main`
2. ✅ Compatibility check against supported edge window (see `COMPATIBILITY.md`)
3. ✅ Third-party dependencies pinned in `cloud/.env` (no :latest)
4. ✅ DB migrations are Expand/Contract (no breaking changes)
5. ✅ Deploy to staging (if available)
6. ✅ Smoke test:
   - `GET /api/health` on api
   - Grafana health (`/api/health`)
   - VictoriaMetrics status (`/api/v1/status/tsdb`)
7. ✅ Deploy to prod
8. ✅ Verify:
   - Fleet Map loads
   - VM ingest OK (remote write rows increasing)
   - error logs stable

## Cloud rollback
- Re-deploy last known-good container image digest (or previous git SHA)
- If schema-related, prefer a forward-fix unless rollback was planned

---

# B) Edge release (Field fleet)

Edge releases are SemVer tags: `edge/vMAJOR.MINOR.PATCH`.

## Rings
Use a ring rollout (even if "rings" are just a list of node IDs):
- **Lab**: a dev node that can be wiped
- **Pilot**: 1–3 real field nodes
- **Stable**: all remaining nodes

## Edge release checklist

### 1) Scope
- All included PRs are linked to issues and labeled
- No open `priority:p0` / `priority:p1` bugs

### 2) Version decision (SemVer)
- Patch/minor/major decision recorded in the release notes

### 3) Update changelog
- `CHANGELOG.md` updated for user-impacting changes

### 3.5) Pin third-party dependencies (no :latest)
Edge releases must pin third-party container images (VM, vmagent, Grafana, Telegraf, node-exporter).

Recommended workflow:
- Deploy + validate in lab using `edge/compose.dev.yml`
- Generate digests from the known-good lab node:
  - `./scripts/pin_third_party_images.sh edge edge/compose.dev.yml`
- Save the output as a file committed with the release tag:
  - `edge/pins/vX.Y.Z.env`

CI will fail if `:latest` appears in release/sandbox deploy files.

### 4) Build and publish artifacts (CI)
- Tag `edge/vX.Y.Z-rc.1` (first RC)
- GitHub Actions builds and publishes:
  - `edge-events:<version>`
  - `edge-frontend:<version>`

### 5) Lab ring
- Deploy RC to Lab
- Verify:
  - all containers healthy
  - events `/health`
  - UI loads
  - telemetry is ingesting locally and remote write is active

### 6) Pilot ring
- Promote *same artifact* to Pilot
- Monitor for regressions (logs, connectivity, operator feedback)

### 7) Stable
- Tag `edge/vX.Y.Z`
- Deploy to Stable ring

### 8) Post-release
- Confirm upgrade success rate
- Confirm cloud fleet map still shows expected metrics
- Close issues/milestone

## Edge rollback
- Roll back nodes to the previous stable edge version
- Keep at least **2 prior stable** versions available

---

# Release tooling in this repo

- CI: `.github/workflows/ci.yml`
- Edge image publish: `.github/workflows/release-edge.yml`
- Cloud image publish: `.github/workflows/release-cloud.yml`
- Edge production compose: `edge/compose.release.yml`
- Cloud production compose: `cloud/compose.release.yml`
