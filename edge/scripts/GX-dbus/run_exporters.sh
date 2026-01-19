#!/bin/sh
set -eu

cd /data/dbus2prom

# Victron python libs (VeDbusItemImport)
export PYTHONPATH="/opt/victronenergy/dbus-systemcalc-py/ext/velib_python:${PYTHONPATH:-}"

pkill -f "/data/dbus2prom/dbus2prom.py" 2>/dev/null || true
sleep 1

start_one() {
  name="$1"
  port="$2"
  map="$3"
  log="$4"
  pid="$5"

  # If pidfile exists and process alive, do nothing
  if [ -f "$pid" ] && kill -0 "$(cat "$pid")" 2>/dev/null; then
    echo "$name already running (pid $(cat "$pid"))"
    return 0
  fi

  # Start with environment variables
  MAP_FILE="$map" LISTEN_PORT="$port" nohup python3 /data/dbus2prom/dbus2prom.py >"$log" 2>&1 &

  echo $! >"$pid"
  echo "started $name pid=$! port=$port"
}

start_one gx_fast 9480 /data/dbus2prom/map_fast.tsv /data/dbus2prom/fast.log /data/dbus2prom/fast.pid
start_one gx_slow 9481 /data/dbus2prom/map_slow.tsv /data/dbus2prom/slow.log /data/dbus2prom/slow.pid
