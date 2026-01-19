> **LEGACY DOCUMENT**: This file preserves the original repo layout and may contain outdated paths.
> Use `README.md`, `RESTRUCTURE_NOTES.md`, and `edge/README.md` for the current layout.

# OVR Telemetry Stack

Industrial telemetry stack for off-grid renewable energy systems. Collects metrics from Victron GX devices, Acuvim power meters, and system metrics into VictoriaMetrics, visualized via Grafana.

## Quick Start

### Prerequisites
- N100 mini-PC running Debian 13
- External eSATA storage mounted at `/mnt/esata`
- Docker and docker-compose installed
- Network access to Victron GX device and Acuvim meters

### Deploy Stack
```bash
cd /opt/stack  # or wherever you cloned this repo
sudo docker-compose pull
sudo docker-compose up -d
```


### Deployment modes (dev vs release)

- **Development / local builds**: use `docker-compose.yml` (this is what the quick start above uses).
- **Field deployments / reproducible releases**: use `docker-compose.release.yml` + an env file.

Example:
```bash
cd /opt/stack
cp edge.env.example edge.env
# edit EDGE_VERSION + (optionally) pin dependency image tags
sudo docker compose --env-file edge.env -f docker-compose.release.yml pull
sudo docker compose --env-file edge.env -f docker-compose.release.yml up -d
```

See `RELEASE.md` and `VERSIONING.md` for the lifecycle protocol.

### Initial Configuration

#### 1. Network Setup (Required for GX connectivity)
Edit site-specific interface names:
```bash
vim ovru-netkit/vars.env
```

Then run the installer:
```bash
cd ovru-netkit
sudo bash install.sh
```

Verify networking:
```bash
sudo bash verify.sh
```

#### 2. Cloudflare Tunnel (Optional - for remote access)
Create tunnel token at https://one.dash.cloudflare.com/, then:
```bash
sudo mkdir -p /etc/cloudflared
sudo tee /etc/cloudflared/cloudflared.env >/dev/null <<EOF
TUNNEL_TOKEN=your_tunnel_token_here
EOF
sudo chmod 600 /etc/cloudflared/cloudflared.env
```

Install cloudflared service:
```bash

## Event Reports

Automatic comprehensive reports are generated when events end, containing:
- Device auto-detection (Victron/Acuvim, phase config, voltage)
- Energy calculations with multiple methods (4 for Victron, 3 for Acuvim)
- Power statistics (peak/avg per phase)
- Phase imbalance analysis
- Load distribution histogram
- Auto-trimmed idle periods (2% peak or 50W threshold)
- Notes and images from event timeline
- Professional HTML rendering with visual charts

### Accessing Reports

When you end an event via the webapp, a **View Report** button appears linking to the full report. Reports are also accessible at:
```
https://your-domain.com/api/reports/<event_id>/html
```

Reports are stored in `/data/reports/event_{id}_{timestamp}/` with both JSON and HTML formats.

### Configuration

Set the report URL base in [docker-compose.yml](docker-compose.yml):
```yaml
event-service:
  environment:
    - REPORT_BASE_URL=https://your-domain.com
```

Default: `https://pro6005-2.seanajacobs.com`

### Manual Report Generation

Generate a report for any past event:
```bash
curl -X POST http://localhost:8088/api/reports/generate \
  -H "x-api-key: your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"event_id": "your_event_id"}'
```

### Technical Details

- **Data Source**: VictoriaMetrics time-series queries with trapezoidal integration
- **System ID Normalization**: Uses canonical IDs and Logger X -> acuvim_1X mappings
- **Device Detection**: Auto-detects from metric patterns (victron_ac_out_power, acuvim_P)
- **Energy Methods**: Victron (total power, apparent power, sum of phases, I×V per phase); Acuvim (real power, √(P²+Q²), I×V per phase)
- **Auto-Trimming**: Removes periods where power < max(2% peak, 50W) for 60+ seconds
- **Background Generation**: Reports generate in separate thread (2-3 seconds typical)

See [event-service/app.py](event-service/app.py) lines 1216-1400 for implementation.

---

```bash
sudo tee /etc/systemd/system/cloudflared.service >/dev/null <<'EOF'
[Unit]
Description=cloudflared
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
EnvironmentFile=/etc/cloudflared/cloudflared.env
ExecStart=/usr/local/bin/cloudflared --no-autoupdate tunnel --protocol http2 run --token ${TUNNEL_TOKEN}
Restart=always
RestartSec=5
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared
```

#### 3. Tailscale (Optional - for VPN access)
Install and authenticate:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --accept-dns=false
# Follow authentication URL
```

#### 4. Log Growth Limits (Recommended)
Limit systemd journal and Docker log disk usage to prevent storage exhaustion:
```bash
sudo bash scripts/hardening/limit-logs.sh
```

This configures:
- **systemd journals**: Capped at 200M, keeps 1G free, 7-day retention
- **Docker logs**: Rotated at 10MB × 3 files per container

**Verify configuration:**
```bash
# Check journal disk usage
journalctl --disk-usage

# Check Docker logging driver
docker info --format '{{.LoggingDriver}}'

# View log file sizes for specific container
docker inspect <container_name> | grep LogPath
ls -lh /var/lib/docker/containers/*/*-json.log
```

**Note**: Existing containers continue using old log settings. Recreate to apply new limits:
```bash
cd /opt/stack
sudo docker-compose up -d --force-recreate
```

## Usage

### Adding Acuvim Meters
1. Add IP address to `telegraf/targets_acuvim.txt` (one per line)
2. Run discovery script:
```bash
sudo bash scripts/telegraf_discover_acuvim.sh
```
3. Telegraf auto-reloads within seconds (no restart needed)

### Modifying Victron Metrics
1. Edit `map_fast.tsv` or `map_slow.tsv` on the Victron GX device
2. Restart dbus2prom.py on the GX device
3. Metrics appear in vmagent scrapes automatically

### GX Exporter Scripts (on GX device)
The GX runs two dbus2prom exporters: fast (9480) and slow (9481).

**Location (on GX):** `/data/dbus2prom/`
- `run_exporters.sh`: starts both exporters (fast + slow)
- `run_fast.sh` / `run_slow.sh`: optional single-exporter loops
- `watchdog.sh`: cron-driven health check that restarts exporters if either port is down

**Restart both exporters:**
```bash
/data/dbus2prom/run_exporters.sh
```

**Quick health check (on GX):**
```bash
busybox netstat -ltn | grep -E ':(9480|9481)\\b'
curl -fsS -m 2 http://127.0.0.1:9480/metrics | head -n 5
curl -fsS -m 2 http://127.0.0.1:9481/metrics | head -n 5
```

### Accessing Grafana
- Local: http://localhost:3000
- Default credentials: admin/admin (change on first login)
- VictoriaMetrics datasource is pre-provisioned

## Debugging Commands

### Stack Health Check
```bash
sudo bash home/ovradmin/stack_health.sh
```

### Container Status
```bash
# All containers
sudo docker ps -a

# Specific container logs
sudo docker logs victoria-metrics --tail 100
sudo docker logs vmagent --tail 100
sudo docker logs telegraf --tail 100
sudo docker logs grafana --tail 100

# Follow logs in real-time
sudo docker logs -f telegraf
```

### VictoriaMetrics Query
```bash
# Check ingestion rates
curl -s http://127.0.0.1:8428/api/v1/query -d 'query=rate(vm_rows_inserted_total[1m])' | jq .

# List all metric names
curl -s http://127.0.0.1:8428/api/v1/label/__name__/values | jq .

# Check storage size
sudo docker exec victoria-metrics df -h /storage
sudo docker exec victoria-metrics du -sh /storage/data
```

### Victron GX Scrape Targets
```bash
# Test fast endpoint (500ms scrape)
curl -s http://192.168.100.2:9480/metrics | head -20

# Test slow endpoint (5s scrape)
curl -s http://192.168.100.2:9481/metrics | head -20

# Check if GX is reachable
ping -c 3 192.168.100.2
```

### Acuvim Meter Testing
```bash
# Check if meter is reachable (Modbus TCP port 502)
timeout 2 bash -c "cat < /dev/null > /dev/tcp/10.10.4.10/502" && echo "OK" || echo "FAIL"

# List discovered meters
ls -l telegraf/telegraf.d/acuvim_*.conf

# Test Modbus read with modbus-cli (if installed)
modbus read -a 1 -t float32 tcp://10.10.4.10:502 13312 2
```

### Networking Diagnostics
```bash
# NetworkManager status
nmcli device status
nmcli connection show

# Check default route and WAN selection
ip route get 1.1.1.1

# Check NAT for GX device
sudo nft list ruleset | grep masquerade

# DHCP leases for GX
sudo cat /var/lib/misc/dnsmasq.leases

# Check IP forwarding
sysctl net.ipv4.ip_forward
```

### Cloudflare Tunnel Status
```bash
# Check service status
systemctl status cloudflared

# View recent logs
sudo journalctl -u cloudflared -n 50 --no-pager

# Check tunnel registration
sudo journalctl -u cloudflared --no-pager | grep -E "Registered tunnel|protocol"

# Test connectivity
pgrep -af cloudflared
```

### Tailscale Status
```bash
# Check connection
sudo tailscale status

# View IP address
sudo tailscale ip

# Check routing
sudo tailscale netcheck
```

## Troubleshooting

### GX Device Not Scraping
```bash
# 1. Verify GX has IP 192.168.100.2
ping -c 3 192.168.100.2

# 2. Check DHCP lease
sudo cat /var/lib/misc/dnsmasq.leases

# 3. Verify dbus2prom running on GX (both ports)
busybox netstat -ltn | grep -E ':(9480|9481)\\b'
curl http://192.168.100.2:9480/metrics | head
curl http://192.168.100.2:9481/metrics | head

# 3b. Restart exporters on GX if needed
ssh root@192.168.100.2 "/data/dbus2prom/run_exporters.sh"

# 4. Check vmagent scrape errors
curl -s http://127.0.0.1:8429/targets | grep -A5 gx_
```

### Acuvim Meters Not Appearing
```bash
# 1. Verify network connectivity
timeout 2 bash -c "cat < /dev/null > /dev/tcp/10.10.4.10/502" && echo "OK" || echo "FAIL"

# 2. Re-run discovery
sudo bash scripts/telegraf_discover_acuvim.sh

# 3. Check telegraf logs
sudo docker logs telegraf --tail 100 | grep -i modbus

# 4. Verify influx data in VM
curl -s "http://127.0.0.1:8428/api/v1/query?query=acuvim" | jq .
```

### Storage Issues
```bash
# Check mount
mountpoint /mnt/esata

# Check disk space
df -h /mnt/esata

# Check VictoriaMetrics storage
sudo docker exec victoria-metrics du -sh /storage/data

# View retention policy (default 30 days)
sudo docker exec victoria-metrics cat /proc/1/cmdline | tr '\0' ' '
```

### High Memory Usage
```bash
# Container memory usage
sudo docker stats --no-stream

# VictoriaMetrics memory limit (adjust in docker-compose.yml)
sudo docker inspect victoria-metrics | jq '.[0].HostConfig.Memory'

# Restart specific container
sudo docker-compose restart victoria-metrics
```

## File Structure

```
.
├── docker-compose.yml              # Main stack definition
├── dbus2prom.py                    # Victron D-Bus exporter (runs on GX)
├── map_fast.tsv                    # Fast metric mappings (500ms)
├── map_slow.tsv                    # Slow metric mappings (5s)
├── event-service/                  # Event + Location Marker service
│   ├── app.py                      # Flask API + VM writer + Web UI
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── test_influx_escaping.py     # Unit tests
│   ├── examples.sh                 # API usage examples
│   └── README.md                   # Service documentation
├── docs/
│   ├── CLOUDFLARED_EVENTS_CONFIG.md # Cloudflare tunnel setup (required)
│   └── GRAFANA_EVENTS.md           # Dashboard examples
├── grafana/
│   └── provisioning/
│       └── datasources/
│           └── vm.yml              # VictoriaMetrics datasource
├── vmagent/
│   └── scrape.yml                  # Prometheus scrape config
├── telegraf/
│   ├── telegraf.conf               # Main Telegraf config
│   ├── targets_acuvim.txt          # List of Acuvim IPs
│   ├── telegraf.d/                 # Auto-generated meter configs
│   └── templates/
│       └── acuvim_modbus.tpl       # Modbus config template
├── scripts/
│   ├── telegraf_discover_acuvim.sh # Meter discovery script
│   └── hardening/
│       └── limit-logs.sh           # Log rotation hardening
├── ovru-netkit/                    # Network setup scripts
│   ├── install.sh
│   ├── verify.sh
│   └── vars.env                    # Site-specific interface names
└── home/ovradmin/
    └── stack_health.sh             # Health check script
```

## Event + Location Marker System

The OVR telemetry stack includes an Event + Location Marker system that stores event windows and location changes directly in VictoriaMetrics without adding labels to every metric (avoiding cardinality explosion).

### Overview

Event markers create dedicated time series in VictoriaMetrics that can be:
- Overlaid as regions/annotations in Grafana
- Used to filter and correlate with GX and Acuvim metrics
- Queried to find relevant time ranges for analysis

**Use cases**:
- Mark startup/shutdown events
- Track load tests or commissioning activities
- Record location changes as rigs move
- Add operational notes and observations
- Correlate events with voltage drops, power spikes, etc.

**Primary Interface**: Touch-friendly web UI accessible via Cloudflare Tunnel + Cloudflare Access (secure remote access from any device)

### Data Model

Single unified metric written to VictoriaMetrics (Influx line protocol):

1. **Event Activity + Location** (region markers with location tag):
   ```
   ovr_event,event_id=startup,system_id=rig_01,location=warehouse active=1i <timestamp_ns>  # Start/location
   ovr_event,event_id=startup,system_id=rig_01,location=warehouse active=0i <timestamp_ns>  # End
   ```

Location changes are represented by new `ovr_event` samples with updated `location` tags.

Notes are stored in SQLite (`audit_log`) and surfaced in the web UI via `/api/notes` (not queryable in Grafana).

### Quick Start

#### 1. Deploy Event Service

Already included in `docker-compose.yml`:

```bash
cd /opt/stack
sudo docker-compose build event-service
sudo docker-compose up -d event-service
```

Verify it's running:
```bash
docker ps | grep event-service
curl http://localhost:8088/health
```

#### 2. Configure Cloudflare Tunnel for Remote Access

**Required for remote access**. The web UI is exposed via Cloudflare Tunnel with Cloudflare Access authentication:

```
https://events.yourdomain.com
```

Follow the complete setup guide: [docs/CLOUDFLARED_EVENTS_CONFIG.md](docs/CLOUDFLARED_EVENTS_CONFIG.md)

**Key steps**:
1. Add DNS CNAME: `events` → `<tunnel-id>.cfargotunnel.com`
2. Create Cloudflare Access policy (email/user authentication)
3. Add ingress rule to `/etc/cloudflared/config.yml`
4. Restart cloudflared: `systemctl restart cloudflared`

#### 3. Access Web UI

Open browser and navigate to:
```
https://events.yourdomain.com
```

- Authenticate via Cloudflare Access (first visit)
- Touch-friendly interface works on phones, tablets, and desktops
- No VPN or port forwarding required

**React migration UI (separate container)**:
- New frontend runs on port `8080` via the `frontend` service in `docker-compose.yml`
- Use a separate Cloudflare hostname (recommended) or point the existing one to `http://localhost:8080`
- API requests are proxied to the existing `event-service` backend

**Local testing** (N100 host only):
```
http://localhost:8088
```

### API Usage

#### Start an Event
```bash
curl -X POST http://localhost:8088/api/event/start \
  -H "Content-Type: application/json" \
  -d '{
    "system_id": "rig_01",
    "event_id": "startup",
    "location": "warehouse",
    "note": "Initial power-on test"
  }'
```

#### End an Event
```bash
# End specific event
curl -X POST http://localhost:8088/api/event/end \
  -H "Content-Type: application/json" \
  -d '{"system_id": "rig_01", "event_id": "startup"}'

# End currently active event (event_id optional)
curl -X POST http://localhost:8088/api/event/end \
  -H "Content-Type: application/json" \
  -d '{"system_id": "rig_01"}'
```

#### Set Location
```bash
curl -X POST http://localhost:8088/api/location/set \
  -H "Content-Type: application/json" \
  -d '{"system_id": "rig_01", "location": "field_site_a"}'
```

#### Add Note
```bash
curl -X POST http://localhost:8088/api/note \
  -H "Content-Type: application/json" \
  -d '{
    "system_id": "rig_01",
    "event_id": "startup",
    "msg": "Voltage dropped to 47V for 3 seconds"
  }'
```

#### Check Status
```bash
# Status for specific system
curl http://localhost:8088/api/status?system_id=rig_01 | jq .

# All systems
curl http://localhost:8088/api/status | jq .
```

### Configuration

Set environment variables in `docker-compose.yml`:

```yaml
environment:
  - VM_WRITE_URL=http://victoria-metrics:8428/write  # VictoriaMetrics endpoint
  - DB_PATH=/data/events.db                           # SQLite state database
  - LOG_LEVEL=INFO                                    # DEBUG, INFO, WARNING
  - API_KEY=your_secret_key_here                      # Optional API authentication
```

**API Key authentication** (optional):
```bash
curl -X POST http://localhost:8088/api/event/start \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_secret_key_here" \
  -d '{"system_id": "rig_01", "event_id": "test"}'
```

### Map Tile Guardrails (Leaflet)

Leaflet tile attempts are counted per UTC calendar month (using `tileloadstart` as an upper bound).
Guardrails block provider selection when fleet usage reaches `GUARDRAIL_LIMIT_PCT` of the monthly free tier.
If both providers are at/above the limit, the current provider stays active and a warning banner is shown.

Environment variables (node + cloud):
- `MAPBOX_TOKEN` (public token for raster tiles)
- `MAPBOX_TOKEN_FILE` (optional path to file containing Mapbox token)
- `ESRI_TOKEN` (optional token for ArcGIS tiles)
- `MAPBOX_FREE_TILES_PER_MONTH` (default 750000)
- `ESRI_FREE_TILES_PER_MONTH` (default 2000000)
- `GUARDRAIL_LIMIT_PCT` (default 0.95)
- `CLOUD_API_URL` (node only, e.g. `https://map.example.com`)

Monitoring:
- Node metrics (scraped at `/metrics`):
  - `map_tiles_month_total{provider="mapbox|esri",node_id="...",deployment_id="..."}`
  - `map_tiles_month_pct{provider="mapbox|esri",node_id="...",deployment_id="..."}`
- Fleet totals in Grafana (cloud VictoriaMetrics):
  - `sum(map_tiles_month_total{provider="mapbox"})`
  - `sum(map_tiles_month_total{provider="esri"})`

Ensure vmagent scrapes the event-service `/metrics` endpoint and remote_write relabeling does not drop
`map_tiles_month_*` series (update `/etc/overdrive/targets.yml` and your relabel configs if needed).

### Grafana Visualization

#### Query Event Regions

To show event windows as shaded regions:

**Query**:
```promql
ovr_event_active{system_id="rig_01"}
```

**Panel type**: State timeline or Time series with threshold
- Value 1 = Event active (green)
- Value 0 = Event ended (transparent)

#### Query Location Timeline

**Query**:
```promql
ovr_event_active{system_id="rig_01"}
```

**Panel type**: State timeline
- Use the `location` label to see location changes over time

#### Event Notes

Notes are stored in SQLite and shown in the web UI only (not in VictoriaMetrics, so no PromQL query).

#### Filtering Other Metrics by Event

Correlate events with power metrics:

```promql
# Show battery voltage only during "load_test" events
victron_dc_voltage_v{} 
  and on(system_id) 
  ovr_event_active{event_id="load_test"} == 1
```

#### Annotation Queries

Add event annotations to any panel:

1. Panel settings → **Annotations** → **Add annotation query**
2. Set query:
   ```promql
   changes(ovr_event_active{system_id="rig_01"}[1s]) != 0
   ```
3. Map fields:
   - Time: `Time`
   - Text: `event_id` label
   - Tags: `system_id`

### Data Persistence

- **VictoriaMetrics**: Event time series (retention: 30 days by default)
- **SQLite**: Current state (active events, locations), notes, and audit log at `/data/events.db`

Backup SQLite:
```bash
docker exec event-service sqlite3 /data/events.db .dump > events_backup.sql
```

### Testing

Run Influx line protocol escaping tests:
```bash
cd event-service
python3 test_influx_escaping.py
```

Expected output:
```
✓ test_escape_tag_value passed
✓ test_escape_field_string passed
✓ test_escape_measurement passed
✓ test_complete_line_protocol passed

✅ All tests passed!
```

### How to Verify (Map Guardrails)

Node endpoints (local):
```bash
curl http://localhost:8088/api/map-tiles/status | jq .
curl -X POST http://localhost:8088/api/map-tiles/increment \
  -H "Content-Type: application/json" \
  -d '{"provider":"mapbox","count":5,"month_key":"2026-01"}'
curl http://localhost:8088/metrics | rg "map_tiles_month_(total|pct)"
```

Cloud endpoints:
```bash
curl https://map.<domain>/api/fleet/map-tiles/status | jq .
curl -X POST https://map.<domain>/api/fleet/map-provider/preferred \
  -H "Content-Type: application/json" \
  -d '{"provider":"esri"}'
```

### Troubleshooting

#### Event service won't start
```bash
docker logs event-service

# Check VictoriaMetrics is reachable
docker exec event-service curl http://victoria-metrics:8428/health
```

#### Events not appearing in VictoriaMetrics
```bash
# Verify writes are succeeding (check event-service logs)
docker logs event-service | grep "Wrote.*lines to VM"

# Query VM directly
curl -s "http://localhost:8428/api/v1/query?query=ovr_event_active" | jq .
```

#### Cloudflare Access 403 errors
- Verify Access policy includes your user or service token
- Check cloudflared logs: `journalctl -u cloudflared -f`
- Test origin directly: `curl http://127.0.0.1:8088/health`

### Best Practices

1. **System IDs**: Use consistent identifiers (e.g., `rig_01`, `ovr_warehouse_unit`)
2. **Event IDs**: Create naming conventions (e.g., `startup`, `load_test_01`, `maintenance_2024_01`)
3. **Locations**: Standardize location names across your fleet
4. **Notes**: Include operator name, relevant metrics, or anomalies observed
5. **Granularity**: Events should be significant milestones (minutes to hours), not every data point

### Security
Phone/Tablet/Desktop ──HTTPS──> Cloudflare Access
Browser                              │
                                     │
                              Cloudflare Tunnel
                              (cloudflared systemd)
                                     │
                                     v
                              Event Service ──Influx──> VictoriaMetrics
                              (localhost:8088)              │
                                     │                      │
                                     │              Prometheus metrics
                                SQLite State        (ovr_event_active,
                               (active events + notes) location tag)
                                                            │
                                                            v
                                                        Grafana
```

**Security layers**:
1. HTTPS enforced by Cloudflare
2. Cloudflare Access authentication (email/SSO)
3. Service only listens on localhost (not exposed directly)
4. Optional API key for additional protection

### Further Documentation

- **[Quick Reference](docs/QUICK_REFERENCE.md)** - Common commands and queries
- **[Deployment Checklist](docs/DEPLOYMENT_CHECKLIST.md)** - Step-by-step deployment
- **[Cloudflared Setup](docs/CLOUDFLARED_EVENTS_CONFIG.md)** - Required: Expose via Cloudflare tunnel
- **[Grafana Dashboards](docs/GRAFANA_EVENTS.md)** - Visualize events and correlate with metrics
- **[Implementation Summary](docs/IMPLEMENTATION_SUMMARY.md)** - Technical overview
- **[API Reference](event-service/README.md)** - Full API documentation

## Architecture

See [.github/copilot-instructions.md](.github/copilot-instructions.md) for detailed architecture documentation.

## License

Proprietary - OVR Energy Systems
