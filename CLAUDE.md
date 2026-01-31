# Claude Context

Essential context for Claude instances working on this repository.

## What This Is

**ovr-tele** is a distributed telemetry and control system for mobile solar+battery installations (RVs, boats, off-grid).

- **Edge nodes** (N100 mini PCs) run on each installation, collecting data from Victron GX devices via MQTT
- **Cloud backend** (VPS) aggregates fleet data, provides fleet map UI, handles event management
- Data flows: GX → MQTT → Telegraf → VictoriaMetrics (edge) → remote_write → VictoriaMetrics (cloud)

## Key Technologies

- **VictoriaMetrics** - Time series database (Prometheus-compatible)
- **Telegraf** - Metrics collection (MQTT consumer with Starlark processor)
- **Mosquitto** - MQTT broker (internal to edge stack)
- **Flask** - Python APIs (edge events service, cloud fleet API)
- **React + Vite** - Frontends (edge dashboard, cloud fleet map)
- **Docker Compose** - Container orchestration
- **Caddy** - TLS termination and auth (cloud)

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
