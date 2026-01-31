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
