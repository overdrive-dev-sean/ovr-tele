# N100 Provisioning Guide

This guide standardizes per-node configuration under `/etc/ovr/` and keeps the repo identical across nodes.

## Automated Debian install (preseed)

Use `provisioning/edge/preseed.cfg` to run an unattended Debian 13 install with the standard layout
(EFI, `/`, `/var`, `/var/log`, `/srv/data`, no swap).

Steps:

1) Copy `provisioning/edge/preseed.cfg` to the root of the Debian installer USB as `preseed.cfg`.
2) Edit `preseed.cfg` to set the install disk (e.g., `/dev/nvme0n1`) and your desired password.
3) Boot the USB, press `e` on the install entry, and append:

```
auto=true priority=critical preseed/file=/cdrom/preseed.cfg
```

To make it permanent, add a new menu entry to `boot/grub/grub.cfg` (UEFI) or
`isolinux/txt.cfg` (legacy BIOS).

Notes:

- Hostname is prompted at install time by design.
- Installer package list is minimal. After networking is up, install packages
  using `provisioning/edge/install-packages.sh`.
- Automated first boot is disabled; use the manual steps below.

## Manual first boot (no automation)

1) Run the helper to bring up ethernet with systemd-networkd:

```
sudo bash /opt/ovr/provisioning/edge/bringup-network.sh
```

If you prefer to do it manually, use the steps below.

2) Identify the interface name and bring it up:

```
ip -br link
sudo ip link set dev <iface> up
```

3) Configure ethernet (DHCP) with systemd-networkd (no extra packages):

```
sudo tee /etc/systemd/network/10-lan.network >/dev/null <<EOF
[Match]
Name=<iface>

[Network]
DHCP=yes
EOF

sudo systemctl enable --now systemd-networkd
sudo systemctl restart systemd-networkd
```

Static config (example):

```
sudo tee /etc/systemd/network/10-lan.network >/dev/null <<EOF
[Match]
Name=<iface>

[Network]
Address=192.168.1.10/24
Gateway=192.168.1.1
DNS=1.1.1.1
DNS=8.8.8.8
EOF

sudo systemctl enable --now systemd-networkd
sudo systemctl restart systemd-networkd
```

If DHCP is still down and `dhclient` exists, try:

```
sudo dhclient -v <iface>
```

4) Verify connectivity:

```
ip -4 addr show
ip route
getent hosts deb.debian.org
ping -c 1 1.1.1.1
```

If DNS fails but IP works, set resolvers:

```
echo -e "nameserver 1.1.1.1\nnameserver 8.8.8.8" | sudo tee /etc/resolv.conf
```

5) Install packages once internet is up:

```
sudo bash /opt/ovr/provisioning/edge/install-packages.sh
```

`install-packages.sh` can optionally run WiFi setup at the end.

It will **only attempt WiFi** if `WIFI_SSID` is set (either via `/etc/ovr/firstboot.env` or by exporting env vars before running):

- `WIFI_SSID` (required)
- `WIFI_PASS` or `WIFI_PASS_FILE` (optional; blank for open network)
- `WIFI_IF` (optional)


If WiFi firmware is missing, bring up ethernet first, run the installer, then
let the WiFi setup step run once NetworkManager is installed.

6) Optional: re-run WiFi config:

```
sudo bash /opt/ovr/provisioning/edge/setup-wifi.sh --ssid "YourSSID" --pass "YourPassword"
```

If you already have `/etc/ovr/firstboot.env` with `WIFI_SSID` and
`WIFI_PASS`, you can just run:

```
sudo bash /opt/ovr/provisioning/edge/setup-wifi.sh
```

### Offline packages on the USB (optional)

If you want to install packages without internet, build a local APT repo on the
USB from another machine, then point apt at it on first boot.

On a connected Debian host:

```
mkdir -p /tmp/ovr-debs
sudo apt-get update
sudo apt-get install --download-only -o Dir::Cache::archives=/tmp/ovr-debs \
  $(cat provisioning/edge/firstboot-packages.txt provisioning/edge/firstboot-packages-wifi.txt)
sudo apt-get install -y dpkg-dev
cd /tmp/ovr-debs
dpkg-scanpackages . /dev/null > Packages
gzip -kf Packages
```

Copy `/tmp/ovr-debs` to the USB (e.g. `/cdrom/ovr/debs`).

On the node:

```
echo "deb [trusted=yes] file:/cdrom/ovr/debs ./" | \
  sudo tee /etc/apt/sources.list.d/usb-local.list
sudo apt-get update
sudo bash /opt/ovr/provisioning/edge/install-packages.sh
```

### Optional USB inputs

If you want to pre-fill values, place these on the USB root (they are copied during install).
They are not applied automatically unless you run the scripts manually.

- `authorized_keys` or `ssh_key.pub` (for `/home/ovr/.ssh/authorized_keys`)
- `ovr/firstboot.env` (optional defaults; see below)
- `ovr/bootstrap.args` (optional bootstrap args; one arg per line)
- `ovr/secrets/*` (optional secrets; see below)

`ovr/firstboot.env` supports:

- `LAN_IF`, `LAN_MODE`, `LAN_IP`, `LAN_GW`, `LAN_DNS`
- `WIFI_SSID`, `WIFI_PASS`, `WIFI_PASS_FILE`, `WIFI_IF`
- `ALLOWED_TCP_PORTS` (comma-separated)
- `EVENT_API_KEY_FILE` (passed through to bootstrap)
- `VM_WRITE_URL`, `VM_QUERY_URL`, `VM_WRITE_URL_SECONDARY`, `VM_WRITE_USERNAME`, `VM_WRITE_PASSWORD_FILE`
- `RUN_WIFI_SETUP=0` to skip WiFi drivers/tools + WiFi config
- `RUN_PACKAGE_INSTALL=0` to skip installing base packages at first boot
- `PACKAGE_LIST_FILE=/path/to/list` to override the base package list
- `WIFI_PACKAGE_LIST_FILE=/path/to/list` to override the WiFi package list
- `APT_FORCE_IPV4=1` to force IPv4 during package install
- `CONNECTIVITY_WAIT_SECONDS`, `CONNECTIVITY_RETRY_SECONDS` to wait for network before package install
- `APT_UPDATE_RETRIES`, `APT_UPDATE_WAIT_SECONDS` to retry `apt-get update`
- `DNS_PROBE_HOST`, `IP_PROBE_HOST`, `IP_PROBE_PORT` to customize connectivity probes
- `RESIZE_TTY=1` to set console width/height for wrapped output (`TTY_COLS`, `TTY_ROWS`, `TTY_DEVICE`)
- `RUN_BOOTSTRAP=0` to skip bootstrap
- `NONINTERACTIVE=1` to skip prompts

`ovr/bootstrap.args` example (one arg per line):

```
--deployment-id
fleet
--node-id
n100-01
--remote-write-url
https://metrics.example.com/api/v1/write
--remote-write-user
ovr
--remote-write-password-file
/etc/ovr/secrets/remote_write_password
```

To prompt for values at first boot, use `?` on the value line:

```
--deployment-id
?Deployment ID
--node-id
?Node ID
--remote-write-url
?Remote write URL (blank to skip)
--remote-write-user
?Remote write user (blank to skip)
```

See `provisioning/edge/bootstrap.args.prompt` for a full template.

### Secrets (USB)

To avoid plaintext in config files, put secrets on the USB under `ovr/secrets/`:

- `remote_write_password`
- `event_api_key`
- `wifi_password`
- `vm_write_password`

These are copied to `/etc/ovr/secrets/` during install. Use file paths in
`edge.env` or `firstboot.env` (e.g., `WIFI_PASS_FILE=/etc/ovr/secrets/wifi_password`).

## Per-node files

- `/etc/ovr/edge.env` (identity + remote write)
- `/etc/ovr/targets.yml` (vmagent scrape targets)
- `/etc/ovr/targets_acuvim.txt` (Modbus meters list)
- `/etc/ovr/stream_aggr.yml` (cloud downsample config for vmagent)
- `/etc/ovr/remote_write_local_relabel.yml` (drop downsampled series locally)
- `/etc/ovr/remote_write_cloud_relabel.yml` (send only downsampled series to cloud)
- `/etc/ovr/secrets/*` (remote write password file)

`DEPLOYMENT_ID` is a static fleet/group label for metrics (not the event ID used in the webapp).
`NODE_ID` should describe the physical node (e.g., `zima-01`, `n100-01`).

## Bootstrap (one command)

Run from the repo root on the N100:

```bash
sudo bash provisioning/edge/bootstrap_n100.sh \
  --deployment-id fleet \
  --node-id n100-01 \
  --lan-mode dhcp \
  --lan-if enp3s0 \
  --remote-write-url https://metrics.example.com/api/v1/write \
  --remote-write-user ovr
```

If you re-run bootstrap without providing optional flags,
it preserves any existing values from `/etc/ovr/edge.env`.

## Targets

You can supply targets explicitly:

```bash
sudo bash provisioning/edge/bootstrap_n100.sh \
  --deployment-id fleet \
  --node-id n100-01 \
  --targets "node_exporter=node_exporter:9100,event_service=events:8088"
```

Or provide a full file:

```bash
sudo bash provisioning/edge/bootstrap_n100.sh \
  --deployment-id fleet \
  --node-id n100-01 \
  --targets-file /path/to/targets.yml
```

## Cloud downsample (vmagent)

When `VM_REMOTE_WRITE_URL` is set, vmagent uses `/etc/ovr/stream_aggr.yml` to downsample
data to 10s intervals for cloud remote_write. By default, any targets on port `9100` (node_exporter)
are excluded from cloud remote_write.

To change the interval or exclusions, edit `/etc/ovr/stream_aggr.yml` and restart:

```bash
cd /opt/ovr/edge
sudo docker compose -f compose.dev.yml up -d
# If running release compose:
# sudo docker compose -f compose.release.yml up -d
```

## Acuvim meters

Edit the list, then run discovery:

```bash
sudo tee /etc/ovr/targets_acuvim.txt >/dev/null <<'EOF'
10.10.4.10
10.10.4.13
EOF

sudo bash edge/scripts/telegraf_discover_acuvim.sh
```

## LAN networking

If you need one-client DHCP + NAT for a LAN port, use the existing
`edge/networking/ovru-netkit` flow. For multi-port role assignment (LAN + WAN + Modbus),
use `edge/networking/ovru-netkit/configure-ports.sh`.

```bash
sudo vim edge/networking/ovru-netkit/vars.env
sudo bash edge/networking/ovru-netkit/configure-ports.sh
sudo bash edge/networking/ovru-netkit/verify.sh
```

Set `MODBUS_ENABLE=1` and `MODBUS_IF`/`MODBUS_IP_CIDR` in `edge/networking/ovru-netkit/vars.env`
to configure a dedicated Modbus-TCP port (no default route).

Legacy flow with package install:

```bash
sudo vim edge/networking/ovru-netkit/vars.env
sudo bash edge/networking/ovru-netkit/install.sh
sudo bash edge/networking/ovru-netkit/verify.sh
```

## Updating per-node values

Update `/etc/ovr/edge.env` or `/etc/ovr/targets.yml`, then restart:

```bash
cd /opt/ovr/edge
sudo docker compose -f compose.dev.yml up -d
# If running release compose:
# sudo docker compose -f compose.release.yml up -d
```
