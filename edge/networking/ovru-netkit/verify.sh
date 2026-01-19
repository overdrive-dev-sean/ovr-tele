#!/usr/bin/env bash
set -e
echo "=== NM status ==="
nmcli -t -f DEVICE,STATE,CONNECTION dev status || true
echo
echo "=== Default route path ==="
ip route get 1.1.1.1 || true
echo
echo "=== resolv.conf ==="
cat /etc/resolv.conf || true
echo
echo "=== dnsmasq ==="
systemctl is-active dnsmasq || true
sudo cat /var/lib/misc/dnsmasq.leases 2>/dev/null || true
echo
echo "=== nftables masquerade ==="
sudo nft list ruleset | grep -n masquerade || true
echo
echo "=== forwarding ==="
sysctl net.ipv4.ip_forward || true
echo
echo "=== cloudflared ==="
systemctl is-active cloudflared 2>/dev/null || true
