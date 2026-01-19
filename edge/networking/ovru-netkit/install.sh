#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/vars.env"

LAN_DNS="${LAN_DNS:-1.1.1.1,8.8.8.8}"
LAN_DHCP_LEASE="${LAN_DHCP_LEASE:-5m}"
LAN_DHCP_NETMASK="${LAN_DHCP_NETMASK:-255.255.255.252}"

LAN_SUBNET="${LAN_IP_CIDR%/*}/30"  # (we’ll compute properly below)

# Compute LAN subnet base from LAN_IP_CIDR (assumes /30 like 192.168.100.1/30)
LAN_NET="$(echo "$LAN_IP_CIDR" | cut -d/ -f1 | awk -F. '{printf "%d.%d.%d.%d", $1,$2,$3, int($4/4)*4 }')"
LAN_SUBNET="${LAN_NET}/30"

echo "== Installing packages =="
sudo apt-get update
sudo apt-get install -y network-manager dnsmasq nftables procps curl

echo "== Enable IP forwarding =="
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-ipforward.conf >/dev/null
sudo sysctl --system >/dev/null

echo "== Configure LAN (oneclient) on ${LAN_IF} =="
sudo nmcli -t -f NAME con show | grep -qx oneclient && sudo nmcli con delete oneclient || true
sudo nmcli con add type ethernet ifname "$LAN_IF" con-name oneclient \
  ipv4.addresses "$LAN_IP_CIDR" ipv4.method manual ipv6.method ignore
sudo nmcli con mod oneclient ipv4.never-default yes ipv6.never-default yes connection.autoconnect yes

echo "== Configure WAN ethernet (wan-enp2s0) on ${WAN_ETH_IF} =="
sudo nmcli -t -f NAME con show | grep -qx wan-enp2s0 && sudo nmcli con delete wan-enp2s0 || true
sudo nmcli con add type ethernet ifname "$WAN_ETH_IF" con-name wan-enp2s0 \
  ipv4.method auto ipv6.method ignore
sudo nmcli con mod wan-enp2s0 connection.autoconnect yes ipv4.route-metric 100 connection.autoconnect-priority 10

echo "== Ensure Wi-Fi profile exists and is fallback =="
if sudo nmcli -t -f NAME con show | grep -qx "$WIFI_CON"; then
  sudo nmcli con mod "$WIFI_CON" connection.autoconnect yes ipv4.route-metric 600 connection.autoconnect-priority -10
else
  echo "WARNING: Wi-Fi profile '$WIFI_CON' not found. Connect once, then rename the SSID profile to '$WIFI_CON'."
fi

echo "== dnsmasq: single-lease DHCP on LAN =="
sudo tee /etc/dnsmasq.d/oneclient-dhcp.conf >/dev/null <<DNS
port=0
interface=$LAN_IF
bind-dynamic
dhcp-authoritative
dhcp-range=$LAN_CLIENT_IP,$LAN_CLIENT_IP,$LAN_DHCP_NETMASK,$LAN_DHCP_LEASE
dhcp-option=option:router,${LAN_IP_CIDR%/*}
dhcp-option=option:dns-server,$LAN_DNS
DNS
sudo systemctl enable --now dnsmasq
sudo systemctl restart dnsmasq

echo "== nftables NAT for LAN subnet (dynamic upstream) =="
sudo mkdir -p /etc/nftables.d
sudo tee /etc/nftables.d/oneclient-nat.nft >/dev/null <<NFT
table ip oneclient_nat {
  chain postrouting {
    type nat hook postrouting priority srcnat;
    ip saddr $LAN_SUBNET oifname != "$LAN_IF" masquerade
  }
}
NFT
grep -q 'include "/etc/nftables.d/\*\.nft"' /etc/nftables.conf || echo 'include "/etc/nftables.d/*.nft"' | sudo tee -a /etc/nftables.conf >/dev/null
sudo systemctl enable --now nftables
sudo systemctl restart nftables

echo "== Optional: disable Wi-Fi power save (recommended for USB Wi-Fi) =="
sudo tee /etc/NetworkManager/conf.d/10-wifi-powersave.conf >/dev/null <<'CONF'
[connection]
wifi.powersave=2
CONF

echo "== Bring connections up =="
sudo nmcli con up oneclient
sudo nmcli con up wan-enp2s0 || true
sudo nmcli con up "$WIFI_CON" || true

echo "== Apply log growth limits (systemd journals & Docker) =="
SCRIPT_DIR="$(dirname "$0")"
if [ -f "$SCRIPT_DIR/../scripts/hardening/limit-logs.sh" ]; then
  sudo bash "$SCRIPT_DIR/../scripts/hardening/limit-logs.sh"
else
  echo "WARNING: Log limiting script not found at $SCRIPT_DIR/../scripts/hardening/limit-logs.sh"
fi

echo "== DONE =="
