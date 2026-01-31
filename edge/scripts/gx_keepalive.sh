#!/usr/bin/env bash
# Send MQTT keepalives to all discovered GX systems
# Run via systemd timer or cron every 30 seconds

set -uo pipefail

GX_SYSTEMS_FILE="${GX_SYSTEMS_FILE:-/etc/ovr/gx_systems.json}"

if [ ! -f "$GX_SYSTEMS_FILE" ]; then
  echo "No gx_systems.json found" >&2
  exit 0
fi

if ! command -v mosquitto_pub >/dev/null 2>&1; then
  echo "mosquitto_pub not installed" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq not installed" >&2
  exit 1
fi

# Read systems and send keepalives
jq -r '.systems[] | "\(.ip)|\(.portal_id)"' "$GX_SYSTEMS_FILE" 2>/dev/null | while IFS='|' read -r ip portal_id; do
  if [ -n "$ip" ] && [ -n "$portal_id" ]; then
    if mosquitto_pub -h "$ip" -t "R/$portal_id/keepalive" -m '' 2>/dev/null; then
      echo "OK $portal_id ($ip)"
    else
      echo "FAIL $portal_id ($ip)" >&2
    fi
  fi
done
