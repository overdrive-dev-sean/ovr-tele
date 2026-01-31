# Claude Handoff Document

*Coordination file for Claude instances working on edge and cloud stacks*

**Last updated:** 2026-01-30 by edge Claude
**Current state:** All feature branches merged to main, clean slate

---

## Business Context

**Overdrive Energy Solutions** provides temporary/portable power for live events, disaster relief, construction, and other applications. The goal: **reduce diesel consumption** through solar offset, battery hybridization, and peak shaving.

**Why this system exists:**
- Accurate load data is critical for sizing systems (undersized = failures, oversized = wasted cost)
- Clients need proof of value (kWh delivered, diesel saved, carbon offset)
- Operations needs real-time visibility across 100+ assets at large events
- Immediate automated reporting upon event completion

**Data sources:** Victron GX (our BESS), ACUVIM (large loads up to 400A 3-phase), Fronius (PV), other Modbus devices. Goal is vendor-agnostic universal aggregation.

**Audiences:**
- Field techs → Edge monitoring dashboard
- Operations → Dashboards + reports
- Clients → Reports + Grafana
- Everyone → Grafana (role-specific dashboards)

See `CLAUDE.md` for full technical context.

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              CLOUD VPS                                   │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │
│  │   Caddy     │  │ Victoria    │  │  Grafana    │  │  Fleet API  │    │
│  │  (TLS/Auth) │  │  Metrics    │  │             │  │  + Map UI   │    │
│  └──────┬──────┘  └──────┬──────┘  └─────────────┘  └──────┬──────┘    │
│         │                │                                  │           │
│         │    metrics.<domain>/api/v1/write (basic auth)    │           │
│         └────────────────┼──────────────────────────────────┘           │
└─────────────────────────────────────────────────────────────────────────┘
                           │
                    remote_write (downsampled :10s_avg)
                           │
┌─────────────────────────────────────────────────────────────────────────┐
│                           EDGE NODE (N100)                               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │
│  │  Telegraf   │  │  vmagent    │  │ Victoria    │  │   Events    │    │
│  │ (GX MQTT)   │  │ (scrape+fw) │  │  Metrics    │  │  Service    │    │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘    │
│         │                │                │                 │           │
│         └────────────────┴────────────────┴─────────────────┘           │
│                                   │                                      │
│  ┌─────────────┐           ┌──────┴──────┐           ┌─────────────┐    │
│  │ MQTT Broker │           │  Frontend   │           │   Grafana   │    │
│  │ (internal)  │           │   (nginx)   │           │             │    │
│  └─────────────┘           └─────────────┘           └─────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
                           │
                      MQTT (port 1883)
                           │
┌─────────────────────────────────────────────────────────────────────────┐
│                         GX DEVICES (Victron)                             │
│         Each GX runs its own MQTT broker with Venus OS topics           │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Directory Layout

```
/opt/ovr/
├── edge/                    # Edge stack (this node)
│   ├── compose.dev.yml
│   ├── services/
│   │   ├── events/          # Python Flask API
│   │   └── frontend/        # React + Vite + nginx
│   ├── telegraf/
│   ├── vmagent/
│   └── mosquitto/
├── cloud/                   # Cloud stack (VPS)
│   ├── compose.dev.yml
│   ├── compose.release.yml
│   ├── services/
│   │   ├── api/             # Python Flask API
│   │   └── map/             # React fleet map
│   └── Caddyfile
└── docs/

/etc/ovr/                    # Canonical config (persists across deploys)
├── edge.env                 # → /opt/ovr/edge/.env (symlink)
├── cloud.env                # → /opt/ovr/cloud/.env (symlink)
├── telegraf.d/              # Generated Telegraf configs
├── gx_systems.json          # GX device registry
├── targets_gx.txt           # GX hostname allowlist
└── secrets/                 # Credentials (0600)
```

---

## Integration Points

### 1. Remote Write (Edge → Cloud)

**Edge side:**
- vmagent scrapes local targets + Telegraf output
- Stream aggregation produces `:10s_avg` metrics
- Remote writes to cloud VM via basic auth

**Cloud side:**
- Caddy terminates TLS, enforces basic auth on `/api/v1/write`
- VictoriaMetrics ingests with `deployment_id`, `node_id` labels

**Config files:**
- Edge: `/etc/ovr/edge.env` (`VM_REMOTE_WRITE_URL`, credentials)
- Edge: `/etc/ovr/stream_aggr.yml`, `remote_write_cloud_relabel.yml`
- Cloud: `/etc/ovr/cloud.env` (`VM_WRITE_PASS_HASH`)
- Cloud: `Caddyfile` (basic_auth block)

**Status:** Configured but needs verification. Stream aggr only matches `vm_federate_acuvim` currently.

### 2. Report Upload (Edge → Cloud)

**Edge side:**
- Events service generates HTML reports
- POSTs to cloud `/api/reports/upload` with bearer token

**Cloud side:**
- Fleet API receives and stores reports

**Config:**
- Edge: `REPORT_UPLOAD_URL`, `REPORT_UPLOAD_TOKEN` in edge.env

**Status:** Not actively tested.

### 3. Map Tile Coordination

**Edge side:**
- Frontend proxies tile requests through events service
- Events service tracks usage, syncs with cloud for guardrails

**Cloud side:**
- Fleet API tracks global tile usage across fleet
- Provides preferred provider based on quota

**Status:** Implemented, working.

---

## API Endpoints

### Edge (`/opt/ovr/edge/services/events/app.py`)

| Endpoint | Purpose |
|----------|---------|
| `GET /api/realtime` | Live GX data from MQTT cache |
| `GET /api/gx/settings/realtime` | GX control settings (MQTT + VM fallback) |
| `POST /api/gx/settings` | Write GX settings via MQTT |
| `GET /api/summary` | System summary for dashboard |
| `GET /api/status` | Health check |
| `POST /api/event/start` | Start local event |
| `POST /api/reports/generate` | Generate report |
| `GET /api/gps/all` | GPS data from all GX devices |

### Cloud (`/opt/ovr/cloud/services/api/app.py`)

| Endpoint | Purpose |
|----------|---------|
| `GET /api/nodes` | All nodes in fleet (from VM metrics) |
| `GET /api/deployments` | Deployment list |
| `GET /api/events` | Fleet-wide events |
| `POST /api/events` | Create event in registry |
| `GET /api/events/registry` | Event registry (all events) |
| `POST /api/events/<id>/add_nodes` | Add nodes to event |
| `POST /api/reports/upload` | Receive reports from edge |
| `GET /api/reports/event/<id>` | Event reports |

---

## Current State (2026-01-30)

### Completed (on main)
- [x] Milestone 0: Env layout + symlinks
- [x] Milestone 3: MQTT broker foundation
- [x] Milestone 4: MQTT summary plane (`/api/realtime`)
- [x] Milestone 4b: MQTT control plane (GX settings)
- [x] Milestone 4c: GX MQTT via Telegraf
- [x] Milestone 4d: GX discovery automation
- [x] Milestone 4e: GX MQTT keepalive
- [x] Milestone 4f: GX Control UX (optimistic UI, MQTT confirmation)
- [x] Fleet map event registry selection (just merged)

### Not Started
- [ ] Milestone 0.5: Smoke scripts
- [ ] Milestone 1: Phase 0 networking (Wi-Fi, mDNS)
- [ ] Milestone 2: Peers + mesh proxy
- [ ] Milestone 5-7: PWA unification
- [ ] Milestone 8: Mesh transport

### Known Issues / Needs Work

1. **Remote write pipeline** - Stream aggr config only matches ACUVIM, not victron_* metrics from Telegraf
2. **Cloud ingestion** - Need to verify data is flowing edge → cloud
3. **API consistency** - Edge and cloud have different event models
4. **Alerting** - Design notes in `docs/design/ALERTING_DESIGN.md`, nothing implemented

---

## Coordination Protocol

When working in tandem:

1. **Pull before working**: `git pull origin main`
2. **Commit often**: Small, focused commits
3. **Update this file**: Note what you're working on, what's blocked
4. **Push when done**: Don't leave uncommitted work

### Work Assignment

| Area | Owner | Notes |
|------|-------|-------|
| Edge services | Edge Claude | events, frontend, telegraf |
| Cloud services | Cloud Claude | api, map, Caddy |
| Shared docs | Either | Coordinate via commits |
| Integration testing | Both | Requires both stacks running |

---

## Active Work / Blockers

*Update this section as you work*

### Edge Claude
- **Working on:** (none currently)
- **Blocked by:** (nothing)
- **Notes:** All branches merged, clean state

### Cloud Claude
- **Working on:** (not yet started)
- **Blocked by:** (nothing)
- **Notes:** Need to pull repo on VPS

---

## Quick Reference

### Edge commands
```bash
cd /opt/ovr/edge
docker compose -f compose.dev.yml up -d --build
docker compose -f compose.dev.yml logs -f events
```

### Cloud commands
```bash
cd /opt/ovr/cloud
docker compose -f compose.dev.yml up -d --build
docker compose -f compose.dev.yml logs -f api
```

### Test remote write
```bash
# On edge - check vmagent is forwarding
curl -s localhost:8429/metrics | grep vmagent_remotewrite

# On cloud - check ingestion
curl -s localhost:8428/api/v1/query?query=up | jq
```

---

## Files Changed Recently

```
2026-01-30: docs/design/ALERTING_DESIGN.md (new)
2026-01-30: ovr-tele_feature_roadmap_v3.md (updated milestone status)
2026-01-30: cloud/services/api/app.py (event resolution)
2026-01-30: cloud/services/map/src/App.jsx (registry selection)
2026-01-30: edge/services/events/app.py (realtime settings, control cache)
2026-01-30: edge/services/frontend/src/App.jsx (optimistic UI, pending state)
```
