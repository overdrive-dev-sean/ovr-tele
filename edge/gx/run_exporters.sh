#!/bin/sh
set -eu

INSTALL_DIR="/data/ovr/dbus2prom"
cd "${INSTALL_DIR}"

export PYTHONPATH="/opt/victronenergy/dbus-systemcalc-py/ext/velib_python:${PYTHONPATH:-}"

pkill -f "${INSTALL_DIR}/dbus2prom.py" 2>/dev/null || true
sleep 1

start_one() {
  name="$1"
  port="$2"
  map="$3"
  log="$4"
  pid="$5"

  if [ -f "$pid" ] && kill -0 "$(cat "$pid")" 2>/dev/null; then
    echo "$name already running (pid $(cat "$pid"))"
    return 0
  fi

  MAP_FILE="$map" LISTEN_PORT="$port" nohup python3 "${INSTALL_DIR}/dbus2prom.py" >"$log" 2>&1 &
  echo $! >"$pid"
  echo "started $name pid=$! port=$port"
}

start_one gx_fast 9480 "${INSTALL_DIR}/map_fast.tsv" "${INSTALL_DIR}/fast.log" "${INSTALL_DIR}/fast.pid"
start_one gx_slow 9481 "${INSTALL_DIR}/map_slow.tsv" "${INSTALL_DIR}/slow.log" "${INSTALL_DIR}/slow.pid"
