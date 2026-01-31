# ovr-tele Feature Roadmap v3
*(Unified PWA + Mesh Proxy + MQTT Summary/Control + GX MQTT/Telegraf discovery)*

This roadmap is organized to keep changes **reviewable, testable, and reversible** on real edge nodes.
It assumes your current "mostly working refactor" is living on an integration branch (ex: `feat/4-fleet-map-events`).

## Status overview (2026-01-31)

### Core Pipeline (Done)

| Milestone | Status | Notes |
|-----------|--------|-------|
| 0 - Env layout + symlinks | ✅ Done | `/etc/ovr/` canonical, symlinks in place |
| 3 - MQTT broker foundation | ✅ Done | Internal Mosquitto + cloud bridge |
| 4 - MQTT summary plane | ✅ Done | `/api/realtime` with GX + ACUVIM |
| 4b - MQTT control plane | ✅ Done | MQTT-based GX control |
| 4c - GX MQTT via Telegraf | ✅ Done | Starlark processor, dynamic config |
| 4d - GX discovery automation | ✅ Done | Allowlist-based, `refresh_gx_mqtt_sources.sh` |
| 4e - GX MQTT keepalive | ✅ Done | Integrated into refresh script |
| 4f - GX Control UX | ✅ Done | Optimistic UI, MQTT confirmation |

### Multi-Node / Unified UI (Not Started)

These form one cohesive package for multi-node field deployments:

| Phase | Milestone | Status | What |
|-------|-----------|--------|------|
| A | 2 - Peers + mesh proxy | ⬜ | Node-to-node query routing |
| B | 6 - Unified PWA | ⬜ | Single UI for edge + cloud (includes service worker) |
| C | 7 - Cutover | ⬜ | Make unified UI default |
| D | 8 - Mesh transport | ⬜ | Nebula/Babel for robust routing |

A and B can happen in parallel. C needs B tested. D can happen anytime after A.

### Optional / Lower Priority

| Milestone | Status | Notes |
|-----------|--------|-------|
| 0.5 - Smoke scripts | ⬜ | Health check scripts |
| 1 - Phase 0 networking | ⬜ | Multi-SSID, mDNS (nice-to-have) |

> Core principle: **Do not ship a giant refactor all at once.**
> We add capability in layers: core pipeline (done) → peer proxy → unified PWA → cutover → mesh hardening.

---

## Guiding architecture

### Planes
- **History / Query plane (high-rate):** VictoriaMetrics on each node (authoritative for that node)
- **Realtime / Control plane (low-rate):** MQTT on each node (internal broker + curated topics only)
- **Transport plane:** Phase‑0 LAN/Wi‑Fi now; Nebula/Babel later (no L2 bridging)
- **UI plane:** PWA shell cached once; UI talks HTTP to *local node only* (proxy does the rest)

### Non-goals (until later)
- No broker-to-broker “full mesh” bridging (easy to melt bandwidth + create loops).
- No vmagent scrape target changes as part of Phase‑0 or MQTT foundation work.
- No “browser MQTT client” required (keep PWA lightweight; let node do aggregation).

---

## Branch naming & merge workflow

**Integration branch:** keep current working baseline in `feat/4-fleet-map-events` (or whatever you use).

For each milestone:
1. Create short-lived branch **off integration branch**
2. PR back into integration branch
3. Field test
4. Only after Milestones 0–6 stabilize, merge integration → `main`

---

# Milestones

## Milestone 0 — Standardize env layout + symlinks ✅
**Branch:** `chore/0-env-layout-ovr-symlinks`
**Status:** Complete (merged to main 2025-01-30)

### What
- Make `/etc/ovr` the canonical env/config home.
- Create symlinks:
  - `/opt/ovr/edge/.env  -> /etc/ovr/edge.env`
  - `/opt/ovr/cloud/.env -> /etc/ovr/cloud.env`
- Resolve `/etc/overdrive` vs `/etc/ovr` drift safely (symlink aliasing; no destructive deletes).
- Fix docs that reference wrong paths (e.g., old `/opt/edge/` instead of `/opt/ovr/edge/`).

### Why
- Prevents “works on one host but not another” due to env/config location mismatch.
- Makes compose usage consistent (`.env` discovered automatically).

### Acceptance
- Fresh provision results in `/etc/ovr` existing and the `/opt/ovr/*/.env` symlinks correct.
- Edge + cloud compose start without needing `--env-file`.

---

## Milestone 0.5 — “Careful checks”: smoke scripts
**Branch:** `chore/0.5-smoke-check-scripts`

### What
- Add `scripts/smoke_edge.sh` and `scripts/smoke_cloud.sh`.
- They verify:
  - required containers running
  - key ports listening
  - key HTTP endpoints return expected JSON
- Read-only checks (no mutations). Offline safe.

### Why
- You want careful checks every time—these become your “flight checklist.”

### Acceptance
- Scripts exit 0 on healthy stacks and non-zero on failures with clear output.

---

## Milestone 1 — Phase 0 networking: multi-SSID + optional mDNS + opt-in Wi‑Fi management
**Branch:** `feat/1-phase0-wifi-mdns-firewall`

### What
- Multi-SSID auto-join (NetworkManager profiles) from a file (e.g., `/etc/ovr/wifi_networks.csv`).
- Optional Avahi (mDNS) on Debian 13 for LAN discovery.
- Optional firewall toggles:
  - `ALLOW_WIFI_MGMT=1` opens management ports on Wi‑Fi interface
  - `ALLOW_MDNS=1` allows UDP 5353 on chosen mgmt interfaces
- GX guidance (doc + optional helper script) to set GX Avahi host-name to match `system_id`.

### Why
- Enables offline local operations on a utility router.
- Enables deterministic discovery workflows (especially for GX devices).

### Acceptance
- Node joins one of several known SSIDs automatically.
- With flags enabled, mDNS works *on the same subnet* and management UI is reachable.
- Default remains hardened when flags are off.

---

# Multi-Node / Unified UI Package (Milestones 2, 6, 7, 8)

**The Goal:** One UI that works everywhere—on your phone at a 10-node field site, or from HQ looking at the whole fleet.

**The Problem:** At large events, you have multiple edge nodes spread across a site. Each has its own data. A field tech connected to one node wants to see data from *all* nodes without:
- Every node needing internet
- Browser making direct connections to every node
- Hammering bandwidth across flaky Wi-Fi/mesh links

**The Solution:** Node-to-node proxy + unified UI + optional mesh hardening.

```
Phone/Tablet
     │
     ▼ (connects to nearest node only)
┌─────────────┐
│   Node A    │◄── You're here
│  (proxy)    │
└─────┬───────┘
      │ /api/peers/node-b/vm/query?...
      ▼
┌─────────────┐     ┌─────────────┐
│   Node B    │     │   Node C    │
│  (has data) │     │  (has data) │
└─────────────┘     └─────────────┘
```

**Build Order:**
| Phase | Milestone | What | Can Start |
|-------|-----------|------|-----------|
| A | 2 | Peer proxy | Now |
| B | 6 | Unified PWA | After A (or parallel) |
| C | 7 | Cutover | After B tested |
| D | 8 | Mesh transport | After A works on Wi-Fi |

---

## Milestone 2 — Peers + Mesh Proxy (Phase A of Multi-Node)
**Branch:** `feat/2-peers-and-mesh-proxy`

### What
- `/etc/ovr/peers.json` — static peer list (who's on this site)
- `GET /api/peers` — expose peer list to UI
- Proxy routes in nginx:
  - `/api/vm/*` → local VictoriaMetrics (`:8428`)
  - `/api/peers/<peer_id>/vm/*` → that peer's VictoriaMetrics (whitelist only, no SSRF)
- Minimal "Peer Test" UI page (no charts, just verify connectivity)

### Why
This is the plumbing. Without it, UI can't get data from other nodes. The key insight: **UI talks to one node only, that node proxies everything else.**

### Acceptance
- Local VM query works via `/api/vm/...`
- Peer VM query works via `/api/peers/<id>/vm/...`
- Unknown peer_id returns 404 (safe)

---

## Milestone 3 — MQTT broker foundation (internal only) ✅
**Branch:** `feat/3-mqtt-broker-foundation`
**Status:** Complete (merged to main 2026-01-31)

### What (implemented)
- Added Mosquitto broker container to edge stack
- No host port mapping (internal Docker network only)
- Persistence enabled for retained messages
- Config at `mosquitto/mosquitto.conf`

### Acceptance ✅
- Broker runs; publish/subscribe works from other containers
- No firewall changes required (internal only)

---

## Milestone 4 — MQTT summary plane + cached HTTP `/api/realtime` ✅
**Branch:** `feat/4-mqtt-summary-plane`
**Status:** Complete (merged to main 2026-01-31)

### What (implemented)
- Events service subscribes directly to GX MQTT (no separate mqtt-summary service needed)
- Background worker connects to all discovered GX devices via paho-mqtt
- Caches SOC, voltage, power, mode from MQTT messages in memory
- New `/api/realtime` endpoint returns cached data instantly (no VM query)
- Payload: `{systems: [{system_id, soc, voltage, pin, pout, mode, ts, stale}, ...], ts}`

### Why
- Dashboard updates without hitting VictoriaMetrics
- Sub-second latency for live values

### Acceptance ✅
- `/api/realtime` returns valid cached JSON instantly
- Data refreshes in realtime via MQTT subscription
- Payload is small and includes staleness indicator

---

## Milestone 4b — MQTT control plane ✅
**Branch:** `feat/4b-mqtt-control-plane`
**Status:** Complete (merged to main 2026-01-31)

### What (implemented)
- GX control via MQTT using `mosquitto_pub` from events container
- Settings: inverter_mode, battery_charge_current, ac_input_current_limit, inverter_output_voltage
- Frontend system selector dropdown for multi-unit control
- Host networking enables direct GX device access

### Acceptance ✅
- MQTT writes to GX devices work for all 4 settings
- Multi-system support with system_id selector
---

## Milestone 4c — GX MQTT ingestion via Telegraf ✅
**Branch:** `feat/4c-telegraf-gx-mqtt-ingest`
**Status:** Complete (merged to main 2025-01-30)

### What (implemented)
- Telegraf MQTT consumer reads from GX brokers on port 1883
- Starlark processor (`telegraf/processors/victron_mqtt.star`) transforms raw MQTT topics into semantic metric names:
  - Input: `N/<portal_id>/<service>/<instance>/<path...>`
  - Output: `victron_<service>_<path>` with `service`, `instance`, `phase` tags
- Config generated dynamically to `/etc/ovr/telegraf.d/gx_mqtt_sources.conf`
- 136+ victron_* metrics verified flowing into VictoriaMetrics

### Acceptance ✅
- Telegraf reads GX MQTT topics
- Writes metrics into local VM
- Cardinality controlled via Starlark processor (topic tag removed, encoded in metric name)

---

## Milestone 4d — GX discovery automation ✅
**Branch:** `feat/4d-gx-mdns-discovery-autoconfig`
**Status:** Complete (merged to main 2025-01-30)

### What (implemented)
- **Allowlist-based discovery** instead of mDNS (GX devices don't advertise `_mqtt._tcp`)
- `targets_gx.txt` contains hostname allowlist (mDNS `.local` names)
- `scripts/refresh_gx_mqtt_sources.sh`:
  1) Resolves hostnames via `getent hosts`
  2) Checks MQTT port 1883 reachability
  3) Discovers portal ID via `mosquitto_sub`
  4) Generates `/etc/ovr/telegraf.d/gx_mqtt_sources.conf`
- Systemd timer runs every 50 seconds

### Why
- GX devices don't advertise MQTT via mDNS, so allowlist approach is more reliable
- Still allows dynamic config generation based on what's reachable

### Acceptance ✅
- Reachable GX devices get config generated
- Unreachable devices are skipped (no stale configs)
- Telegraf config updates automatically

---

## Milestone 4e — GX MQTT keepalive helper ✅
**Branch:** `feat/4e-gx-mqtt-keepalive-helper`
**Status:** Complete (merged to main 2025-01-30)

### What (implemented)
- Keepalive integrated into `refresh_gx_mqtt_sources.sh` (not a separate service)
- After discovering portal ID, sends `mosquitto_pub -t "R/${portal_id}/keepalive" -m ''`
- Runs every 50 seconds via systemd timer (`ovr-refresh-gx-mqtt-sources.timer`)
- Well under the 60-second GX timeout

### Why
- GX MQTT requires periodic keepalive or it stops publishing
- Combining with discovery script keeps it simple (one timer, one script)

### Acceptance ✅
- GX devices continue publishing metrics steadily
- No republish storms (empty message body)

---

## Milestone 4f — GX Control UX improvements ✅
**Branch:** `main`
**Status:** Complete (merged to main 2026-01-31)

### What (implemented)
- New `/api/gx/settings/realtime` endpoint with MQTT cache + VictoriaMetrics fallback
- Source tracking field (`mqtt`, `vm`, `none`) to distinguish real device confirmations from stale data
- Frontend optimistic UI updates (instant visual feedback when changing settings)
- Pending confirmation state with amber color and "(confirming...)" indicator
- MQTT-only confirmation logic (only confirms when GX device publishes new value via MQTT)
- Fixed nginx proxy to use Docker gateway IP for events service on host network

### Why
- Settings now load instantly from MQTT cache instead of slow VM queries
- Users see immediate feedback when changing settings
- Visual distinction between "sent" (amber) and "confirmed by device" (normal) states
- Prevents false confirmations from stale VictoriaMetrics data

### Acceptance ✅
- Settings load instantly from MQTT cache with VM fallback
- Changing a setting shows immediate optimistic update in amber
- Confirmation only triggers when GX device confirms via MQTT (source: mqtt)
- Timeout after 20s clears pending state with warning

---

## Milestone 6 — Unified PWA (Phase B of Multi-Node)
**Branch:** `feat/6-unified-pwa`
**Depends on:** Milestone 2 (peer proxy)

> *Note: Old Milestone 5 (PWA service worker) is folded into this. Service worker caching only matters once you have a unified UI.*

### What
- Create shared UI package (`web/unified-pwa/`) deployed at `/beta` initially
- **Service worker** for offline shell + asset caching
- `/api/capabilities` endpoint for runtime feature detection (am I edge or cloud?)
- UI logic adapts based on context:
  - **Edge:** `/api/peers` + `/api/peers/<id>/vm/...` + `/api/realtime`
  - **Cloud:** `/api/nodes` + MQTT WebSocket for fleet view
- Same components, different data sources
- Keep legacy UIs at current paths during beta

### Why
- One UI for field techs (multi-node on-site) and ops (fleet view from HQ)
- Cached shell means UI survives brief disconnects on flaky mesh
- Safe field testing at `/beta` before cutover

### Acceptance
- `/beta` works on both edge and cloud
- Service worker caches shell; subsequent loads are instant/offline-capable
- Legacy UIs still work at original paths
- UI correctly detects edge vs cloud and adjusts data fetching

---

## Milestone 7 — Cutover (Phase C of Multi-Node)
**Branch:** `feat/7-unified-pwa-cutover`
**Depends on:** Milestone 6 tested in field

### What
- Make unified PWA the default at `/`
- Move legacy UIs to `/legacy` (rollback path for one release)

### Acceptance
- Default works on mobile + desktop
- `/legacy` exists as rollback path

---

## Milestone 8 — Mesh Transport (Phase D of Multi-Node)
**Branch:** `feat/8-mesh-underlay-overlay`
**Depends on:** Milestone 2 working on basic Wi-Fi

### What
- **Babeld** — routing protocol for lossy wireless links
- **Nebula** — overlay network with predictable addressing (`10.231.<site>.<node>`)
- **No L2 bridging** — routed only (prevents broadcast storms on mesh)

### Why
- Stabilizes routing across HaLow noise and weird upstream subnets
- Peer list transitions from LAN IPs → Nebula IPs without UI changes
- The proxy layer (M2) is transport-agnostic, so hardening transport is independent

### When
Can start anytime after Milestone 2 works on basic Wi-Fi. Doesn't block PWA work.

---

## Optional later milestones
- **Headless charts:** `feat/x-headless-vm-graphs` (uPlot)
- **Broker bridging (summary-only):** only after stable and carefully filtered

---

# Notes on your specific question (Telegraf vs bridge)
The roadmap favors:
- Telegraf consuming directly from GX MQTT brokers (selective topics/fields) → VM
- Internal node MQTT broker only for Overdrive summary/control plane
- No “raw GX broker bridging” unless you need it later for debugging/3rd party consumers

This keeps mesh bandwidth under control and keeps responsibilities clear.

---

## Where to put configuration (suggested)

### Directory structure
```
/opt/ovr/
├── edge/          # Edge stack (field node)
│   ├── .env -> /etc/ovr/edge.env
│   ├── docker-compose.yml
│   ├── scripts/
│   ├── telegraf/
│   └── systemd/
└── cloud/         # Cloud stack (VPS)
    ├── .env -> /etc/ovr/cloud.env
    └── docker-compose.yml

/etc/ovr/          # Canonical config home (persists across deploys)
├── edge.env
├── cloud.env
├── telegraf.d/    # Generated Telegraf configs
│   ├── gx_mqtt_sources.conf
│   └── acuvim_*.conf
├── targets_gx.txt       # GX device allowlist (optional override)
├── targets_acuvim.txt   # ACUVIM IP list (optional override)
├── peers.json           # Peer routing (Milestone 2)
└── wifi_networks.csv    # Known Wi-Fi (Milestone 1)
```

### Config file locations
- `/etc/ovr/edge.env` and `/etc/ovr/cloud.env` are canonical env files
- `/etc/ovr/telegraf.d/` for generated Telegraf configs (Milestone 4c/4d)
- `/etc/ovr/peers.json` for peer routing (Milestone 2)
- `/etc/ovr/wifi_networks.csv` for known Wi‑Fi (Milestone 1)
- `/etc/ovr/targets_gx.txt` for GX allowlist (overrides repo default)
- `/etc/ovr/targets_acuvim.txt` for ACUVIM IPs (overrides repo default)

---
