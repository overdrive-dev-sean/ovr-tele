# ovr-tele Feature Roadmap v3
*(Unified PWA + Mesh Proxy + MQTT Summary/Control + GX MQTT/Telegraf discovery)*

This roadmap is organized to keep changes **reviewable, testable, and reversible** on real edge nodes.
It assumes your current “mostly working refactor” is living on an integration branch (ex: `feat/4-fleet-map-events`).

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

## Milestone 0 — Standardize env layout + symlinks
**Branch:** `chore/0-env-layout-ovr-symlinks`

### What
- Make `/etc/ovr` the canonical env/config home.
- Create symlinks:
  - `/opt/edge/.env  -> /etc/ovr/edge.env`
  - `/opt/cloud/.env -> /etc/ovr/cloud.env`
- Resolve `/etc/overdrive` vs `/etc/ovr` drift safely (symlink aliasing; no destructive deletes).
- Fix docs that reference wrong paths (e.g., `/opt/edge/edge`).

### Why
- Prevents “works on one host but not another” due to env/config location mismatch.
- Makes compose usage consistent (`.env` discovered automatically).

### Acceptance
- Fresh provision results in `/etc/ovr` existing and the `/opt/*/.env` symlinks correct.
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

## Milestone 4c — GX MQTT ingestion via Telegraf (optional, incremental)
**Branch:** `feat/4c-telegraf-gx-mqtt-ingest`

> This milestone is **optional** and can be staged in two parts:
> - 4c.1: wiring + config disabled by default
> - 4c.2: enable on specific nodes after validating GX topic formats

### What
- Add Telegraf MQTT consumption (from one or more GX brokers) to write into local VM.
- Keep existing exporter/vmagent flow intact until fully validated.
- Keep MQTT ingest config **disabled by default** (e.g., `.disabled` file) until confirmed.

### Why
- Allows curated ingestion directly from GX MQTT without broker bridging.
- Lets you choose only needed topics/fields.

### Acceptance
- With config disabled, nothing changes.
- With config enabled on a test node:
  - Telegraf reads GX MQTT topics
  - writes metrics into local VM (`:8428/write` via Influx line protocol)
  - does not explode cardinality

---

## Milestone 4d — GX discovery automation (mDNS → generated Telegraf config)
**Branch:** `feat/4d-gx-mdns-discovery-autoconfig`

### What
- Add a small “gx-discovery” helper service/script on the node that:
  1) uses Avahi to browse for MQTT brokers (or your custom GX service type)
  2) generates `/etc/telegraf/telegraf.d/90-gx-mqtt-auto.conf` from discovered brokers
  3) triggers Telegraf reload/restart
- Configure Avahi to listen only on intended interfaces (service subnet + optionally Wi‑Fi), not WAN.
- Include a “do not reflect mDNS across subnets” default (no broadcast storms).

### Why
- Lets your node act as the **central aggregation point** without bridging subnets.
- Dynamic onboarding of “additional GX devices on Wi‑Fi” becomes realistic.

### Acceptance
- Plug in GX or join same Wi‑Fi; node discovers it via mDNS on that interface.
- Telegraf config updates and starts ingesting from that broker.
- No subnet mDNS reflection unless explicitly enabled.

---

## Milestone 4e — GX MQTT keepalive helper (if needed for dbus-flashmq)
**Branch:** `feat/4e-gx-mqtt-keepalive-helper`

### What
- Add a tiny helper that sends the required keepalive/read request periodically to GX MQTT topics (with suppress-republish) so GX continues publishing.
- Configurable list of GX portal IDs or discovered IDs.

### Why
- Some GX MQTT modes won’t publish anything until keepalive is requested.
- This avoids “it stopped publishing after a while” field issues.

### Acceptance
- On real GX devices, metrics continue publishing steadily without republish storms.

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
- `/etc/ovr/edge.env` and `/etc/ovr/cloud.env` are canonical
- `/etc/ovr/peers.json` for peer routing (phase 2)
- `/etc/ovr/wifi_networks.csv` for known Wi‑Fi (phase 1)
- `/etc/ovr/gx_mqtt_sources.json` (optional; if you prefer static GX broker list instead of mDNS discovery)
- `/etc/telegraf/telegraf.d/90-gx-mqtt-auto.conf` generated file (phase 4d)

---
