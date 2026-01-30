# OVR Event Service - Deployment Checklist

Quick reference for deploying the Event + Location Marker system.

## Prerequisites

- [ ] N100/Zima host with Docker and docker-compose
- [ ] VictoriaMetrics running and accessible
- [ ] Cloudflared tunnel configured as systemd service
- [ ] Cloudflare Access account set up
- [ ] Domain name for tunnel ingress

## Deployment Steps

### 1. Deploy Event Service Container

```bash
cd /opt/ovr/edge  # or your repo location

# Dev/local (build on the node)
sudo docker compose -f compose.dev.yml up -d --build events

# Production/release (pull pinned image)
cp edge.env.example edge.env
# edit EDGE_VERSION + GHCR_OWNER
sudo docker compose --env-file edge.env -f compose.release.yml up -d events
```

**Verify**:
```bash
docker ps | grep events
curl http://localhost:8088/health
# Should return: {"status":"ok","vm_url":"http://victoria-metrics:8428/write"}
```

### 2. Configure DNS (Cloudflare Dashboard)

- [ ] Add CNAME record:
  - **Name**: `events`
  - **Target**: `<your-tunnel-id>.cfargotunnel.com`
  - **Proxy status**: Proxied (orange cloud)

### 3. Create Cloudflare Access Application

In Cloudflare Zero Trust dashboard:

- [ ] **Access** → **Applications** → **Add application** → **Self-hosted**
- [ ] Configure application:
  - **Name**: OVR Event Service
  - **Subdomain**: `events`
  - **Domain**: `yourdomain.com`
  - **Session Duration**: 24 hours
- [ ] Create policy:
  - **Policy name**: Allow Operators
  - **Action**: Allow
  - **Include**: Emails ending in `@yourcompany.com` (or specific users)
  - **Audience Tag**: `event-service-access`
- [ ] Save application

### 4. Update Cloudflared Configuration

Edit `/etc/cloudflared/config.yml`:

```yaml
ingress:
  # Add BEFORE catch-all rule:
  - hostname: events.yourdomain.com
    service: http://127.0.0.1:8088
    originRequest:
      access:
        required: true
        teamName: your-team-name  # From Zero Trust dashboard URL
        audTag:
          - event-service-access
  
  # Existing rules...
  - service: http_status:404  # Catch-all (must be last)
```

**Validate**:
```bash
sudo cloudflared tunnel ingress validate
```

**Restart**:
```bash
sudo systemctl restart cloudflared
sudo systemctl status cloudflared
sudo journalctl -u cloudflared -n 20 --no-pager
```

### 5. Test Access

**From browser**:
- [ ] Navigate to `https://events.yourdomain.com`
- [ ] Authenticate via Cloudflare Access
- [ ] Verify UI loads correctly

**Test API call**:
```bash
curl -X POST https://events.yourdomain.com/api/event/start \
  -H "Content-Type: application/json" \
  -d '{"system_id":"test_rig","event_id":"deployment_test"}'
```

**Check VictoriaMetrics**:
```bash
curl -s 'http://localhost:8428/api/v1/query?query=ovr_event_active' | jq .
```

### 6. Configure Grafana (Optional)

- [ ] Add dashboard panels for event visualization
- [ ] Configure annotations for event markers
- [ ] Set up variables for system_id filtering

See [docs/GRAFANA_EVENTS.md](GRAFANA_EVENTS.md) for examples.

## Post-Deployment

### Set System IDs

Document your system ID naming convention:
- Example: `rig_01`, `rig_02`, `warehouse_unit`, etc.

### Train Users

Share access URL with operators:
```
https://events.yourdomain.com
```

Key workflows:
1. Enter System ID
2. Start Event (with Event ID)
3. Add notes during event
4. Set location when moving
5. End event when complete

### Monitoring

Check event service health:
```bash
# Container status
docker ps | grep events
docker stats events --no-stream

# Logs
docker logs events -f

# Database size
docker exec events du -h /data/events.db
```

Check cloudflared health:
```bash
systemctl status cloudflared
journalctl -u cloudflared -f
```

## Optional Enhancements

### Service Token for API Automation

If you need automated API access (scripts, integrations):

1. Create Service Token in Cloudflare Zero Trust
2. Add token to Access policy
3. Use in API calls:
   ```bash
   curl -H "CF-Access-Client-Id: <id>" \
        -H "CF-Access-Client-Secret: <secret>" \
        https://events.yourdomain.com/api/status
   ```

### API Key Authentication

Add extra security layer via docker-compose:

```yaml
environment:
  - EVENT_API_KEY=your_secret_key_here
```

Then include in requests:
```bash
curl -H "X-API-Key: your_secret_key_here" \
     https://events.yourdomain.com/api/event/start ...
```

### Backup/Restore

Backup SQLite database:
```bash
docker exec events sqlite3 /data/events.db .dump > events_backup.sql
```

Restore:
```bash
cat events_backup.sql | docker exec -i events sqlite3 /data/events.db
```

## Troubleshooting

### Can't access web UI

**403 Forbidden**:
- Check Access policy includes your email
- Verify audTag matches config
- Try incognito mode

**502 Bad Gateway**:
- Verify event service running: `docker ps`
- Check service health: `curl http://localhost:8088/health`
- Review cloudflared logs: `journalctl -u cloudflared -n 50`

**Connection timeout**:
- Verify DNS resolves: `nslookup events.yourdomain.com`
- Check cloudflared status: `systemctl status cloudflared`

### Events not in VictoriaMetrics

```bash
# Check service logs for write errors
docker logs events | grep -i error

# Test VM endpoint
docker exec events curl http://victoria-metrics:8428/health

# Query VM directly
curl 'http://localhost:8428/api/v1/query?query=ovr_event_active'
```

### High latency

- Check cloudflared tunnel latency: `journalctl -u cloudflared | grep latency`
- Verify VictoriaMetrics not overloaded: `docker stats victoria-metrics`
- Review event service response times in logs

## Rollback

If needed, stop event service:
```bash
cd /opt/ovr/edge && sudo docker compose -f compose.dev.yml stop events
```

Remove cloudflared ingress rule and restart:
```bash
# Edit /etc/cloudflared/config.yml (remove events ingress)
sudo systemctl restart cloudflared
```

## Success Criteria

- [ ] Web UI accessible via `https://events.yourdomain.com`
- [ ] Can start/end events successfully
- [ ] Events appear in VictoriaMetrics within 5 seconds
- [ ] Grafana can query event metrics
- [ ] No errors in event service logs
- [ ] No errors in cloudflared logs
- [ ] Mobile/tablet access works

## Support

- **Event Service**: Check `docker logs events`
- **Cloudflared**: Check `journalctl -u cloudflared`
- **VictoriaMetrics**: Check `docker logs victoria-metrics`
- **API Examples**: Run `bash edge/services/events/examples.sh`
