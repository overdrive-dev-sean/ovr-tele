# Claude Context

Essential context for Claude instances working on this repository.

## What This Is

**ovr-tele** is the telemetry and control platform for **Overdrive Energy Solutions**, a company providing temporary/portable power solutions that reduce diesel consumption through:

- **Direct PV offset** - Solar displacing generator runtime
- **Hybridization** - Battery + generator working together efficiently
- **Peak shaving** - Using battery to reduce grid demand charges

### Why Accurate Data Matters

Proper system sizing prevents failures. Undersized battery = dead loads. Oversized = wasted cost. Accurate load profiling from real events informs future deployments and proves value to clients (diesel saved, kWh delivered, carbon offset).

### Use Cases

- **Live events** (primary) - Concerts, festivals, corporate events
- **Disaster relief** - Emergency power deployments
- **Construction** - Temporary site power
- **Time-shifting** - e.g., overnight battery charging for autonomous robots

### Scale

Large events can have **100+ assets** being measured simultaneously - multiple BESS units, generator feeds, stage transformers, PV inverters, etc.

## Architecture

**Edge nodes** (N100 mini PCs) are distributed across a site. Each node monitors multiple systems in its proximity via Ethernet or WiFi. Nodes are added as needed for coverage.

**Cloud backend** (VPS) aggregates data from all edge nodes across all deployments. Provides fleet-wide visibility, event management, and reporting.

```
Data flow: Assets → Edge Nodes → VictoriaMetrics (local) → remote_write → Cloud VM
```

## Data Sources

| Source | Protocol | What it measures |
|--------|----------|------------------|
| **Victron GX** | MQTT | Overdrive's own BESS units (battery, inverter, solar) |
| **ACUVIM** | Modbus/HTTP | Large loads - transformers, stage services (up to 400A 3-phase) |
| **Fronius** | Modbus | Grid-tie PV inverters |
| **Other BESS** | Modbus | Third-party battery systems when needed |

The goal is a **universal data aggregation system** - not locked to any single vendor.

## Reporting

Reports are generated **automatically and immediately**:
- Per-system report when that system's participation in an event ends
- Aggregated event report when the entire event concludes
- All data retained for deeper analysis and potential ML

### Audiences

| Product | Audience |
|---------|----------|
| **Reports** | Operations, clients (billing, sustainability) |
| **Edge dashboard** | Field techs, operations (real-time monitoring) |
| **Grafana** | Everyone (different dashboards for different roles) |

## Data Model

### Entity Hierarchy

```
Deployment (fleet owner)
    └── Event (a job/engagement, 1 day to 1+ month)
            └── Node (edge N100 at a location)
                    └── System (GX, ACUVIM, etc. - the actual data source)
```

### Key Concepts

| Entity | Description | Example |
|--------|-------------|---------|
| **Deployment** | Fleet owner/manager | "Overdrive", "RoboticsCompanyRental" |
| **Event** | A specific job with start/end | "Coachella2026", "DisasterReliefTX" |
| **Node** | Edge N100 device | "node-04", "n100-stage-left" |
| **System** | Individual monitored asset | "bess-01", "acuvim-transformer-a" |

### Relationships

- **Nodes and systems float freely** - not permanently tied to anything
- A system connects to whichever node is physically nearby
- A node participates in whichever event it's commissioned for
- Same equipment moves from event to event throughout the year
- Eventually every system may have a built-in node, but not yet

### Fleet Map Filtering

```
Deployment (filter first)
    → Events (can have multiple concurrent events, or see whole fleet)
        → Nodes
            → Systems
```

## Event & Commissioning Workflow

### Event Lifecycle

1. **Event Created** - By ops in advance (for consistency) or by first field tech on-site
2. **Nodes Commission** - Each node joins the event, adds systems
3. **Event Active** - Logging, monitoring, real-time dashboards
4. **Systems End** - Individual systems can end participation (generates per-system report)
5. **Event Ends** - Aggregate report generated, data preserved on cloud

Events can last **1 day to 1+ month**.

### Node Commissioning Flow (on edge dashboard)

```
1. Choose/create event name
   - If event exists → select from dropdown (auto-populated from cloud)
   - If no events → create new name (first node defines it)
   - If offline → create temp name (can be merged later)

2. Add at least one system_id
   - Select from dropdown (ideally auto-discovered)
   - GX, ACUVIM, or other data source

3. Set location tag for the system
   - e.g., "Stage Left", "Generator A", "Main Transformer"

4. Add more systems as needed (now or later)

5. Add notes and photos
   - Node-level notes (about the deployment location)
   - System-level notes (about specific equipment)
```

**Key design principle:** Works entirely offline. Edge dashboard lives on the edge node - no internet required to commission and start logging.

### Cloud's Role

- **Fleet map** - Overview of all events, all nodes, all systems
- **Real-time aggregation** - As much live data as connectivity allows
- **Long-term storage** - Downsampled data so edges can start fresh after 6+ months
- **Event coordination** - Manage overall event start/stop, sync with first node
- **Preserve artifacts** - Collect notes, photos, reports from nodes before they go offline

### Offline/Merge Scenarios

If a node commissions offline with a temp event name:
- Data logs locally under temp name
- When connectivity restored, temp event can be merged into the real event
- Reports and data get re-associated

## Key Technologies

- **VictoriaMetrics** - Time series database (Prometheus-compatible, handles high cardinality)
- **Telegraf** - Metrics collection (MQTT consumer, Modbus, with Starlark processor)
- **Mosquitto** - MQTT broker (internal to edge stack)
- **Flask** - Python APIs (edge events service, cloud fleet API)
- **React + Vite** - Frontends (edge dashboard, cloud fleet map)
- **Docker Compose** - Container orchestration
- **Caddy** - TLS termination and auth (cloud)
- **Grafana** - Dashboards for all audiences

## Victron/Venus OS MQTT

GX devices run Venus OS with an MQTT broker on port 1883. Topic format:
```
N/<portal_id>/<service>/<instance>/<path>
```

Examples:
- `N/abc123/vebus/276/Soc` - Battery state of charge
- `N/abc123/vebus/276/Dc/0/Voltage` - DC voltage
- `N/abc123/vebus/276/Mode` - Inverter mode (1=charger, 2=inverter, 3=on, 4=off)

Keepalive required every 60s: `R/<portal_id>/keepalive` with empty payload.

## Directory Structure

```
/opt/ovr/
├── edge/                 # Edge stack
│   ├── services/events/  # Flask API (app.py)
│   ├── services/frontend/# React dashboard
│   └── telegraf/         # Telegraf config + Starlark processor
├── cloud/                # Cloud stack
│   ├── services/api/     # Flask fleet API
│   └── services/map/     # React fleet map
└── docs/

/etc/ovr/                 # Runtime config (persists across deploys)
├── edge.env              # Edge environment
├── telegraf.d/           # Generated Telegraf configs
└── gx_systems.json       # GX device registry
```

## Common Gotchas

1. **Docker networking** - Edge events service uses `network_mode: host` for GX MQTT access. Frontend nginx proxies to `172.20.1.1:8088` (Docker gateway).

2. **Metric naming** - Telegraf produces `victron_*` metrics with `db="telegraf"` label. Old vmagent scraping used `job="gx_fast"` etc. Don't mix them up.

3. **MQTT keepalive** - GX stops publishing if no keepalive for 60s. The `ovr-refresh-gx-mqtt-sources` systemd timer handles this.

4. **Env file locations** - Canonical is `/etc/ovr/*.env`, symlinked to compose directories.

## Coordination

See `HANDOFF.md` for:
- Current work status
- Integration points between edge and cloud
- Coordination protocol for multiple Claude instances

## Roadmap

See `ovr-tele_feature_roadmap_v3.md` for milestone status and planned work.
