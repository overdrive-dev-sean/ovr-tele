#!/bin/sh
cd /data/dbus2prom || exit 1
export PYTHONPATH=/opt/victronenergy/dbus-systemcalc-py/ext/velib_python:$PYTHONPATH
export MAP_FILE=/data/dbus2prom/map_fast.tsv
export LISTEN_ADDR=0.0.0.0
export LISTEN_PORT=9480

while true; do
  echo "fast: starting"
  python3 /data/dbus2prom/dbus2prom.py
  rc=$?
  echo "fast: exited rc=$rc; restarting in 2s"
  sleep 2
done



