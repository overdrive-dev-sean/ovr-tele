#!/bin/sh
export PYTHONPATH=/opt/victronenergy/dbus-systemcalc-py/ext/velib_python:$PYTHONPATH
export MAP_FILE=/data/dbus2prom/map_slow.tsv
export LISTEN_ADDR=0.0.0.0
export LISTEN_PORT=9481
exec python3 /data/dbus2prom/dbus2prom.py

