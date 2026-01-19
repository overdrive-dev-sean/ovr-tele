#!/usr/bin/env python3
"""
dbus2prom.py — minimal Victron D-Bus -> Prometheus exporter (scrape target for vmagent)

Runs on VenusOS / GX. Reads a map TSV and exposes only those metrics at /metrics.

Map TSV format (tab-separated), 7 columns:
  service    path    pretty    group    name    phase    device

Example run:
  export PYTHONPATH=/opt/victronenergy/dbus-systemcalc-py/ext/velib_python:$PYTHONPATH
  MAP_FILE=/data/dbus2prom/map_fast.tsv LISTEN_PORT=9480 ./dbus2prom.py

Notes:
- Uses VeDbusItemImport to subscribe to D-Bus updates (low overhead).
- HTTP response is just formatting cached values (scrape can be 250-500ms).
"""

import os
import re
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
import dbus

from vedbus import VeDbusItemImport


LABEL_KEYS = ("group", "name", "phase", "device")

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)).strip())
    except Exception:
        return default

def _env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def _sanitize_metric_name(s: str) -> str:
    s = s.strip()
    if not s:
        return "victron_metric"
    s = re.sub(r"[^a-zA-Z0-9_:]", "_", s)
    if not re.match(r"^[a-zA-Z_:]", s):
        s = "_" + s
    return s

def _escape_label_value(v: str) -> str:
    # Prometheus text format escaping
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

def _parse_map(map_file: str):
    rows = []
    with open(map_file, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                # ignore malformed row
                continue
            service, path, pretty, group, name, phase, device = [p.strip() for p in parts[:7]]
            if not service or not path or not pretty:
                continue
            if not path.startswith("/"):
                path = "/" + path
            rows.append({
                "service": service,
                "path": path,
                "metric": _sanitize_metric_name(pretty),
                "labels": {
                    "group": group,
                    "name": name,
                    "phase": phase,
                    "device": device,
                }
            })
    return rows

def _to_float(v):
    # Handle dbus types and strings
    if v is None:
        return None
    # dbus Boolean is int-ish but treat as 0/1
    if isinstance(v, (bool,)):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        # dbus.* types often stringify cleanly
        return float(str(v).strip())
    except Exception:
        return None

class Exporter:
    def __init__(self, map_rows, include_path_label=False, include_service_label=False):
        self.map_rows = map_rows
        self.include_path_label = include_path_label
        self.include_service_label = include_service_label

        self._lock = threading.Lock()
        self._latest = {}   # key=(service,path) -> float
        self._updated = {}  # key=(service,path) -> unix ts
        self._key_to_row = {}  # key -> row dict
        self._items = []

    def _on_change(self, service, path, changes):
        # veDbus callback signature: (serviceName, path, changes_dict)
        try:
            v = changes.get("Value", None)
        except Exception:
            v = None
        fv = _to_float(v)
        if fv is None:
            return
        key = (service, path)
        with self._lock:
            self._latest[key] = fv
            self._updated[key] = time.time()

    def start(self):
        DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()

        for r in self.map_rows:
            key = (r["service"], r["path"])
            self._key_to_row[key] = r
            try:
                it = VeDbusItemImport(bus, r["service"], r["path"], self._on_change)
                self._items.append(it)
                # seed initial value if available
                try:
                    fv = _to_float(it.get_value())
                    if fv is not None:
                        with self._lock:
                            self._latest[key] = fv
                            self._updated[key] = time.time()
                except Exception:
                    pass
            except Exception as e:
                # keep going even if a single path doesn't exist
                # (it may appear later)
                # print(f"WARN: couldn't import {r['service']} {r['path']}: {e}")
                pass

        self._loop = GLib.MainLoop()
        t = threading.Thread(target=self._loop.run, daemon=True)
        t.start()

    def render_metrics(self) -> str:
        now = time.time()
        lines = []
        lines.append("# HELP dbus2prom_uptime_seconds Exporter uptime in seconds")
        lines.append("# TYPE dbus2prom_uptime_seconds gauge")
        lines.append(f"dbus2prom_uptime_seconds {now - START_TS:.3f}")

        # Emit each mapped metric as a gauge
        with self._lock:
            items = list(self._key_to_row.items())
            latest = dict(self._latest)

        # Group by metric name to keep output stable-ish
        for key, row in items:
            metric = row["metric"]
            val = latest.get(key, None)
            if val is None:
                continue

            labels = dict(row["labels"])

            # omit '-' / empty labels to reduce noise
            for k in list(labels.keys()):
                if labels[k] is None:
                    labels.pop(k, None)
                    continue
                v = str(labels[k]).strip()
                if v == "" or v == "-" or v == "—":
                    labels.pop(k, None)
                else:
                    labels[k] = v

            if self.include_service_label:
                labels["service"] = row["service"]
            if self.include_path_label:
                labels["path"] = row["path"]

            if labels:
                lbl = ",".join([f'{k}="{_escape_label_value(str(v))}"' for k, v in labels.items()])
                lines.append(f"{metric}{{{lbl}}} {val:.6f}")
            else:
                lines.append(f"{metric} {val:.6f}")

        return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/-/ready", "/-/healthy"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return

        if self.path != "/metrics":
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found\n")
            return

        body = EXPORTER.render_metrics().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # quiet by default (set VERBOSE_HTTP=1 to enable)
        if _env_bool("VERBOSE_HTTP", False):
            super().log_message(fmt, *args)


def main():
    global START_TS, EXPORTER

    map_file = os.environ.get("MAP_FILE", "").strip()
    if not map_file:
        raise SystemExit("MAP_FILE is required")

    listen_addr = os.environ.get("LISTEN_ADDR", "0.0.0.0").strip()
    listen_port = _env_int("LISTEN_PORT", 9480)

    include_path_label = _env_bool("INCLUDE_PATH_LABEL", False)
    include_service_label = _env_bool("INCLUDE_SERVICE_LABEL", False)

    START_TS = time.time()
    rows = _parse_map(map_file)
    print(f"Loaded map rows: {len(rows)} from {map_file}")
    print(f"Listening on http://{listen_addr}:{listen_port}/metrics")

    EXPORTER = Exporter(rows, include_path_label=include_path_label, include_service_label=include_service_label)
    EXPORTER.start()

    httpd = HTTPServer((listen_addr, listen_port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
