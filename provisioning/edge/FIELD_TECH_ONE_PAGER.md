# Field Tech One-Pager (N100 Node Install)

This is a short, on-site checklist for provisioning a new node.

## 0) USB prep (before install)

USB root should contain:
- `preseed.cfg`
- `OVR_Telemetry_Stack/` (this repo)
- optional: `overdrive/firstboot.env`, `overdrive/bootstrap.args`, `overdrive/secrets/*`

## 1) First boot (no automation)

Log in, then get internet working.

### Option A: single-port quick internet (recommended for first boot)

```
sudo bash /opt/stack/provisioning/edge/bringup-network.sh --iface <iface> --mode dhcp
```

Static example:

```
sudo bash /opt/stack/provisioning/edge/bringup-network.sh --iface <iface> --mode static \
  --ip 192.168.1.10/24 --gw 192.168.1.1 --dns "1.1.1.1,8.8.8.8"
```

Verify:

```
ip -4 addr show
ip route
getent hosts deb.debian.org
ping -c 1 1.1.1.1
```

### Option B: multi-port roles (GX/WAN/Modbus)

If you must wire multiple ports:
1) Get temporary internet first (Option A).
2) Install packages.
3) Configure roles via `edge/networking/ovru-netkit`.

## 2) Install packages

```
sudo bash /opt/stack/provisioning/edge/install-packages.sh
```

This will also attempt WiFi setup **only if** `WIFI_SSID` is configured (either via `/etc/ovr/firstboot.env` or by exporting `WIFI_SSID`/`WIFI_PASS` before running the script).

## 3) WiFi change (optional)

```
sudo bash /opt/stack/provisioning/edge/setup-wifi.sh --ssid "SSID" --pass "PASSWORD"
```

If `/etc/ovr/firstboot.env` already has `WIFI_SSID` / `WIFI_PASS`,
you can run without args:

```
sudo bash /opt/stack/provisioning/edge/setup-wifi.sh
```

## 4) Multi-port roles (GX + WAN + Modbus)

Edit per-node settings:

```
sudo vim /opt/edge/networking/ovru-netkit/vars.env
```

Then apply:

```
sudo bash /opt/edge/networking/ovru-netkit/configure-ports.sh
sudo bash /opt/edge/networking/ovru-netkit/verify.sh
```

Notes:
- Set `MODBUS_ENABLE=1`, `MODBUS_IF`, `MODBUS_IP_CIDR` for a Modbus-only port.
- If you used `bringup-network.sh` (systemd-networkd), set `DISABLE_NETWORKD=1`.

## 5) Bootstrap (app + stack configuration)

Run from repo root:

```
cd /opt/stack
sudo bash provisioning/edge/bootstrap_n100.sh --deployment-id <fleet> --node-id <node>
```

### Bootstrap args explained

Identity (required):
- `--deployment-id`  fleet/group label (shared across nodes)
- `--node-id`        unique node name (e.g., `node_xx`)

Identity (optional):
- `--system-id`      hardware/system label (defaults to node-id)
- `--hostname`       set OS hostname

Networking (only for single-port NM config):
- `--lan-mode dhcp|static`
- `--lan-if <ifname>`
- `--lan-ip <cidr>` + `--lan-gw <ip>` + optional `--lan-dns`
- `--wifi-ssid <ssid>` + `--wifi-pass <pass>`

Remote write (metrics):
- `--remote-write-url`
- `--remote-write-user`
- `--remote-write-password` (or `--remote-write-password-file`)

Local VM (events):
- `--vm-write-url` / `--vm-query-url`
- `--vm-write-url-secondary`
- `--vm-write-username`
- `--vm-write-password` (or `--vm-write-password-file`)

Event service:
- `--event-api-key` (or `--event-api-key-file`)

Map tiles:
- `--mapbox-token` (or `--mapbox-token-file`)

GX integration:
- `--has-gx true|false`
- `--gx-host` (IP)
- `--gx-port` (optional)
- `--gx-user` (default: root)
- `--gx-password` (or `--gx-password-file`)

Targets (optional, override defaults):
- `--targets "job=host:port,job=host:port"`
- `--targets-file /path/to/targets.yml`

### Bootstrap args via file (recommended)

You can avoid typing by using:
- `/etc/ovr/bootstrap.args` (one arg per line)
- `/etc/ovr/firstboot.env` (shared defaults)
- `/etc/ovr/secrets/*` for passwords

If using multi-port roles with `edge/networking/ovru-netkit`, avoid `--lan-*`/`--wifi-*` flags.

## 6) Final check

```
cd /opt/stack
sudo docker compose up -d
```
