#!/usr/bin/env bash
set -euo pipefail

### === EDIT THESE ===
LAN_IF="enp3s0"              # port to GX / laptop
WAN_IF="enp2s0"              # port to internet router (DHCP client)
WIFI_CON_NAME="wan-wifi"     # NetworkManager wifi connection profile name
### ==================

LAN_IP_CIDR="192.168.100.1/30"
LAN_SUBNET="192.168.100.0/30"
LAN_CLIENT_IP="192.168.100.2"
LAN_NETMASK="255.255.255.252"

echo "== Installing packages =="
apt-get update
apt-get install -y network-manager dnsmasq nftables procps

echo "== Enable IPv4 forwarding =="
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-ipforward.conf
sysctl --system >/dev/null

echo "== Configure LAN connection (oneclient) on $LAN_IF =="
nmcli -t -f NAME con show | grep -qx "oneclient" && nmcli con delete oneclient || true
nmcli con add type ethernet ifname "$LAN_IF" con-name oneclient \
  ipv4.addresses "$LAN_IP_CIDR" ipv4.method manual ipv6.method ignore
nmcli con mod oneclient ipv4.never-default yes ipv6.never-default yes
nmcli con mod oneclient connection.autoconnect yes

echo "== Configure WAN DHCP connection (wan-enp2s0) on $WAN_IF =="
nmcli -t -f NAME con show | grep -qx "wan-enp2s0" && nmcli con delete wan-enp2s0 || true
nmcli con add type ethernet ifname "$WAN_IF" con-name wan-enp2s0 \
  ipv4.method auto ipv6.method ignore
nmcli con mod wan-enp2s0 connection.autoconnect yes ipv4.route-metric 100 connection.autoconnect-priority 10

echo "== Configure Wi-Fi fallback metrics on '$WIFI_CON_NAME' (must already exist) =="
if nmcli -t -f NAME con show | grep -qx "$WIFI_CON_NAME"; then
  nmcli con mod "$WIFI_CON_NAME" connection.autoconnect yes ipv4.route-metric 600 connection.autoconnect-priority -10
else
  echo "WARNING: Wi-Fi connection profile '$WIFI_CON_NAME' not found."
  echo "Create it by connecting to Wi-Fi once, then rename it to '$WIFI_CON_NAME'."
fi

echo "== Configure dnsmasq DHCP on $LAN_IF (single lease) =="
cat > /etc/dnsmasq.d/oneclient-dhcp.conf <<DNS
port=0
interface=$LAN_IF
bind-interfaces
dhcp-range=$LAN_CLIENT_IP,$LAN_CLIENT_IP,$LAN_NETMASK,12h
dhcp-option=option:router,${LAN_IP_CIDR%/*}
dhcp-option=option:dns-server,1.1.1.1,8.8.8.8
DNS

systemctl enable --now dnsmasq
systemctl restart dnsmasq

echo "== Configure nftables NAT for $LAN_SUBNET (dynamic upstream) =="
mkdir -p /etc/nftables.d
cat > /etc/nftables.d/oneclient-nat.nft <<NFT
table ip oneclient_nat {
  chain postrouting {
    type nat hook postrouting priority srcnat;
    ip saddr $LAN_SUBNET oifname != "$LAN_IF" masquerade
  }
}
NFT

grep -q 'include "/etc/nftables.d/\*\.nft"' /etc/nftables.conf || \
  echo 'include "/etc/nftables.d/*.nft"' >> /etc/nftables.conf

systemctl enable --now nftables
systemctl restart nftables

echo "== Bring connections up =="
nmcli con up oneclient
nmcli con up wan-enp2s0 || true
[ -n "$WIFI_CON_NAME" ] && nmcli con up "$WIFI_CON_NAME" || true

echo
echo "== DONE. Status: =="
nmcli -t -f DEVICE,STATE,CONNECTION dev status || true
ip route get 1.1.1.1 || true
nft list ruleset | grep -n masquerade || true
echo "Forwarding: $(sysctl -n net.ipv4.ip_forward)"
