# OVR Event Service - Quick Reference

## Access

**Web UI**: `https://events.yourdomain.com`  
**Local** (N100 only): `http://localhost:8088`

## Common Commands

### Check Service Status
```bash
docker ps | grep events
docker logs events --tail 50
curl http://localhost:8088/health
```

### GX Exporter Scripts (on GX device)
The GX runs two dbus2prom exporters: fast (9480) and slow (9481).

```bash
# Restart both exporters on GX
/data/dbus2prom/run_exporters.sh

# Quick health check (BusyBox-safe)
busybox netstat -ltn | grep -E ':(9480|9481)\b'
curl -fsS -m 2 http://127.0.0.1:9480/metrics | head -n 5
curl -fsS -m 2 http://127.0.0.1:9481/metrics | head -n 5
```

### Start Event
```bash
curl -X POST https://events.yourdomain.com/api/event/start \
  -H "Content-Type: application/json" \
  -d '{"system_id":"rig_01","event_id":"startup","location":"warehouse"}'
```

### End Event
```bash
curl -X POST https://events.yourdomain.com/api/event/end \
  -H "Content-Type: application/json" \
  -d '{"system_id":"rig_01"}'
```

### End All Loggers (Auto-Generates Report)
```bash
curl -X POST https://events.yourdomain.com/api/event/end_all \
  -H "Content-Type: application/json" \
  -d '{"event_id":"startup"}'
```
Response includes `report_url` field with link to HTML report.

## Reports

### View Event Report
```bash
# HTML report (no auth required)
open https://events.yourdomain.com/api/reports/{event_id}/html

# JSON data (requires API key)
curl https://events.yourdomain.com/api/reports/{event_id} \
  -H "x-api-key: your_api_key"
```

### Generate Report Manually
```bash
curl -X POST http://localhost:8088/api/reports/generate \
  -H "x-api-key: your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"event_id": "startup"}'
```

### List All Reports
```bash
curl http://localhost:8088/api/reports
```

### Check Report Files
```bash
docker exec events ls -lh /data/reports/
docker exec events cat /data/reports/event_startup_*/data.json | jq
```

### Set Location
```bash
curl -X POST https://events.yourdomain.com/api/location/set \
  -H "Content-Type: application/json" \
  -d '{"system_id":"rig_01","location":"field_a"}'
```

### Check Status
```bash
curl https://events.yourdomain.com/api/status?system_id=rig_01 | jq .
```

## Grafana Queries

### Event Timeline
```promql
ovr_event_active{system_id="rig_01"}
```

### Location Changes
```promql
ovr_event_active{system_id="rig_01"}
```

**Note**: Use the `location` label to see location changes over time.

### Voltage During Event
```promql
victron_dc_voltage_v{} 
  and on(system_id) ovr_event_active{event_id="load_test"} == 1
```

**Note**: Event notes are stored in SQLite and displayed in the web UI only (not queryable in Grafana).

## Web UI Features

- **Real-time metrics**: SOC (10s), Alerts/Pin/Pout (1s update intervals)
- **Multi-logger support**: Manage multiple services per event
- **Notes system**: Service-tagged notes with bulk delete (stored in SQLite)
- **GX control**: Adjust inverter settings via SSH (reads from VictoriaMetrics)
- **Responsive design**: Mobile-first, touch-friendly interface

## Troubleshooting

### Can't Access Web UI
```bash
# Check cloudflared
sudo systemctl status cloudflared
sudo journalctl -u cloudflared -n 20

# Check service
docker ps | grep events
curl http://localhost:8088/health
```

### Events Not Appearing
```bash
# Check service logs
docker logs events | grep -i error

# Query VictoriaMetrics
curl 'http://localhost:8428/api/v1/query?query=ovr_event_active' | jq .
```

### Container Restart
```bash
cd /opt/edge && docker compose -f compose.dev.yml restart events
docker logs events -f
```

## File Locations

- **Config**: `edge/compose.dev.yml`
- **Database**: Docker volume `event_data` → `/data/events.db`
- **Logs**: `docker logs events`
- **Cloudflared**: `/etc/cloudflared/config.yml`

## Key Concepts

**System ID**: Unique rig identifier (e.g., `rig_01`, `warehouse_unit`)  
**Event ID**: Event name (e.g., `startup`, `load_test`, `maintenance`)  
**Location**: Physical location (e.g., `warehouse`, `field_a`, `shop`)  
**Note**: Text observation/annotation

## Data in VictoriaMetrics

```
# Event start/location
ovr_event,event_id=startup,system_id=rig_01,location=warehouse active=1i <timestamp_ns>

# Event end
ovr_event,event_id=startup,system_id=rig_01,location=warehouse active=0i <timestamp_ns>
```

## Backup/Restore

### Backup
```bash
docker exec events sqlite3 /data/events.db .dump > events_backup.sql
```

### Restore
```bash
cat events_backup.sql | docker exec -i events sqlite3 /data/events.db
```

## Links

- **Main README**: [../README.md](../README.md#event--location-marker-system)
- **Cloudflare Setup**: [CLOUDFLARED_EVENTS_CONFIG.md](CLOUDFLARED_EVENTS_CONFIG.md)
- **Grafana Examples**: [GRAFANA_EVENTS.md](GRAFANA_EVENTS.md)
- **Deployment Guide**: [DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md)
- **Implementation Details**: [IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md)
