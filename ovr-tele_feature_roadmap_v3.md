# ovr-tele Feature Roadmap v3
*(Unified PWA + Mesh Proxy + MQTT Summary/Control + GX MQTT/Telegraf discovery)*

This roadmap is organized to keep changes **reviewable, testable, and reversible** on real edge nodes.
It assumes your current "mostly working refactor" is living on an integration branch (ex: `feat/4-fleet-map-events`).

## Status overview (2025-01-30)

| Milestone | Status | Notes |
|-----------|--------|-------|
| 0 - Env layout + symlinks | ✅ Done | `/etc/ovr/` canonical, symlinks in place |
| 0.5 - Smoke scripts | ⬜ Not started | |
| 1 - Phase 0 networking | ⬜ Not started | |
| 2 - Peers + mesh proxy | ⬜ Not started | |
| 3 - MQTT broker foundation | ⬜ Not started | |
| 4 - MQTT summary plane | ⬜ Not started | |
| 4b - MQTT control plane | ⬜ Not started | |
| 4c - GX MQTT via Telegraf | ✅ Done | Starlark processor, dynamic config gen |
| 4d - GX discovery automation | ✅ Done | Allowlist-based (not mDNS), `refresh_gx_mqtt_sources.sh` |
| 4e - GX MQTT keepalive | ✅ Done | Integrated into refresh script, 50s timer |
| 5 - PWA service worker | ⬜ Not started | |
| 6 - Unified PWA beta | ⬜ Not started | |
| 7 - PWA cutover | ⬜ Not started | |
| 8 - Mesh transport | ⬜ Not started | |

> Core principle: **Do not ship a giant refactor all at once.**  
> We add capability in layers: host layout → Phase‑0 networking → safe peer proxy → MQTT summary/control → GX ingestion → unified PWA beta → cutover.

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

## Milestone 2 — Static peer list + safe “mesh proxy” (VM queries)
**Branch:** `feat/2-peers-and-mesh-proxy`

### What
- Define `/etc/ovr/peers.json` (static list).
- Provide `GET /api/peers` from the edge API.
- Edge nginx proxies:
  - `/api/vm/*` → local VictoriaMetrics (`:8428`)
  - `/api/peers/<peer_id>/vm/*` → peer VictoriaMetrics (`<peer_host>:8428`) via whitelist map (no SSRF)
- Minimal “Peer Test” UI page (no charts).

### Why
- This is the core low-bandwidth data path: **UI talks to local node only**, node routes requests.

### Acceptance
- Local VM query works via `/api/vm/...`
- Peer VM query works via `/api/peers/<id>/vm/...`
- Unknown peer_id returns 404 (safe).

---

## Milestone 3 — MQTT broker foundation (internal only)
**Branch:** `feat/3-mqtt-broker-foundation`

### What
- Add a local Mosquitto broker container to edge stack.
- Default: no host port mapping (internal only).
- Persist messages (if desired) and document topic namespace.

### Why
- Establish “realtime/control plane” foundation without exposing ports or adding mesh traffic.

### Acceptance
- Broker runs; publish/subscribe works from other containers on the compose network.
- No firewall changes required (internal only).

---

## Milestone 4 — MQTT summary plane + cached HTTP `/api/realtime`
**Branch:** `feat/4-mqtt-summary-plane`

### What
- Add `mqtt-summary` service that:
  - polls a trusted local source (recommended: `events` `/api/summary`)
  - publishes a **tiny** JSON payload at a controlled interval (2–10s)
  - serves cached JSON at `GET /api/realtime` (so PWA can poll cheaply over mesh)
- Add nginx route:
  - `/api/realtime` → `mqtt-summary` service
- Keep payload **small and versioned**:
  - topic: `ovr/<deployment>/<node>/summary/v1`
  - payload: `{ts, system_id, soc, pin, pout, alarms_count, last_seen_ms, ...}`

### Why
- Gives you “live view” without pounding VM and without pushing full telemetry across mesh.

### Acceptance
- `/api/realtime` returns valid cached JSON quickly.
- MQTT topic updates at configured interval.
- Payload size remains small and stable.

---

## Milestone 4b — MQTT control plane (Overdrive-safe commands only)
**Branch:** `feat/4b-mqtt-control-plane`

### What
- Add `mqtt-control` service that subscribes to:
  - `ovr/<deployment>/<node>/cmd/v1`
- Allowed actions: **Overdrive-safe** operations only (events/notes/location) — *not inverter power controls yet*.
- Translate commands → existing local HTTP APIs, publish ack:
  - `ovr/<deployment>/<node>/ack/v1`

### Why
- Gives you a clean command bus for field ops without coupling PWA to device protocols.

### Acceptance
- Valid commands produce expected HTTP calls and ack `ok=true`.
- Invalid commands ack `ok=false` with error.

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

## Milestone 5 — PWA service worker: cache the app shell
**Branch:** `feat/5-pwa-app-shell-cache`

### What
- Register service worker.
- Cache app shell assets (JS/CSS/icons) so UI downloads once.
- Preserve map tile caching behavior.

### Why
- Solves the “UI assets crossing mesh” problem even before full UI unification.

### Acceptance
- After first load, UI loads offline (shell).
- Navigating doesn’t re-download bundles.

---

## Milestone 6 — Unified PWA as `/beta` (parallel deployment)
**Branch:** `feat/6-unified-pwa-beta`

### What
- Create a new shared UI package (e.g., `web/unified-pwa/`) and deploy it under `/beta`.
- Add `/api/capabilities` on edge + cloud for runtime feature flags.
- Beta UI uses:
  - `/api/peers` for peer list
  - `/api/peers/<id>/vm/...` for history queries
  - `/api/realtime` for low-cost live view
- Keep legacy UI untouched.

### Why
- Safe field testing without risking the “main” UI.

### Acceptance
- `/beta` works on edge and cloud.
- Legacy UIs still work.

---

## Milestone 7 — Cutover unified PWA as default (keep rollback)
**Branch:** `feat/7-unified-pwa-cutover`

### What
- Make unified PWA the default UI.
- Keep legacy UI reachable at `/legacy` for at least one release.

### Acceptance
- Default works on mobile + desktop.
- `/legacy` exists as rollback path.

---

## Milestone 8 — Mesh transport upgrades (later)
**Branch:** `feat/8-mesh-underlay-overlay`

### What
- Underlay: routed interfaces only (no L2 bridging).
- Babeld routing for lossy links.
- Nebula overlay addressing (example scheme: `10.231.<site>.<node>`).

### Why
- Stabilizes routing across HaLow noise and weird upstream subnets.
- Peer list host fields can transition from LAN IPs → Nebula IPs without PWA changes.

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
