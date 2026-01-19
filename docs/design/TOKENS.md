# Token Configuration Guide

This document describes where to configure access tokens for remote access services.

## Cloudflare Tunnel

### Obtaining Token
1. Visit https://one.dash.cloudflare.com/
2. Navigate to **Networks** → **Tunnels**
3. Create a new tunnel or select existing tunnel
4. Copy the tunnel token (long base64-encoded string)

### Configuration Location
`/etc/cloudflared/cloudflared.env` on the N100 host:

```bash
TUNNEL_TOKEN=<PASTE_CLOUDFLARE_TUNNEL_TOKEN_HERE>
```

### Apply Changes
```bash
sudo chmod 600 /etc/cloudflared/cloudflared.env
sudo systemctl restart cloudflared
sudo journalctl -u cloudflared -n 20 --no-pager
```

### Verify
```bash
# Should show "Registered tunnel connection"
sudo journalctl -u cloudflared --no-pager | grep -i registered
```

---

## Tailscale

### Obtaining Auth Key (Optional)
For automated deployments, create a reusable auth key:
1. Visit https://login.tailscale.com/admin/settings/keys
2. Click **Generate auth key**
3. Configure options:
   - **Reusable**: Yes (for multiple deployments)
   - **Ephemeral**: No (persist after disconnect)
   - **Preapproved**: Yes (skip manual approval)
4. Copy the key (starts with `tskey-auth-...`)

### Interactive Setup (Recommended for first deployment)
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --accept-dns=false
```
Follow the authentication URL printed by the command.

### Automated Setup (Using Auth Key)
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --authkey <tskey-auth-...> --accept-dns=false
```

### Configuration Notes
- **`--accept-dns=false`**: Prevents Tailscale from overriding system DNS (important for LTE/Wi-Fi failover)
- Auth keys expire based on settings (default: 90 days)
- Reusable keys can be revoked from admin console

### Verify
```bash
sudo tailscale status
sudo tailscale ip -4
```

---

## Security Best Practices

1. **Never commit tokens to git**: Add `/etc/cloudflared/cloudflared.env` to .gitignore (already excluded)
2. **Restrict file permissions**: `chmod 600` on token files
3. **Rotate regularly**: Generate new tokens every 90 days
4. **Use separate tokens per site**: Don't reuse production tokens across deployments
5. **Revoke compromised tokens immediately**: 
   - Cloudflare: Delete tunnel from admin panel
   - Tailscale: Revoke key from admin console

---

## Grafana Admin Password

Default credentials are `admin/admin`. Change on first login via Grafana UI.

To reset admin password:
```bash
sudo docker exec -it grafana grafana-cli admin reset-admin-password newpassword
sudo docker compose restart grafana
```

---

## Deployment Checklist

- [ ] Configure `/etc/cloudflared/cloudflared.env` with tunnel token
- [ ] Set permissions: `sudo chmod 600 /etc/cloudflared/cloudflared.env`
- [ ] Start cloudflared: `sudo systemctl enable --now cloudflared`
- [ ] Install Tailscale and authenticate
- [ ] Change Grafana admin password
- [ ] Document tokens in secure password manager (not in this repo)
