#!/bin/sh
# Restart dbus2prom exporters if they stop responding

STATUS=/data/dbus2prom/watchdog.status
LOG=/data/dbus2prom/watchdog.log

check() {
  PORT="$1"
  curl -m 1 -fsS "http://127.0.0.1:${PORT}/metrics" >/dev/null 2>&1
}

if check 9480 && check 9481; then
  echo "$(date) OK" > "$STATUS"
  exit 0
fi

echo "$(date) FAIL dbus2prom unresponsive, restarting..." >> "$LOG"
echo "$(date) FAIL" > "$STATUS"

pkill -f "/data/dbus2prom/dbus2prom.py" >/dev/null 2>&1
sh /data/dbus2prom/run_exporters.sh >> "$LOG" 2>&1

