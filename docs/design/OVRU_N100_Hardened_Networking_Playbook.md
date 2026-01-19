# OVRU N100 “Hardened Networking Host” — One‑Document Playbook (Debian 13)

This is a **single, copy‑pasteable** playbook to reproduce the exact “hardened” setup we built:

- **GX LAN port** on the N100: static IP + **single‑IP DHCP** (always hands out `192.168.100.2`)
- **NAT** that LAN out whichever upstream is active
- Two upstreams:
  - **Ethernet to LTE router** (sometimes true WAN; sometimes “just a switch” with SIM disabled)
  - **USB Wi‑Fi** (`wan-wifi`) as shop WAN and/or fallback
- **cloudflared** (Cloudflare Tunnel) hardened across WAN/DNS changes
- **WAN selector** automation: if LTE router has no internet, it won’t steal default route

> **Secrets are injected per site** (Wi‑Fi PSK changes, Cloudflare tunnel token, optional Tailscale auth). This doc does **not** embed secrets.

If you prefer a scripted setup, edit `edge/networking/ovru-netkit/vars.env` and run:

```bash
sudo bash edge/networking/ovru-netkit/configure-ports.sh
```

To enable a dedicated Modbus-TCP port, set `MODBUS_ENABLE=1` plus `MODBUS_IF` and
`MODBUS_IP_CIDR` in `edge/networking/ovru-netkit/vars.env`.

The rest of this document is the manual, step-by-step equivalent.

---

## 0) What you get at the end

### Interfaces (example defaults)
- **LAN for GX**: `enp3s0` → `192.168.100.1/30`, DHCP gives one client `192.168.100.2`
- **LTE router Ethernet**: `enp2s0` → DHCP client (may or may not have actual internet)
- **USB Wi‑Fi adapter**: `wlx…` with NM profile name `wan-wifi`

### LAN network plan
- N100 LAN IP: `192.168.100.1/30`
- Client lease: `192.168.100.2`
- Only one usable client IP (by design)

---

## 1) Preflight: identify your interface names

Run:
```bash
ip -br link
nmcli -t -f DEVICE,TYPE,STATE dev status
```

Pick:
- `LAN_IF`  = the port your **GX** plugs into (example `enp3s0`)
- `WAN_ETH_IF` = the port going to the **LTE router** (example `enp2s0`)
- `WIFI_DEV` = USB Wi‑Fi device name (example `wlx40ae301f0240`)

---

## 2) One-time: connect Wi‑Fi and name the profile `wan-wifi` (site secret injection)

### Connect once (creates a profile)
```bash
nmcli dev wifi list
nmcli dev wifi connect "YOUR_SSID" password "YOUR_PASSWORD"
```

### Rename the created SSID profile to a stable name
```bash
nmcli -t -f NAME,TYPE con show | grep 802-11-wireless
sudo nmcli con mod "YOUR_SSID" connection.id "wan-wifi"
sudo nmcli con up wan-wifi
```

### Recommended for USB Wi‑Fi stability (power save OFF)
```bash
sudo tee /etc/NetworkManager/conf.d/10-wifi-powersave.conf >/dev/null <<'EOF'
[connection]
wifi.powersave=2
EOF
sudo systemctl restart NetworkManager
```

---

## 3) Install the baseline: LAN DHCP (single IP) + NAT + forwarding + WAN metrics

> This section is **safe to paste** on a fresh Debian 13 box.
> It assumes NetworkManager is used (Debian default on desktops; installable on servers).

### 3.1 Set variables (EDIT THESE 3!)
```bash
LAN_IF="enp3s0"
WAN_ETH_IF="enp2s0"
WIFI_CON="wan-wifi"

LAN_IP_CIDR="192.168.100.1/30"
LAN_CLIENT_IP="192.168.100.2"
```

### 3.2 Install packages
```bash
sudo apt update
sudo apt install -y network-manager dnsmasq nftables procps curl bind9-dnsutils
sudo systemctl enable --now NetworkManager
```

### 3.3 Configure LAN interface (static) in NetworkManager
```bash
sudo nmcli con delete oneclient 2>/dev/null || true

sudo nmcli con add type ethernet ifname "$LAN_IF" con-name oneclient   ipv4.addresses "$LAN_IP_CIDR" ipv4.method manual ipv6.method ignore

# LAN must NEVER become default route:
sudo nmcli con mod oneclient ipv4.never-default yes ipv6.never-default yes
sudo nmcli con mod oneclient connection.autoconnect yes
sudo nmcli con up oneclient
```

### 3.4 Configure LTE-router Ethernet (DHCP client) and set preferred metrics
```bash
sudo nmcli con delete wan-enp2s0 2>/dev/null || true

sudo nmcli con add type ethernet ifname "$WAN_ETH_IF" con-name wan-enp2s0   ipv4.method auto ipv6.method ignore

# Prefer wired WAN when it's valid:
sudo nmcli con mod wan-enp2s0 connection.autoconnect yes ipv4.route-metric 100 connection.autoconnect-priority 10
sudo nmcli con up wan-enp2s0 || true

# Wi-Fi as fallback (higher metric):
sudo nmcli con mod "$WIFI_CON" connection.autoconnect yes ipv4.route-metric 600 connection.autoconnect-priority -10
sudo nmcli con up "$WIFI_CON" || true
```
Note: `ipv4.route-metric` picks the preferred default route when multiple links are up (lower wins). `connection.autoconnect-priority` controls which profile NetworkManager tries to bring up first (higher wins). Keep WAN metric lower than Wi-Fi and WAN autoconnect priority higher so LTE wins and Wi-Fi stays fallback.

### 3.5 Enable IP forwarding
```bash
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-ipforward.conf
sudo sysctl --system
```

### 3.6 dnsmasq single-IP DHCP server on LAN
We use `bind-dynamic` so dnsmasq doesn’t freak out if the interface is “late” during boot.

```bash
sudo tee /etc/dnsmasq.d/oneclient-dhcp.conf >/dev/null <<EOF
port=0

interface=$LAN_IF
bind-dynamic
dhcp-authoritative

# Exactly one lease. 5m is swap-friendly; change to 30m/1h later if desired.
dhcp-range=$LAN_CLIENT_IP,$LAN_CLIENT_IP,255.255.255.252,5m

dhcp-option=option:router,${LAN_IP_CIDR%/*}
dhcp-option=option:dns-server,1.1.1.1,8.8.8.8
EOF

sudo systemctl enable --now dnsmasq
sudo systemctl restart dnsmasq
sudo systemctl status dnsmasq --no-pager
```

**If you swap devices and the new one doesn’t get a lease:** clear it:
```bash
sudo rm -f /var/lib/misc/dnsmasq.leases
sudo systemctl restart dnsmasq
```

### 3.7 NAT via nftables (dynamic upstream)
This NATs only the GX LAN subnet, and works no matter which upstream is active.

```bash
# Compute the /30 base (for 192.168.100.1/30 it is 192.168.100.0/30)
LAN_NET="$(echo "$LAN_IP_CIDR" | cut -d/ -f1 | awk -F. '{printf "%d.%d.%d.%d", $1,$2,$3, int($4/4)*4 }')"
LAN_SUBNET="${LAN_NET}/30"

sudo mkdir -p /etc/nftables.d
sudo tee /etc/nftables.d/oneclient-nat.nft >/dev/null <<EOF
table ip oneclient_nat {
  chain postrouting {
    type nat hook postrouting priority srcnat;
    ip saddr $LAN_SUBNET oifname != "$LAN_IF" masquerade
  }
}
EOF

# Include the directory from /etc/nftables.conf (one-time)
grep -q 'include "/etc/nftables.d/\*\.nft"' /etc/nftables.conf ||   echo 'include "/etc/nftables.d/*.nft"' | sudo tee -a /etc/nftables.conf >/dev/null

sudo systemctl enable --now nftables
sudo systemctl restart nftables

sudo nft list ruleset | grep -n masquerade || true
```

---

## 4) Cloudflare Tunnel (cloudflared) — single source of truth

### 4.1 Install cloudflared (choose one method)
Confirm the binary path:
```bash
command -v cloudflared
```

If it is in `/usr/local/bin/cloudflared`, great. If it’s elsewhere, adjust the systemd unit below accordingly.

### 4.2 Inject tunnel token (SECRET per host)
```bash
sudo mkdir -p /etc/cloudflared
sudo tee /etc/cloudflared/cloudflared.env >/dev/null <<'EOF'
TUNNEL_TOKEN=PASTE_TOKEN_HERE
EOF
sudo chmod 600 /etc/cloudflared/cloudflared.env
```

### 4.3 systemd unit
We force `http2` because it’s typically more reliable on enterprise Wi‑Fi (roaming/VLAN weirdness).

```bash
CLOUDFLARED_BIN="$(command -v cloudflared)"
echo "Using: $CLOUDFLARED_BIN"

sudo tee /etc/systemd/system/cloudflared.service >/dev/null <<EOF
[Unit]
Description=cloudflared
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
EnvironmentFile=/etc/cloudflared/cloudflared.env
ExecStart=$CLOUDFLARED_BIN --no-autoupdate tunnel --protocol http2 run --token \${TUNNEL_TOKEN}

Restart=always
RestartSec=5
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared
sudo systemctl restart cloudflared
sudo journalctl -u cloudflared -n 40 --no-pager
```

### 4.4 If you accidentally break the token
Symptom: `Unauthorized: Invalid tunnel secret`

Fix: replace `TUNNEL_TOKEN=...` in `/etc/cloudflared/cloudflared.env`, then:
```bash
sudo systemctl restart cloudflared
```

### 4.5 Optional: disable cloudflared updater timer (stability)
```bash
sudo systemctl disable --now cloudflared-update.timer 2>/dev/null || true
sudo systemctl stop cloudflared-update.service 2>/dev/null || true
```

---

## 5) “LTE router is sometimes WAN, sometimes switch” — WAN selector automation (recommended)

### Problem we’re solving
Your LTE router will happily hand out a default gateway even when:
- SIM is disabled, and
- it has no upstream

That **breaks DNS** (and cloudflared) because your default route points to a dead internet path.

### Solution
A timer runs a health-check *through the LTE ethernet interface*:
- if LTE path can reach internet → allow LTE to be default route
- if not → set LTE connection to `never-default` so Wi‑Fi becomes default route

We also make DNS deterministic:
- when LTE is selected as WAN, force DNS to **LTE gateway only** (many LTE routers blackhole public DNS to 1.1.1.1/8.8.8.8)

### 5.1 Install the selector script
```bash
sudo tee /usr/local/sbin/wan-select.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

LTE_IF="enp2s0"
LTE_CON="wan-enp2s0"
WIFI_CON="wan-wifi"

# Health check: HTTPS request via the LTE interface
CHECK_URL="https://cloudflare.com/cdn-cgi/trace"

STATE_FILE="/run/wan-select.state"
LOGTAG="wan-select"

lte_gateway() {
  nmcli -g IP4.GATEWAY device show "$LTE_IF" | head -n1
}

lte_has_internet() {
  timeout 4 curl --interface "$LTE_IF" -fsS "$CHECK_URL" >/dev/null 2>&1
}

set_lte_default() {
  local gw
  gw="$(lte_gateway)"

  # LTE can be default route; prefer it
  nmcli con mod "$LTE_CON" ipv4.never-default no ipv6.never-default yes ipv4.route-metric 100
  nmcli con mod "$WIFI_CON" ipv4.route-metric 600

  # Force DNS to LTE gateway only (prevents public DNS timeouts on many LTE routers)
  if [[ -n "$gw" ]]; then
    nmcli con mod "$LTE_CON" ipv4.ignore-auto-dns yes ipv4.dns "$gw" ipv4.dns-search ""
  fi
}

set_wifi_default() {
  # LTE stays up for reaching modbus devices, but cannot be default route
  nmcli con mod "$LTE_CON" ipv4.never-default yes ipv6.never-default yes

  # Wi-Fi becomes default
  nmcli con mod "$WIFI_CON" ipv4.never-default no ipv6.never-default yes ipv4.route-metric 100

  # Return LTE DNS to auto (so it doesn't pollute resolv.conf when LTE isn't WAN)
  nmcli con mod "$LTE_CON" ipv4.ignore-auto-dns no ipv4.dns "" ipv4.dns-search ""
}

# Ensure both connections exist/up (best effort)
nmcli con up "$WIFI_CON" >/dev/null 2>&1 || true
nmcli con up "$LTE_CON"  >/dev/null 2>&1 || true

if lte_has_internet; then
  WANT="lte"
  set_lte_default
else
  WANT="wifi"
  set_wifi_default
fi

PREV="$(cat "$STATE_FILE" 2>/dev/null || true)"
if [[ "$WANT" != "$PREV" ]]; then
  echo "$WANT" > "$STATE_FILE"
  logger -t "$LOGTAG" "WAN switched to: $WANT"
  # Let routes/DNS settle, then restart cloudflared
  sleep 3
  systemctl restart cloudflared || true
fi
EOF

sudo chmod +x /usr/local/sbin/wan-select.sh
```

> **EDIT NOTE:** If your LTE interface is not `enp2s0`, change `LTE_IF=...` at the top of the script.

### 5.2 Run once (manual test)
```bash
sudo /usr/local/sbin/wan-select.sh
ip route get 1.1.1.1
cat /etc/resolv.conf
sudo journalctl -t wan-select -n 40 --no-pager
```

### 5.3 Install as a systemd timer (always running)
```bash
sudo tee /etc/systemd/system/wan-select.service >/dev/null <<'EOF'
[Unit]
Description=Select WAN based on LTE internet reachability
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/wan-select.sh
EOF

sudo tee /etc/systemd/system/wan-select.timer >/dev/null <<'EOF'
[Unit]
Description=Run WAN selector periodically

[Timer]
OnBootSec=10
OnUnitActiveSec=15
AccuracySec=5

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now wan-select.timer
systemctl list-timers --all | grep wan-select
```

---

## 6) Fast verification & helpers (use these constantly)

### 6.1 “Am I healthy?” — one-shot verification
```bash
echo "=== NM status ==="
nmcli -t -f DEVICE,STATE,CONNECTION dev status

echo
echo "=== Default route ==="
ip route get 1.1.1.1 || true

echo
echo "=== resolv.conf ==="
cat /etc/resolv.conf || true

echo
echo "=== LAN DHCP leases ==="
sudo cat /var/lib/misc/dnsmasq.leases 2>/dev/null || true

echo
echo "=== NAT masquerade ==="
sudo nft list ruleset | grep -n masquerade || true

echo
echo "=== cloudflared ==="
systemctl is-active cloudflared || true
sudo journalctl -u cloudflared -n 20 --no-pager || true
```

### 6.2 “Cloudflared is down” — debug checklist
1) Check DNS:
```bash
cat /etc/resolv.conf
dig +time=2 +tries=1 SRV _v2-origintunneld._tcp.argotunnel.com
```

2) Check cloudflared logs:
```bash
sudo journalctl -u cloudflared -n 80 --no-pager
```

3) Restart cloudflared:
```bash
sudo systemctl restart cloudflared
```

### 6.3 “GX won’t get an IP” (single-lease pool)
Most common: the last client still holds the lease.

```bash
sudo cat /var/lib/misc/dnsmasq.leases
sudo rm -f /var/lib/misc/dnsmasq.leases
sudo systemctl restart dnsmasq
```

### 6.4 Windows SSH “host key changed” fix
When an IP gets reused or you reinstalled Debian:
```powershell
ssh-keygen -R 10.10.4.20
ssh ovradmin@10.10.4.20
```

---

## 7) Replication strategy (no secrets baked in)

### What is safe to replicate “as-is”
- NetworkManager **non-secret** settings (metrics, never-default on LAN)
- dnsmasq config (single lease)
- nftables config (NAT)
- sysctl ip_forward
- wan-select scripts + timers
- cloudflared systemd unit (but NOT the token)

### What must be injected per site
- Wi‑Fi credentials (create/rename profile to `wan-wifi`)
- Cloudflare tunnel token in `/etc/cloudflared/cloudflared.env`
- Optional: Tailscale auth / accept-dns preference

---

## 8) Optional: keep Tailscale without breaking DNS (recommended mode)
If you use Tailscale for emergencies but want NetworkManager DNS to remain authoritative:
```bash
sudo tailscale set --accept-dns=false
sudo systemctl restart tailscaled
```

---

## 9) “One file to copy” idea
If you want a *single* self-contained installer script later, you can paste sections 3–5 into one script and run it. This doc is intentionally explicit and readable, but it can be automated further.

---

## 10) Quick sanity tests (copy/paste)
### Test outbound HTTP via the current default route
```bash
curl -I https://cloudflare.com --max-time 5
```

### Test that cloudflared SRV lookup works (system resolver)
```bash
dig +time=2 +tries=1 SRV _v2-origintunneld._tcp.argotunnel.com
```

### Test WAN selector state/log
```bash
sudo journalctl -t wan-select -n 80 --no-pager
```

---

### End
If you want, we can turn this into a proper `edge/networking/ovru-netkit` repo with:
- `/scripts/install.sh` (prompts for interface names)
- `/scripts/inject-secrets.sh` (asks for SSID/password + tunnel token)
- `/scripts/verify.sh`
…so you can deploy a new N100 in ~5 minutes.
