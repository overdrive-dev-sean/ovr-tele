#!/bin/sh
export PYTHONPATH=/opt/victronenergy/dbus-systemcalc-py/ext/velib_python:$PYTHONPATH
export MAP_FILE=/data/dbus2prom/map_fast.tsv
export LISTEN_ADDR=0.0.0.0
export LISTEN_PORT=9480
exec python3 /data/dbus2prom/dbus2prom.py




