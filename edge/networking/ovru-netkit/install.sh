#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${VARS_FILE:-$SCRIPT_DIR/vars.env}"

if [ ! -f "$VARS_FILE" ]; then
  echo "ERROR: vars file not found: $VARS_FILE" >&2
  exit 1
fi

echo "== Installing packages =="
sudo apt-get update
sudo apt-get install -y network-manager dnsmasq nftables procps curl

echo "== Configure ports =="
sudo PROMPT_ENABLE=0 VARS_FILE="$VARS_FILE" "$SCRIPT_DIR/configure-ports.sh"

echo "== Optional: disable Wi-Fi power save (recommended for USB Wi-Fi) =="
sudo tee /etc/NetworkManager/conf.d/10-wifi-powersave.conf >/dev/null <<'CONF'
[connection]
wifi.powersave=2
CONF

echo "== Apply log growth limits (systemd journals & Docker) =="
if [ -f "$SCRIPT_DIR/../scripts/hardening/limit-logs.sh" ]; then
  sudo bash "$SCRIPT_DIR/../scripts/hardening/limit-logs.sh"
else
  echo "WARNING: Log limiting script not found at $SCRIPT_DIR/../scripts/hardening/limit-logs.sh"
fi

echo "== DONE =="
