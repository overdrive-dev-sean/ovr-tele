# Cloudflared Configuration for Event Service

This file shows how to add the OVR Event Service to your existing cloudflared tunnel configuration.

## Prerequisites

- Cloudflared tunnel already configured (as systemd service)
- Event service running on `http://127.0.0.1:8088`
- Cloudflare Access configured for your domain

## Configuration Location

Cloudflared config is typically at `/etc/cloudflared/config.yml`

Check your systemd service to confirm:
```bash
systemctl cat cloudflared
```

## Add Ingress Rule

Edit `/etc/cloudflared/config.yml` and add this ingress rule **before** the catch-all rule:

```yaml
tunnel: <your-tunnel-id>
credentials-file: /etc/cloudflared/<your-tunnel-id>.json

ingress:
  # OVR Event Service with Cloudflare Access enforcement
  - hostname: events.yourdomain.com
    service: http://127.0.0.1:8088
    originRequest:
      access:
        required: true
        teamName: your-team-name
        audTag:
          - event-service-access

  # Add other services here...
  
  # Catch-all rule (must be last)
  - service: http_status:404
```

### Configuration Parameters

- **hostname**: Your subdomain (e.g., `events.yourdomain.com`)
- **service**: Event service URL (default: `http://127.0.0.1:8088`)
- **teamName**: Your Cloudflare Zero Trust team name (find in Zero Trust dashboard URL: `https://<teamName>.cloudflareaccess.com`)
- **audTag**: Access policy audience tags (optional, but recommended for granular control)

## Setup Steps

### 1. Configure DNS in Cloudflare

Add a CNAME record:
```
Type: CNAME
Name: events
Target: <your-tunnel-id>.cfargotunnel.com
Proxy status: Proxied (orange cloud)
```

### 2. Create Cloudflare Access Policy

In Cloudflare Zero Trust dashboard:

1. Go to **Access** → **Applications** → **Add an application**
2. Select **Self-hosted**
3. Configure:
   - **Application name**: OVR Event Service
   - **Subdomain**: `events` (or your choice)
   - **Domain**: `yourdomain.com`
   - **Session Duration**: 24 hours (or your preference)
4. Add policies:
   - **Policy name**: Allow Operators
   - **Action**: Allow
   - **Include**: Emails matching `*@yourcompany.com` (or specific users/groups)
   - **Audience (AUD) Tag**: `event-service-access` (matches audTag in config)
5. Save application

### 3. Update cloudflared Config

```bash
sudo nano /etc/cloudflared/config.yml
```

Add the ingress rule shown above.

### 4. Validate Configuration

```bash
cloudflared tunnel ingress validate
```

Expected output:
```
Validating rules...
OK
```

### 5. Restart cloudflared

```bash
sudo systemctl restart cloudflared
sudo systemctl status cloudflared
```

Check logs:
```bash
sudo journalctl -u cloudflared -n 50 --no-pager
```

## Testing

### Browser Access (with Cloudflare Access)

1. Navigate to `https://events.yourdomain.com`
2. You'll be prompted to authenticate via Cloudflare Access
3. After authentication, you'll see the Event Marker UI

### API Access (for automation/scripts)

For automated API calls (e.g., from monitoring scripts), use Service Tokens:

#### Service Token (for automated clients)

1. In Cloudflare Zero Trust → **Access** → **Service Auth** → **Service Tokens**
2. Create new token: "API Automation"
3. Copy `Client ID` and `Client Secret`
4. Use in API requests:
   ```bash
   curl -X POST https://events.yourdomain.com/api/event/start \
     -H "Content-Type: application/json" \
     -H "CF-Access-Client-Id: <your-client-id>" \
     -H "CF-Access-Client-Secret: <your-client-secret>" \
     -d '{"system_id":"rig_01","event_id":"test"}'
   ```
5. Add this service token to the Access policy created above:
   - Edit policy → Include → **Service Auth** → Select your token

## Local Testing (without Cloudflare Access)

For local development/testing, access directly:
```bash
curl http://127.0.0.1:8088/health
```

Or from another machine on the network:
```bash
curl http://<n100-ip>:8088/health
```

## Troubleshooting

### 403 Forbidden
- Check Access policy includes your user/email
- Verify audTag matches if configured
- Try incognito window to clear auth cache

### 502 Bad Gateway
- Verify event service is running: `docker ps | grep events`
- Check service is listening: `curl http://127.0.0.1:8088/health`
- Review cloudflared logs: `journalctl -u cloudflared -f`

### Service Token Not Working
- Verify token is added to Access policy
- Token must be included in **every** request

## Security Notes

1. **HTTPS Only**: Cloudflare enforces HTTPS for all tunnel traffic
2. **Access Control**: All requests must pass through Cloudflare Access authentication
3. **Origin Security**: Event service only listens on localhost (not exposed directly)
4. **API Key**: Optional `X-API-Key` header provides additional layer (set via docker compose `EVENT_API_KEY` env var)

## Complete Example Config

```yaml
tunnel: abc123-def456-ghi789
credentials-file: /etc/cloudflared/abc123-def456-ghi789.json

ingress:
  # Grafana (example)
  - hostname: grafana.yourdomain.com
    service: http://127.0.0.1:3000
    originRequest:
      access:
        required: true
        teamName: your-team-name

  # OVR Event Service
  - hostname: events.yourdomain.com
    service: http://127.0.0.1:8088
    originRequest:
      access:
        required: true
        teamName: your-team-name
        audTag:
          - event-service-access

  # Catch-all
  - service: http_status:404
```

## References

- [Cloudflare Tunnel Ingress Rules](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/tunnel-guide/local/local-management/ingress/)
- [Cloudflare Access Policies](https://developers.cloudflare.com/cloudflare-one/policies/access/)
- [Service Tokens](https://developers.cloudflare.com/cloudflare-one/identity/service-tokens/)
