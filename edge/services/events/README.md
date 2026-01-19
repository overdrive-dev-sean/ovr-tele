# OVR Event Service

Lightweight HTTP service for marking events and location changes in OVR telemetry data.

**Primary interface**: Touch-friendly web UI accessible via Cloudflare Tunnel + Cloudflare Access.

## Architecture

```
┌─────────────────────────────────────────┐
│  Browser (phone/tablet/desktop)         │
│  https://events.yourdomain.com          │
└────────────────┬────────────────────────┘
                 │ HTTPS
                 ▼
┌─────────────────────────────────────────┐
│  Cloudflare Access (authentication)     │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│  Cloudflared Tunnel (systemd)           │
│  localhost:8088 ← ingress               │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│  Event Service (Docker)                 │
│  ├─ Flask API (REST endpoints)          │
│  ├─ Web UI (touch-friendly)             │
│  ├─ VM Writer (Influx protocol)         │
│  ├─ VM Reader (PromQL queries)          │
│  ├─ SQLite (state + audit log + notes)  │
│  └─ SSH Control (GX device writes only) │
└────────────────┬────────────────────────┘
                 │ Influx line protocol
                 ▼
┌─────────────────────────────────────────┐
│  VictoriaMetrics                        │
│  ├─ ovr_event_active                  │
│  ├─ location tag (ovr_event)                      │
│  └─ victron_* metrics (read by webapp)  │
└────────────────┬────────────────────────┘
                 │ PromQL
                 ▼
┌─────────────────────────────────────────┐
│  Grafana (visualization)                │
│  Event overlays + metric correlation    │
└─────────────────────────────────────────┘
```

## Quick Start

### Run with Docker Compose (recommended)

From the edge directory:

```bash
cd /opt/edge  # or wherever you cloned the repo
sudo docker compose -f compose.dev.yml up -d --build events
```

Access UI: `http://localhost:8088`

### Run Standalone

```bash
cd edge/services/events

# Install dependencies
pip3 install -r requirements.txt

# Set environment (optional)
export VM_WRITE_URL="http://localhost:8428/write"
export DB_PATH="./events.db"
export LOG_LEVEL="INFO"

# Run
python3 app.py
```

## API Endpoints

### POST /api/event/start
Start a new event.

**Request**:
```json
{
  "system_id": "rig_01",
  "event_id": "startup",
  "location": "warehouse",  // optional
  "note": "Initial test",    // optional
  "ts": 1641024000000000000  // optional, nanoseconds
}
```

**Response**:
```json
{
  "success": true,
  "system_id": "rig_01",
  "event_id": "startup",
  "location": "warehouse",
  "ts": 1641024000000000000
}
```

### POST /api/event/end
End an event.

**Request**:
```json
{
  "system_id": "rig_01",
  "event_id": "startup",  // optional, ends current if omitted
  "ts": 1641024005000000000  // optional
}
```

### POST /api/location/set
Update system location.

**Request**:
```json
{
  "system_id": "rig_01",
  "location": "field_site_a",
  "ts": 1641024010000000000  // optional
}
```

### POST /api/note
Add a note/annotation.

**Request**:
```json
{
  "system_id": "rig_01",
  "event_id": "startup",  // optional
  "msg": "Voltage dropped to 47V",
  "ts": 1641024012000000000  // optional
}
```

### GET /api/status
Get current status.

**Query params**:
- `system_id` (optional): Filter by specific system

**Response**:
```json
{
  "system_id": "rig_01",
  "active_event": {
    "event_id": "startup",
    "location": "warehouse",
    "started_at": 1641024000000000000
  },
  "current_location": {
    "location": "warehouse",
    "updated_at": 1641024000000000000
  },
  "recent_logs": [...]
}
```

### Map tile usage endpoints

#### POST /api/map-tiles/increment
Increment local tile attempt counters for the current month.

```json
{
  "provider": "mapbox",
  "count": 25,
  "month_key": "2026-01",
  "node_id": "node-01",
  "deployment_id": "deploy-01"
}
```

#### GET /api/map-tiles/status
Fleet-aware usage status (local + cloud).

```json
{
  "month_key": "2026-01",
  "thresholds": { "mapbox": 750000, "esri": 2000000, "guardrailPct": 0.95 },
  "local": { "mapbox": 123, "esri": 98 },
  "fleet": { "mapbox": 12450, "esri": 9100 },
  "pct": { "mapbox": 1.7, "esri": 0.5 },
  "blocked": { "mapbox": false, "esri": false },
  "preferredProvider": "esri",
  "recommendedProvider": "esri",
  "warning": null
}
```

## Configuration

Environment variables (see `.env.example`):

- `VM_WRITE_URL`: VictoriaMetrics write endpoint (default: `http://victoria-metrics:8428/write`)
- `DB_PATH`: SQLite database path (default: `/data/events.db`)
- `LOG_LEVEL`: Logging level (default: `INFO`)
- `API_KEY`: Optional authentication key
- `PORT`: HTTP port (default: `8088`)

## Testing

Run unit tests:

```bash
python3 test_influx_escaping.py
```

Test API with curl:

```bash
# Health check
curl http://localhost:8088/health

# Start event
curl -X POST http://localhost:8088/api/event/start \
  -H "Content-Type: application/json" \
  -d '{"system_id":"test","event_id":"demo"}'

# Check status
curl "http://localhost:8088/api/status?system_id=test"

# End event
curl -X POST http://localhost:8088/api/event/end \
  -H "Content-Type: application/json" \
  -d '{"system_id":"test"}'
```

## Data Storage

### VictoriaMetrics (Time Series)

**Event Activity + Location (unified)**
```
ovr_event,event_id=startup,system_id=rig_01,location=warehouse active=1i <ts_ns>  # Start/location
ovr_event,event_id=startup,system_id=rig_01,location=warehouse active=0i <ts_ns>  # End
```

Location changes are represented by new `ovr_event` samples with updated `location` tags.

### SQLite (Persistent Storage)

**Notes Table** (`audit_log`)
- Notes stored with full text (VictoriaMetrics converts strings to 0)
- Retrieved via `/api/notes` endpoint
- Supports service-specific tagging ([Logger X] prefix)
- Bulk delete functionality with checkboxes (2+ notes)
- Deletion uses `note_id` (audit_log id) with `note_text` fallback for legacy clients

## Grafana Queries

### Event regions
```promql
ovr_event_active{system_id="rig_01"}
```

### Location timeline
```promql
ovr_event_active{system_id="rig_01"}
```

**Note**: Use the `location` label to show location changes over time.

**Note**: Event notes are stored in SQLite (not VictoriaMetrics) and displayed in the web UI only.

## Web UI Features

### Real-time Metrics Dashboard
- **SOC** (State of Charge) - Updates every 10s
- **Alerts** - Updates every 1s
- **P<sub>in</sub>** (AC Input Power) - Updates every 1s
- **P<sub>out</sub>** (AC Output Power) - Updates every 1s
- Responsive grid layout: vertical stack on mobile, 2×2 on desktop

### Multi-Logger Event Management
- Add/remove loggers (services) dynamically
- GPS location capture per logger
- Service-specific notes with [Logger X] tagging
- Active logger display with visual status indicators

### Notes System
- Full-text storage in SQLite (not VictoriaMetrics)
- Service tagging for context (General note or [Logger X])
- Bulk delete with checkboxes (appears when 2+ notes exist)
- "Select All" functionality
- Auto-refresh on event changes

### GX Device Control
- Set battery charge current, inverter mode, AC input limit
- SSH write commands only (reads from VictoriaMetrics)
- Real-time setting display from VM metrics

## Architecture

- **Flask** web framework (lightweight, minimal dependencies)
- **SQLite** for state persistence, audit log, and notes
- **Requests** for HTTP calls to VictoriaMetrics (read/write)
- **Paramiko** for SSH control of GX device (write-only)
- **Influx line protocol** for time series writes
- **PromQL queries** for reading GX settings from VictoriaMetrics
- Retry logic with exponential backoff (3 attempts)
- Touch-friendly web UI (responsive, mobile-ready)

## Security

- Optional API key authentication (`X-API-Key` header)
- CORS not enabled by default (add if needed for cross-origin access)
- Runs as non-root user in Docker (UID 1000)
- Health checks built-in

## Files

- `app.py`: Main Flask application
- `Dockerfile`: Container build
- `requirements.txt`: Python dependencies
- `test_influx_escaping.py`: Unit tests
- `.env.example`: Example configuration

## Integration

### Cloudflare Tunnel
See `../docs/CLOUDFLARED_EVENTS_CONFIG.md` for secure remote access.

### Grafana
Query event metrics alongside GX and Acuvim data using `system_id` label.

## License

Proprietary - OVR Energy Systems
