#!/usr/bin/env python3
"""
OVR Event + Location Marker Service

Lightweight HTTP service that writes event windows and location changes
to VictoriaMetrics as dedicated time series (no cardinality explosion).

Data Model in VM (Influx line protocol):
1) ovr_event,event_id=X,system_id=Y,location=Z active=1|0 <ts_ns>
   - event_id: Event identifier (required)
   - system_id: Logger/device identifier (required)
   - location: GPS or manual location (optional, use '-' if empty)
   - active: 1=logger active on event, 0=logger removed/event ended
2) ovr_event_note,system_id=X,event_id=Y text="..." <ts_ns> (future)
"""

import os
import re
import time
import json
import math
import sqlite3
import logging
import hashlib
import threading
from datetime import datetime
from typing import Optional, Dict, Any, List
from contextlib import contextmanager
from werkzeug.utils import secure_filename
from urllib.parse import quote

from map_tiles import (
    PROVIDERS as MAP_TILE_PROVIDERS,
    utc_month_key,
    init_map_tables,
    increment_tile_count,
    get_tile_counts,
    get_tile_sync_totals,
    set_tile_sync_total,
    set_preferred_provider,
    get_preferred_provider,
    build_guardrail_status,
    is_valid_provider,
)

import requests
from flask import Flask, request, jsonify, render_template_string, send_from_directory, make_response

try:
    import paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False

# Configuration from environment
VM_WRITE_URL = os.environ.get("VM_WRITE_URL", "http://victoria-metrics:8428/write")
VM_WRITE_URL_SECONDARY = os.environ.get("VM_WRITE_URL_SECONDARY", "").strip()
VM_QUERY_URL = os.environ.get("VM_QUERY_URL", "http://victoria-metrics:8428")
VM_WRITE_USERNAME = os.environ.get("VM_WRITE_USERNAME", "").strip()
VM_WRITE_PASSWORD = os.environ.get("VM_WRITE_PASSWORD", "")
VM_WRITE_PASSWORD_FILE = os.environ.get("VM_WRITE_PASSWORD_FILE", "").strip()
DB_PATH = os.environ.get("DB_PATH", "/data/events.db")
IMAGES_PATH = os.environ.get("IMAGES_PATH", "/data/images")
REPORTS_PATH = os.environ.get("REPORTS_PATH", "/data/reports")
REPORT_UPLOAD_URL = os.environ.get("REPORT_UPLOAD_URL", "").strip()
REPORT_UPLOAD_TOKEN = os.environ.get("REPORT_UPLOAD_TOKEN", "").strip()
REPORT_UPLOAD_TIMEOUT = float(os.environ.get("REPORT_UPLOAD_TIMEOUT", "8"))
REPORT_UPLOAD_RETRY_INTERVAL = int(os.environ.get("REPORT_UPLOAD_RETRY_INTERVAL", "120"))
REPORT_UPLOAD_BACKOFF_BASE = int(os.environ.get("REPORT_UPLOAD_BACKOFF_BASE", "30"))
REPORT_UPLOAD_BACKOFF_MAX = int(os.environ.get("REPORT_UPLOAD_BACKOFF_MAX", "3600"))
API_KEY = os.environ.get("EVENT_API_KEY", os.environ.get("API_KEY", ""))  # Optional simple API key
API_KEY_FILE = os.environ.get("EVENT_API_KEY_FILE", os.environ.get("API_KEY_FILE", "")).strip()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# Map configuration
MAP_TILE_URL = os.environ.get(
    "MAP_TILE_URL",
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
MAP_TILE_ATTRIBUTION = os.environ.get("MAP_TILE_ATTRIBUTION", "Tiles (c) Esri")
MAP_DEFAULT_ZOOM = int(os.environ.get("MAP_DEFAULT_ZOOM", "16"))
MAP_TILE_CACHE_PREFIX = os.environ.get("MAP_TILE_CACHE_PREFIX", "").strip()
MAP_DEFAULT_PROVIDER = os.environ.get("MAP_DEFAULT_PROVIDER", "esri").strip().lower()
MAPBOX_TOKEN = os.environ.get("MAPBOX_TOKEN", "").strip()
MAPBOX_TOKEN_FILE = os.environ.get("MAPBOX_TOKEN_FILE", "").strip()
ESRI_TOKEN = os.environ.get("ESRI_TOKEN", "").strip()
MAPBOX_TILE_URL_TEMPLATE = os.environ.get(
    "MAPBOX_TILE_URL",
    "https://api.mapbox.com/styles/v1/mapbox/satellite-v9/tiles/256/{z}/{x}/{y}?access_token={token}",
)
ESRI_TILE_URL = os.environ.get("ESRI_TILE_URL", MAP_TILE_URL)
MAPBOX_ATTRIBUTION = os.environ.get("MAPBOX_ATTRIBUTION", "Imagery (c) Mapbox")
ESRI_ATTRIBUTION = os.environ.get("ESRI_ATTRIBUTION", MAP_TILE_ATTRIBUTION)
MAPBOX_MAX_ZOOM = int(os.environ.get("MAPBOX_MAX_ZOOM", "20"))
ESRI_MAX_ZOOM = int(os.environ.get("ESRI_MAX_ZOOM", "19"))

def canonicalize_system_id(system_id: str) -> str:
    if not system_id:
        return system_id
    trimmed = system_id.strip()
    if trimmed == "Pro6005.2":
        return "Pro6005-2"
    return trimmed

# System identification
NODE_ID = os.environ.get("NODE_ID", "").strip()
SYSTEM_ID_RAW = os.environ.get("SYSTEM_ID", "").strip()
SYSTEM_ID = canonicalize_system_id(SYSTEM_ID_RAW or NODE_ID)
DEPLOYMENT_ID = os.environ.get("DEPLOYMENT_ID", "").strip()
TEMP_EVENT_PREFIX = "temp-"

# Map usage guardrails
MAPBOX_FREE_TILES_PER_MONTH = int(os.environ.get("MAPBOX_FREE_TILES_PER_MONTH", "750000"))
ESRI_FREE_TILES_PER_MONTH = int(os.environ.get("ESRI_FREE_TILES_PER_MONTH", "2000000"))
GUARDRAIL_LIMIT_PCT = float(os.environ.get("GUARDRAIL_LIMIT_PCT", "0.95"))

# Cloud fleet API (optional)
CLOUD_API_URL = os.environ.get("CLOUD_API_URL", os.environ.get("CLOUD_BASE_URL", "")).strip()
CLOUD_API_TIMEOUT = float(os.environ.get("CLOUD_API_TIMEOUT", "4"))
TILE_USAGE_SYNC_INTERVAL = int(os.environ.get("TILE_USAGE_SYNC_INTERVAL", "60"))

# GX Device SSH configuration
GX_HOST = os.environ.get("GX_HOST", "").strip()
GX_USER = os.environ.get("GX_USER", "root")
GX_PASSWORD = os.environ.get("GX_PASSWORD", "").strip()
GX_PASSWORD_FILE = os.environ.get("GX_PASSWORD_FILE", "").strip()

# Image upload settings
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'heic', 'webp'}
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5  # seconds

# Heartbeat configuration
HEARTBEAT_INTERVAL = 2  # seconds - how often to write active events/locations to VM

# Setup logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

if not VM_WRITE_PASSWORD and VM_WRITE_PASSWORD_FILE:
    try:
        with open(VM_WRITE_PASSWORD_FILE, "r") as handle:
            VM_WRITE_PASSWORD = handle.read().strip()
    except Exception as exc:
        logger.warning(f"Failed to read VM write password file: {exc}")

if not API_KEY and API_KEY_FILE:
    try:
        with open(API_KEY_FILE, "r") as handle:
            API_KEY = handle.read().strip()
    except Exception as exc:
        logger.warning(f"Failed to read API key file: {exc}")

if not MAPBOX_TOKEN and MAPBOX_TOKEN_FILE:
    try:
        with open(MAPBOX_TOKEN_FILE, "r") as handle:
            MAPBOX_TOKEN = handle.read().strip()
    except Exception as exc:
        logger.warning(f"Failed to read Mapbox token file: {exc}")

if not GX_PASSWORD and GX_PASSWORD_FILE:
    try:
        with open(GX_PASSWORD_FILE, "r") as handle:
            GX_PASSWORD = handle.read().strip()
    except Exception as exc:
        logger.warning(f"Failed to read GX password file: {exc}")

# Log paramiko availability
if not PARAMIKO_AVAILABLE:
    logger.warning("paramiko not installed - GX device control will not work. Install with: pip install paramiko")

app = Flask(__name__)

# Global flag for heartbeat thread
_heartbeat_stop = threading.Event()


# ============================================================================
# Map Tile Providers
# ============================================================================

def _append_query_param(url: str, key: str, value: str) -> str:
    if not value:
        return url
    if f"{key}=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{key}={value}"


def _mapbox_tile_url() -> str:
    url = MAPBOX_TILE_URL_TEMPLATE
    if "{token}" in url:
        url = url.replace("{token}", MAPBOX_TOKEN)
    if "{access_token}" in url:
        url = url.replace("{access_token}", MAPBOX_TOKEN)
    if "{token}" not in MAPBOX_TILE_URL_TEMPLATE and "{access_token}" not in MAPBOX_TILE_URL_TEMPLATE:
        url = _append_query_param(url, "access_token", MAPBOX_TOKEN)
    return url


def _esri_tile_url() -> str:
    return _append_query_param(ESRI_TILE_URL, "token", ESRI_TOKEN)


def _tile_prefix(url: str) -> str:
    return url.split("{", 1)[0] if "{" in url else url


def _map_tile_providers() -> Dict[str, Dict[str, Any]]:
    return {
        "mapbox": {
            "id": "mapbox",
            "label": "Mapbox Satellite",
            "url": _mapbox_tile_url(),
            "attribution": MAPBOX_ATTRIBUTION,
            "maxZoom": MAPBOX_MAX_ZOOM,
            "tileSize": 256,
            "zoomOffset": 0,
        },
        "esri": {
            "id": "esri",
            "label": "Esri World Imagery",
            "url": _esri_tile_url(),
            "attribution": ESRI_ATTRIBUTION,
            "maxZoom": ESRI_MAX_ZOOM,
            "tileSize": 256,
            "zoomOffset": 0,
        },
    }


MAP_TILE_PROVIDERS_CONFIG = _map_tile_providers()
MAP_TILE_CACHE_PREFIXES = [
    prefix
    for prefix in {
        *(filter(None, [MAP_TILE_CACHE_PREFIX])),
        _tile_prefix(MAP_TILE_PROVIDERS_CONFIG["mapbox"]["url"]),
        _tile_prefix(MAP_TILE_PROVIDERS_CONFIG["esri"]["url"]),
    }
    if prefix.startswith(("http://", "https://")) and len(prefix) > 10
]


# ============================================================================
# Influx Line Protocol Escaping
# ============================================================================

def escape_tag_value(s: str) -> str:
    """Escape tag values: commas, equals, spaces -> backslash-escaped."""
    if not s:
        return ""
    s = str(s)
    s = s.replace("\\", "\\\\")  # backslash first
    s = s.replace(",", "\\,")
    s = s.replace("=", "\\=")
    s = s.replace(" ", "\\ ")
    return s


def escape_field_string(s: str) -> str:
    """Escape field string values: quotes and backslashes."""
    if not s:
        return '""'
    s = str(s)
    s = s.replace("\\", "\\\\")  # backslash first
    s = s.replace('"', '\\"')
    return f'"{s}"'


def escape_measurement(s: str) -> str:
    """Escape measurement names: commas and spaces."""
    if not s:
        return "metric"
    s = str(s)
    s = s.replace("\\", "\\\\")
    s = s.replace(",", "\\,")
    s = s.replace(" ", "\\ ")
    return s


def build_event_line(event_id: str, system_id: str, location: Optional[str], active: int, ts_ns: int) -> str:
    """Build unified ovr_event line with optional node/deployment tags."""
    tags = [
        f"event_id={escape_tag_value(event_id)}",
        f"system_id={escape_tag_value(system_id)}",
        f"location={escape_tag_value(location or '-')}",
    ]
    if NODE_ID:
        tags.append(f"node_id={escape_tag_value(NODE_ID)}")
    if DEPLOYMENT_ID:
        tags.append(f"deployment_id={escape_tag_value(DEPLOYMENT_ID)}")
    return f"{escape_measurement('ovr_event')}," + ",".join(tags) + f" active={int(active)}i {ts_ns}"


def _safe_event_fragment(value: str) -> str:
    if not value:
        return ""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")


def _is_temp_event_id(event_id: str) -> bool:
    return bool(event_id and event_id.startswith(TEMP_EVENT_PREFIX))


def _generate_temp_event_id(system_id: str) -> str:
    fragment = _safe_event_fragment(NODE_ID or system_id or SYSTEM_ID or "node")
    ts = int(time.time())
    if not fragment:
        fragment = "node"
    return f"{TEMP_EVENT_PREFIX}{fragment}-{ts}"


# ============================================================================
# VictoriaMetrics Writer with Retry
# ============================================================================

def _vm_write_auth() -> Optional[tuple[str, str]]:
    if VM_WRITE_USERNAME and VM_WRITE_PASSWORD:
        return (VM_WRITE_USERNAME, VM_WRITE_PASSWORD)
    return None


def _write_to_vm_url(url: str, lines: List[str]) -> tuple[bool, str]:
    """
    Write Influx line protocol lines to a single VictoriaMetrics endpoint.
    Returns (success: bool, error_message: str)
    """
    if not url:
        return True, ""

    payload = "\n".join(lines)
    logger.debug(f"Writing to VM {url}:\n{payload}")
    auth = _vm_write_auth()

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(
                url,
                data=payload,
                headers={"Content-Type": "text/plain"},
                timeout=5,
                auth=auth,
            )
            if resp.status_code in (200, 204):
                logger.info(f"Wrote {len(lines)} lines to VM ({url}) successfully")
                return True, ""
            err_msg = f"VM returned {resp.status_code}: {resp.text[:200]}"
            logger.warning(f"Attempt {attempt+1}/{MAX_RETRIES} failed: {err_msg}")
        except Exception as e:
            err_msg = f"Exception writing to VM: {e}"
            logger.warning(f"Attempt {attempt+1}/{MAX_RETRIES} failed: {err_msg}")

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
        else:
            return False, err_msg

    return False, "Max retries exceeded"


def write_to_vm(lines: List[str]) -> tuple[bool, str]:
    """
    Write Influx line protocol lines to VictoriaMetrics.
    Returns (success: bool, error_message: str)
    """
    if not lines:
        return True, ""

    success, error = _write_to_vm_url(VM_WRITE_URL, lines)
    if not success:
        return False, error

    if VM_WRITE_URL_SECONDARY and VM_WRITE_URL_SECONDARY != VM_WRITE_URL:
        secondary_success, secondary_error = _write_to_vm_url(VM_WRITE_URL_SECONDARY, lines)
        if not secondary_success:
            logger.warning(f"Secondary VM write failed: {secondary_error}")

    return True, ""


# ============================================================================
# VictoriaMetrics Reader
# ============================================================================

def escape_prom_label_value(value: str) -> str:
    """Escape label values for PromQL queries."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _vm_query(query: str) -> Optional[Dict[str, Any]]:
    url = f"{VM_QUERY_URL.rstrip('/')}/api/v1/query"
    try:
        resp = requests.get(url, params={"query": query}, timeout=5)
        if resp.status_code != 200:
            logger.warning(f"VM query failed ({resp.status_code}): {resp.text[:200]}")
            return None
        payload = resp.json()
        if payload.get("status") != "success":
            logger.warning(f"VM query error: {payload}")
            return None
        return payload.get("data")
    except Exception as e:
        logger.warning(f"VM query exception: {e}")
        return None


def _vm_value_to_float(value: Any) -> Optional[float]:
    if isinstance(value, list) and len(value) >= 2:
        try:
            return float(value[1])
        except Exception:
            return None
    return None


def vm_query_scalar(query: str) -> Optional[float]:
    data = _vm_query(query)
    if not data or data.get("resultType") != "vector":
        return None
    results = data.get("result") or []
    if not results:
        return None
    return _vm_value_to_float(results[0].get("value"))


def vm_query_vector(query: str) -> List[Dict[str, Any]]:
    data = _vm_query(query)
    if not data or data.get("resultType") != "vector":
        return []
    return data.get("result") or []


def vm_query_range(query: str, start_time: int, end_time: int, step: str = "30s") -> Optional[Dict[str, Any]]:
    """Query VictoriaMetrics with time range for range vectors."""
    url = f"{VM_QUERY_URL.rstrip('/')}/api/v1/query_range"
    try:
        resp = requests.get(url, params={
            "query": query,
            "start": start_time / 1e9,  # Convert nanoseconds to seconds
            "end": end_time / 1e9,
            "step": step
        }, timeout=30)
        if resp.status_code != 200:
            logger.warning(f"VM range query failed ({resp.status_code}): {resp.text[:200]}")
            return None
        payload = resp.json()
        if payload.get("status") != "success":
            logger.warning(f"VM range query error: {payload}")
            return None
        return payload.get("data")
    except Exception as e:
        logger.warning(f"VM range query exception: {e}")
        return None


def vm_integrate_metric(query: str, start_time: int, end_time: int) -> float:
    """Integrate a metric over time using trapezoidal rule. Returns energy in Wh."""
    data = vm_query_range(query, start_time, end_time, step="30s")
    if not data or data.get("resultType") != "matrix":
        return 0.0
    
    results = data.get("result") or []
    if not results:
        return 0.0
    
    # Take first series (should be only one for non-aggregated query)
    values = results[0].get("values", [])
    if len(values) < 2:
        return 0.0
    
    # Trapezoidal integration: sum of (time_delta * avg_value)
    total_wh = 0.0
    for i in range(1, len(values)):
        t1, v1 = values[i-1][0], float(values[i-1][1])
        t2, v2 = values[i][0], float(values[i][1])
        dt_hours = (t2 - t1) / 3600.0  # Convert seconds to hours
        avg_value = (v1 + v2) / 2.0
        total_wh += avg_value * dt_hours
    
    return total_wh


def vm_integrate_product(metric1: str, metric2: str, start_time: int, end_time: int) -> float:
    """Integrate the product of two metrics (e.g., voltage * current). Returns energy in VAh."""
    # Query both metrics as range vectors
    data1 = vm_query_range(metric1, start_time, end_time, step="30s")
    data2 = vm_query_range(metric2, start_time, end_time, step="30s")
    
    if not data1 or not data2:
        return 0.0
    
    results1 = data1.get("result", [])
    results2 = data2.get("result", [])
    
    if not results1 or not results2:
        return 0.0
    
    values1 = results1[0].get("values", [])
    values2 = results2[0].get("values", [])
    
    if len(values1) != len(values2) or len(values1) < 2:
        return 0.0
    
    # Integrate product using trapezoidal rule
    total_vah = 0.0
    for i in range(1, len(values1)):
        t1, v1_m1 = values1[i-1][0], float(values1[i-1][1])
        t2, v2_m1 = values1[i][0], float(values1[i][1])
        _, v1_m2 = values2[i-1][0], float(values2[i-1][1])
        _, v2_m2 = values2[i][0], float(values2[i][1])
        
        dt_hours = (t2 - t1) / 3600.0
        product1 = v1_m1 * v1_m2
        product2 = v2_m1 * v2_m2
        avg_product = (product1 + product2) / 2.0
        total_vah += avg_product * dt_hours
    
    return total_vah


def vm_query_avg(query: str, start_time: int, end_time: int) -> Optional[float]:
    """Calculate average value of a metric over time range."""
    avg_query = f"avg_over_time({query}[{int((end_time - start_time) / 1e9)}s])"
    data = _vm_query(avg_query)
    if not data or data.get("resultType") != "vector":
        return None
    results = data.get("result") or []
    if not results:
        return None
    return _vm_value_to_float(results[0].get("value"))


def vm_metric_exists(query: str, start_time: int, end_time: int) -> bool:
    """Check if a metric exists in the given time range."""
    data = vm_query_range(query, start_time, end_time, step="5m")
    if not data:
        return False
    results = data.get("result") or []
    return len(results) > 0


def vm_has_nonzero_data(query: str, start_time: int, end_time: int, threshold: float = 1.0) -> bool:
    """Check if metric exists and has non-zero values above threshold."""
    avg_val = vm_query_avg(query, start_time, end_time)
    return avg_val is not None and abs(avg_val) > threshold


# ============================================================================
# Report Generation - Device Detection
# ============================================================================

def detect_voltage_level(avg_voltage: float) -> int:
    """Classify voltage into standard nominal levels."""
    if avg_voltage < 140:
        return 120
    elif avg_voltage < 260:
        return 240
    elif avg_voltage < 300:
        return 277  # 480V 3-phase line-to-neutral
    elif avg_voltage < 520:
        return 480
    else:
        return int(round(avg_voltage / 10) * 10)  # Round to nearest 10V


def normalize_system_id_for_query(system_id: str) -> list:
    """Return list of system_id variants to try."""
    canonical = canonicalize_system_id(system_id)
    variants = [canonical]
    if canonical != system_id:
        variants.append(system_id)

    # Try swapping . and - for hostname/service name mismatches
    for value in list(variants):
        if "." in value:
            variants.append(value.replace(".", "-"))
        elif "-" in value:
            variants.append(value.replace("-", "."))

    # Handle Logger X -> acuvim_1X mapping (Logger # = last digit of IP)
    # e.g., "Logger 0" -> IP x.x.x.10 -> device="acuvim_10"
    if canonical.startswith("Logger "):
        try:
            logger_num = canonical.split()[1]  # Extract "0" from "Logger 0"
            variants.append(f"acuvim_1{logger_num}")  # Map to acuvim_10, acuvim_13, etc.
        except (IndexError, ValueError):
            pass

    deduped = []
    seen = set()
    for value in variants:
        if value and value not in seen:
            deduped.append(value)
            seen.add(value)

    return deduped


def detect_device_configuration(system_id: str, start_time: int, end_time: int) -> Dict[str, Any]:
    """Auto-detect device type, phase config, voltage, and available metrics."""
    
    config = {
        "system_id": system_id,
        "source": None,           # "victron" or "acuvim"
        "device_model": None,     # "pro6000", "pro600", "acuvim2r"
        "phase_config": None,     # "3phase", "split_phase", "single_phase"
        "phases": [],             # ["L1", "L2", "L3"] or ["L1", "L2"] or ["L1"]
        "voltage_nominal": None,  # Auto-detected: 120, 240, 480, etc.
        "has_reactive_power": False,
        "has_apparent_power": False,
        "detection_confidence": "unknown"
    }
    
    # Try all system_id variants.
    system_id_variants = normalize_system_id_for_query(system_id)
    actual_system_id = None
    
    # Try Victron first (Pro6000/Pro600) - test all variants
    for sid in system_id_variants:
        victron_query = f'victron_ac_out_power{{system_id="{escape_prom_label_value(sid)}"}}'
        if vm_metric_exists(victron_query, start_time, end_time):
            actual_system_id = sid
            break
    
    if actual_system_id:
        config["source"] = "victron"
        config["detection_confidence"] = "high"
        config["system_id"] = actual_system_id  # Use the matched variant for queries
        
        # Check which phases exist and have data (use actual_system_id that was found)
        l1_query = f'victron_ac_out_l1_p{{system_id="{escape_prom_label_value(actual_system_id)}"}}'
        l2_query = f'victron_ac_out_l2_p{{system_id="{escape_prom_label_value(actual_system_id)}"}}'
        l3_query = f'victron_ac_out_l3_p{{system_id="{escape_prom_label_value(actual_system_id)}"}}'
        
        l1_exists = vm_has_nonzero_data(l1_query, start_time, end_time, threshold=10.0)
        l2_exists = vm_has_nonzero_data(l2_query, start_time, end_time, threshold=10.0)
        l3_exists = vm_has_nonzero_data(l3_query, start_time, end_time, threshold=10.0)
        
        # Detect phase configuration
        if l1_exists and l2_exists and l3_exists:
            config["phase_config"] = "3phase"
            config["phases"] = ["L1", "L2", "L3"]
            config["device_model"] = "pro6000"
        elif l1_exists and l2_exists:
            config["phase_config"] = "split_phase"
            config["phases"] = ["L1", "L2"]
            config["device_model"] = "pro6000_or_pro600"
        elif l1_exists:
            config["phase_config"] = "single_phase"
            config["phases"] = ["L1"]
            config["device_model"] = "pro600"
        else:
            config["detection_confidence"] = "low"
            logger.warning(f"Victron detected for {system_id} but no phase data found")
        
        # Detect nominal voltage
        if l1_exists:
            v_query = f'victron_ac_out_l1_v{{system_id="{escape_prom_label_value(actual_system_id)}"}}'
            avg_voltage = vm_query_avg(v_query, start_time, end_time)
            if avg_voltage:
                config["voltage_nominal"] = detect_voltage_level(avg_voltage)
        
        # Check for apparent power metric
        s_query = f'victron_ac_out_apparent{{system_id="{escape_prom_label_value(actual_system_id)}"}}'
        config["has_apparent_power"] = vm_metric_exists(s_query, start_time, end_time)
        config["has_reactive_power"] = False  # Victron doesn't expose Q
        
        return config
    
    # Try Acuvim (power meters) - also try variants
    for sid in system_id_variants:
        acuvim_query = f'acuvim_P{{device=~".*{sid}.*"}}'
        if vm_metric_exists(acuvim_query, start_time, end_time):
            actual_system_id = sid
            break
    
    if actual_system_id:
        config["source"] = "acuvim"
        config["device_model"] = "acuvim2r"
        config["detection_confidence"] = "high"
        config["system_id"] = actual_system_id  # Use the matched variant
        
        # Acuvim always reports all phases, detect which are active
        va_query = f'acuvim_Va{{device=~".*{actual_system_id}.*"}}'
        vb_query = f'acuvim_Vb{{device=~".*{actual_system_id}.*"}}'
        vc_query = f'acuvim_Vc{{device=~".*{actual_system_id}.*"}}'
        
        va_exists = vm_has_nonzero_data(va_query, start_time, end_time, threshold=20.0)
        vb_exists = vm_has_nonzero_data(vb_query, start_time, end_time, threshold=20.0)
        vc_exists = vm_has_nonzero_data(vc_query, start_time, end_time, threshold=20.0)
        
        # Detect configuration
        if va_exists and vb_exists and vc_exists:
            # Check if it's true 3-phase or split-phase (Vc would be ~0 for split)
            avg_vc = vm_query_avg(vc_query, start_time, end_time)
            if avg_vc and avg_vc > 20:  # Threshold: >20V means real phase
                config["phase_config"] = "3phase"
                config["phases"] = ["A", "B", "C"]
            else:
                config["phase_config"] = "split_phase"
                config["phases"] = ["A", "B"]
        elif va_exists and vb_exists:
            config["phase_config"] = "split_phase"
            config["phases"] = ["A", "B"]
        elif va_exists:
            config["phase_config"] = "single_phase"
            config["phases"] = ["A"]
        else:
            config["detection_confidence"] = "low"
            logger.warning(f"Acuvim detected for {system_id} but no phase data found")
        
        # Detect voltage level
        if config["phase_config"] == "3phase":
            vll_query = f'acuvim_Vll{{device=~".*{actual_system_id}.*"}}'
            avg_vll = vm_query_avg(vll_query, start_time, end_time)
            if avg_vll:
                config["voltage_nominal"] = detect_voltage_level(avg_vll)
        else:
            vln_query = f'acuvim_Vln{{device=~".*{actual_system_id}.*"}}'
            avg_vln = vm_query_avg(vln_query, start_time, end_time)
            if avg_vln:
                config["voltage_nominal"] = detect_voltage_level(avg_vln)
        
        # Acuvim has P and Q, so we can calculate S
        config["has_reactive_power"] = True
        config["has_apparent_power"] = True  # Can calculate from P and Q
        
        return config
    
    # No known metrics found
    config["detection_confidence"] = "none"
    logger.warning(f"No known metrics found for system_id={system_id} in time range")
    return config


# ============================================================================
# Report Generation - Energy Calculations
# ============================================================================

def calculate_victron_energy(config: Dict[str, Any], start_time: int, end_time: int) -> Dict[str, Any]:
    """Calculate energy metrics for Victron devices."""
    system_id = escape_prom_label_value(config["system_id"])
    methods = {}
    
    # Method 1: Total power integration (always available)
    total_p_query = f'victron_ac_out_power{{system_id="{system_id}"}}'
    methods["total_power_wh"] = {
        "value": vm_integrate_metric(total_p_query, start_time, end_time),
        "description": "Real energy consumed - integration of total real power (W) over event duration",
        "metric": "victron_ac_out_power",
        "includes_reactive": False
    }
    
    # Method 2: Apparent power integration (if available)
    if config["has_apparent_power"]:
        total_s_query = f'victron_ac_out_apparent{{system_id="{system_id}"}}'
        methods["apparent_power_vah"] = {
            "value": vm_integrate_metric(total_s_query, start_time, end_time),
            "description": "Apparent energy - integration of total apparent power (VA) including reactive component",
            "metric": "victron_ac_out_apparent",
            "includes_reactive": True
        }
    
    # Method 3: Sum of per-phase power (if multi-phase)
    if len(config["phases"]) > 1:
        phase_energies = {}
        total_phase_sum = 0.0
        for phase in config["phases"]:
            phase_query = f'victron_ac_out_{phase.lower()}_p{{system_id="{system_id}"}}'
            energy = vm_integrate_metric(phase_query, start_time, end_time)
            phase_energies[phase] = energy
            total_phase_sum += energy
        
        methods["sum_of_phase_power_wh"] = {
            "value": total_phase_sum,
            "per_phase": phase_energies,
            "description": f"Real energy (sum of {len(config['phases'])} individual phase measurements)",
            "metric": "sum(victron_ac_out_lX_p)",
            "includes_reactive": False
        }
    
    # Method 4: V*I integration per phase
    iv_total = 0.0
    iv_per_phase = {}
    for phase in config["phases"]:
        v_query = f'victron_ac_out_{phase.lower()}_v{{system_id="{system_id}"}}'
        i_query = f'victron_ac_out_{phase.lower()}_i{{system_id="{system_id}"}}'
        energy = vm_integrate_product(v_query, i_query, start_time, end_time)
        iv_per_phase[phase] = energy
        iv_total += energy
    
    methods["integrated_iv_vah"] = {
        "value": iv_total,
        "per_phase": iv_per_phase,
        "description": "Apparent energy - integration of instantaneous V*I product per phase",
        "metric": "V*I per phase",
        "includes_reactive": True
    }
    
    # Calculate average power factor if we have both real and apparent
    if "apparent_power_vah" in methods and methods["apparent_power_vah"]["value"] > 0:
        real_energy = methods["total_power_wh"]["value"]
        apparent_energy = methods["apparent_power_vah"]["value"]
        methods["avg_power_factor"] = real_energy / apparent_energy
    elif methods["integrated_iv_vah"]["value"] > 0:
        real_energy = methods["total_power_wh"]["value"]
        apparent_energy = methods["integrated_iv_vah"]["value"]
        methods["avg_power_factor"] = real_energy / apparent_energy
    
    return methods


def calculate_acuvim_energy(config: Dict[str, Any], start_time: int, end_time: int) -> Dict[str, Any]:
    """Calculate energy metrics for Acuvim meters."""
    system_id = config["system_id"]
    device_filter = f'device=~".*{system_id}.*"'
    methods = {}
    
    # Method 1: Total real power
    p_query = f'acuvim_P{{{device_filter}}}'
    methods["real_power_wh"] = {
        "value": vm_integrate_metric(p_query, start_time, end_time),
        "description": "Real energy consumed - integration of real power (W) over event duration",
        "metric": "acuvim_P",
        "includes_reactive": False
    }
    
    # Method 2: Apparent power calculated from P and Q
    if config["has_reactive_power"]:
        # Query P and Q as range vectors, calculate S = sqrt(P^2 + Q^2) point by point
        p_data = vm_query_range(p_query, start_time, end_time, step="30s")
        q_query = f'acuvim_Q{{{device_filter}}}'
        q_data = vm_query_range(q_query, start_time, end_time, step="30s")
        
        if p_data and q_data:
            p_results = p_data.get("result", [])
            q_results = q_data.get("result", [])
            
            if p_results and q_results:
                p_values = p_results[0].get("values", [])
                q_values = q_results[0].get("values", [])
                
                if len(p_values) == len(q_values) and len(p_values) > 1:
                    apparent_energy = 0.0
                    for i in range(1, len(p_values)):
                        t1, p1 = p_values[i-1][0], float(p_values[i-1][1])
                        t2, p2 = p_values[i][0], float(p_values[i][1])
                        _, q1 = q_values[i-1][0], float(q_values[i-1][1])
                        _, q2 = q_values[i][0], float(q_values[i][1])
                        
                        s1 = math.sqrt(p1**2 + q1**2)
                        s2 = math.sqrt(p2**2 + q2**2)
                        dt_hours = (t2 - t1) / 3600.0
                        apparent_energy += ((s1 + s2) / 2.0) * dt_hours
                    
                    methods["apparent_power_vah"] = {
                        "value": apparent_energy,
                        "description": "Apparent energy - calculated as sqrt(P^2 + Q^2) including reactive component",
                        "metric": "sqrt(acuvim_P^2 + acuvim_Q^2)",
                        "includes_reactive": True
                    }
                    
                    # Calculate average power factor
                    if apparent_energy > 0:
                        methods["avg_power_factor"] = methods["real_power_wh"]["value"] / apparent_energy
    
    # Method 3: Per-phase V*I integration
    iv_total = 0.0
    iv_per_phase = {}
    phase_map = {"A": "a", "B": "b", "C": "c"}
    
    for phase in config["phases"]:
        phase_lower = phase_map[phase]
        v_query = f'acuvim_V{phase_lower}{{{device_filter}}}'
        i_query = f'acuvim_I{phase_lower}{{{device_filter}}}'
        energy = vm_integrate_product(v_query, i_query, start_time, end_time)
        iv_per_phase[phase] = energy
        iv_total += energy
    
    methods["integrated_iv_vah"] = {
        "value": iv_total,
        "per_phase": iv_per_phase,
        "description": f"Apparent energy - V*I integration per phase ({len(config['phases'])} phases)",
        "metric": "V*I per phase",
        "includes_reactive": True
    }
    
    return methods


def calculate_energy_all_methods(config: Dict[str, Any], start_time: int, end_time: int) -> Dict[str, Any]:
    """Calculate energy using all applicable methods for this device configuration."""
    if config["source"] == "victron":
        return calculate_victron_energy(config, start_time, end_time)
    elif config["source"] == "acuvim":
        return calculate_acuvim_energy(config, start_time, end_time)
    else:
        return {"error": "Unknown device source"}


def calculate_power_stats(config: Dict[str, Any], start_time: int, end_time: int) -> Dict[str, Any]:
    """Calculate peak, average, and per-phase power statistics."""
    stats = {
        "peak_power_w": 0.0,
        "avg_power_w": 0.0,
        "per_phase": {}
    }
    
    if config["source"] == "victron":
        system_id = escape_prom_label_value(config["system_id"])
        
        # Total power stats
        p_query = f'victron_ac_out_power{{system_id="{system_id}"}}'
        stats["peak_power_w"] = vm_query_scalar(f'max_over_time({p_query}[{int((end_time - start_time) / 1e9)}s])') or 0.0
        stats["avg_power_w"] = vm_query_avg(p_query, start_time, end_time) or 0.0
        
        # Per-phase stats
        for phase in config["phases"]:
            phase_query = f'victron_ac_out_{phase.lower()}_p{{system_id="{system_id}"}}'
            stats["per_phase"][phase] = {
                "peak_w": vm_query_scalar(f'max_over_time({phase_query}[{int((end_time - start_time) / 1e9)}s])') or 0.0,
                "avg_w": vm_query_avg(phase_query, start_time, end_time) or 0.0
            }
    
    elif config["source"] == "acuvim":
        device_filter = f'device=~".*{config["system_id"]}.*"'
        
        # Total power stats
        p_query = f'acuvim_P{{{device_filter}}}'
        stats["peak_power_w"] = vm_query_scalar(f'max_over_time({p_query}[{int((end_time - start_time) / 1e9)}s])') or 0.0
        stats["avg_power_w"] = vm_query_avg(p_query, start_time, end_time) or 0.0
        
        # Acuvim does not have per-phase power directly, would need to calculate from V*I
        # For now, just report total
    
    return stats


def calculate_phase_imbalance(config: Dict[str, Any], start_time: int, end_time: int) -> float:
    """Calculate maximum phase imbalance as percentage."""
    if len(config["phases"]) < 2:
        return 0.0  # No imbalance for single-phase
    
    phase_avgs = []
    
    if config["source"] == "victron":
        system_id = escape_prom_label_value(config["system_id"])
        for phase in config["phases"]:
            query = f'victron_ac_out_{phase.lower()}_p{{system_id="{system_id}"}}'
            avg = vm_query_avg(query, start_time, end_time)
            if avg:
                phase_avgs.append(avg)
    
    elif config["source"] == "acuvim":
        device_filter = f'device=~".*{config["system_id"]}.*"'
        phase_map = {"A": "a", "B": "b", "C": "c"}
        for phase in config["phases"]:
            # Calculate avg V*I for each phase
            phase_lower = phase_map[phase]
            v_query = f'acuvim_V{phase_lower}{{{device_filter}}}'
            i_query = f'acuvim_I{phase_lower}{{{device_filter}}}'
            # Approximate with avg(V) * avg(I)
            avg_v = vm_query_avg(v_query, start_time, end_time)
            avg_i = vm_query_avg(i_query, start_time, end_time)
            if avg_v and avg_i:
                phase_avgs.append(avg_v * avg_i)
    
    if len(phase_avgs) < 2:
        return 0.0
    
    # Imbalance = (max - min) / avg * 100
    avg_all = sum(phase_avgs) / len(phase_avgs)
    if avg_all == 0:
        return 0.0
    
    imbalance_pct = ((max(phase_avgs) - min(phase_avgs)) / avg_all) * 100.0
    return round(imbalance_pct, 2)


def calculate_load_distribution(config: Dict[str, Any], start_time: int, end_time: int, peak_power: float) -> Dict[str, Any]:
    """Calculate time spent at different percentages of peak capacity."""
    if peak_power <= 0:
        return {}
    
    # Get power time series
    if config["source"] == "victron":
        system_id = escape_prom_label_value(config["system_id"])
        query = f'victron_ac_out_power{{system_id="{system_id}"}}'
    elif config["source"] == "acuvim":
        device_filter = f'device=~".*{config["system_id"]}.*"'
        query = f'acuvim_P{{{device_filter}}}'
    else:
        return {}
    
    # Query range data
    data = vm_query_range(query, start_time, end_time, step="30s")
    if not data or data.get("resultType") != "matrix":
        return {}
    
    results = data.get("result", [])
    if not results:
        return {}
    
    values = results[0].get("values", [])
    if len(values) < 2:
        return {}
    
    # Define bins: 0-20%, 20-40%, 40-60%, 60-80%, 80-100%, >100%
    bins = {
        "0-20%": 0.0,
        "20-40%": 0.0,
        "40-60%": 0.0,
        "60-80%": 0.0,
        "80-100%": 0.0,
        ">100%": 0.0
    }
    
    total_time = 0.0
    
    for i in range(1, len(values)):
        t1, v1 = values[i-1][0], float(values[i-1][1])
        t2, _ = values[i][0], float(values[i][1])
        dt = t2 - t1  # seconds
        
        pct = (v1 / peak_power) * 100.0
        
        if pct < 20:
            bins["0-20%"] += dt
        elif pct < 40:
            bins["20-40%"] += dt
        elif pct < 60:
            bins["40-60%"] += dt
        elif pct < 80:
            bins["60-80%"] += dt
        elif pct <= 100:
            bins["80-100%"] += dt
        else:
            bins[">100%"] += dt
        
        total_time += dt
    
    # Convert to percentages and readable format
    distribution = {}
    for bin_name, seconds in bins.items():
        distribution[bin_name] = {
            "seconds": round(seconds, 1),
            "percent": round((seconds / total_time * 100.0), 1) if total_time > 0 else 0.0
        }
    
    return distribution


def trim_event_times(loggers: list, start_time: int, end_time: int) -> tuple:
    """Trim event start/end times to exclude idle periods where load is near zero.
    
    Returns (trimmed_start, trimmed_end, was_trimmed_flag)
    """
    if not loggers:
        return start_time, end_time, False
    
    # Query power data for all loggers to find actual load activity
    all_power_data = []
    
    for logger_info in loggers:
        system_id = logger_info["system_id"]
        
        # Try all system_id variants (handles Logger X -> acuvim_1X, etc.).
        variants = normalize_system_id_for_query(system_id)
        
        data_found = False
        for sid_variant in variants:
            if not data_found:
                # Try Victron
                victron_query = f'victron_ac_out_power{{system_id="{escape_prom_label_value(sid_variant)}"}}'
                data = vm_query_range(victron_query, start_time, end_time, step="10s")
                
                if data and data.get("result") and data["result"]:
                    all_power_data.extend(data["result"][0].get("values", []))
                    data_found = True
            
            if not data_found:
                # Try Acuvim
                acuvim_query = f'acuvim_P{{device=~".*{escape_prom_label_value(sid_variant)}.*"}}'
                data = vm_query_range(acuvim_query, start_time, end_time, step="10s")
                
                if data and data.get("result") and data["result"]:
                    all_power_data.extend(data["result"][0].get("values", []))
                    data_found = True
    
    if not all_power_data or len(all_power_data) < 10:
        logger.warning("Not enough power data for time trimming")
        return start_time, end_time, False
    
    # Sort by timestamp
    all_power_data.sort(key=lambda x: x[0])
    
    # Calculate peak power to determine threshold
    powers = [abs(float(p[1])) for p in all_power_data]
    peak_power = max(powers)
    
    # Threshold: 2% of peak or 50W, whichever is higher
    threshold = max(peak_power * 0.02, 50.0)
    
    # Find first sustained period above threshold (at least 60 seconds)
    trimmed_start = start_time
    for i in range(len(all_power_data) - 6):  # Need 6 consecutive points (60s)
        if all(abs(float(all_power_data[j][1])) > threshold for j in range(i, min(i + 6, len(all_power_data)))):
            trimmed_start = int(all_power_data[i][0] * 1e9)  # Convert to nanoseconds
            break
    
    # Find last sustained period above threshold
    trimmed_end = end_time
    for i in range(len(all_power_data) - 1, 5, -1):  # Search backwards
        if all(abs(float(all_power_data[j][1])) > threshold for j in range(max(0, i - 5), i + 1)):
            trimmed_end = int(all_power_data[i][0] * 1e9)  # Convert to nanoseconds
            break
    
    # Only trim if we actually found load activity
    was_trimmed = (trimmed_start != start_time or trimmed_end != end_time)
    
    if was_trimmed:
        logger.info(f"Trimmed event times: {(start_time - trimmed_start) / 1e9:.1f}s from start, {(trimmed_end - end_time) / 1e9:.1f}s from end")
    
    return trimmed_start, trimmed_end, was_trimmed


def render_report_html(report: Dict[str, Any], image_base_url: str = "/images") -> str:
    """Render report JSON data as styled HTML."""
    event_id = report["event_id"]
    duration_hours = report["duration_seconds"] / 3600.0
    start_dt = datetime.fromtimestamp(report["start_time"] / 1e9)
    end_dt = datetime.fromtimestamp(report["end_time"] / 1e9)
    gen_dt = datetime.fromtimestamp(report["generated_at"] / 1e9)
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Event Report: {event_id}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
            color: #333;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
        }}
        .header h1 {{ margin-bottom: 10px; font-size: 2em; }}
        .header .meta {{ opacity: 0.9; font-size: 0.95em; }}
        .content {{ padding: 30px; }}
        .section {{ margin-bottom: 40px; }}
        .section h2 {{
            color: #667eea;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
            margin-bottom: 20px;
            font-size: 1.5em;
        }}
        .logger-card {{
            background: #f8f9fa;
            border-left: 4px solid #667eea;
            padding: 20px;
            margin-bottom: 30px;
            border-radius: 8px;
        }}
        .logger-card h3 {{
            color: #333;
            margin-bottom: 15px;
            font-size: 1.3em;
        }}
        .config-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }}
        .config-item {{
            background: white;
            padding: 12px;
            border-radius: 6px;
            border: 1px solid #e0e0e0;
        }}
        .config-item label {{
            display: block;
            font-size: 0.85em;
            color: #666;
            margin-bottom: 5px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .config-item value {{
            font-size: 1.1em;
            font-weight: 600;
            color: #333;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
            background: white;
        }}
        table th {{
            background: #667eea;
            color: white;
            padding: 12px;
            text-align: left;
            font-weight: 600;
        }}
        table td {{
            padding: 10px 12px;
            border-bottom: 1px solid #e0e0e0;
        }}
        table tr:hover {{ background: #f8f9fa; }}
        .metric-value {{ font-weight: 600; color: #667eea; }}
        .load-bar {{
            background: #e0e0e0;
            height: 30px;
            border-radius: 4px;
            overflow: hidden;
            margin: 5px 0;
            position: relative;
        }}
        .load-bar-fill {{
            background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
            height: 100%;
            display: flex;
            align-items: center;
            padding-left: 10px;
            color: white;
            font-weight: 600;
            font-size: 0.9em;
        }}
        .error-box {{
            background: #fee;
            border-left: 4px solid #f44;
            padding: 15px;
            border-radius: 6px;
            color: #c33;
        }}
        .notes-list {{ list-style: none; }}
        .notes-list li {{
            background: #fffef0;
            border-left: 3px solid #ffc107;
            padding: 12px;
            margin: 10px 0;
            border-radius: 4px;
        }}
        .note-meta {{ font-size: 0.85em; color: #666; margin-top: 5px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Event Report: {event_id}</h1>
            <div class="meta">
                <div>Duration: {duration_hours:.2f} hours ({report["duration_seconds"] / 60:.1f} minutes)</div>
                <div>Period: {start_dt.strftime("%Y-%m-%d %H:%M:%S")} - {end_dt.strftime("%Y-%m-%d %H:%M:%S")}</div>
                <div>Generated: {gen_dt.strftime("%Y-%m-%d %H:%M:%S")}</div>
            </div>
        </div>
        <div class="content">
"""
    
    # Logger sections
    for system_id, logger_data in report["loggers"].items():
        config = logger_data["config"]
        location = logger_data.get("location", "")
        
        # Device icon based on source
        icon = "Victron" if config["source"] == "victron" else "Acuvim"
        
        html += f"""
            <div class="section">
                <div class="logger-card">
                    <h3>{icon} {system_id}</h3>
"""
        
        if location:
            html += f'<p style="color: #666; margin-bottom: 15px;">Location: {location}</p>\n'
        
        # Configuration
        html += """
                    <div class="config-grid">
"""
        
        phase_config = (config.get("phase_config") or "unknown").replace("_", " ").title()
        phases = config.get("phases") or []
        confidence = (config.get("detection_confidence") or "unknown").title()

        config_items = [
            ("Device Type", config.get("device_model", "unknown").upper() if config.get("device_model") else "Unknown"),
            ("Phase Config", phase_config),
            ("Phases", ", ".join(phases) if phases else "N/A"),
            ("Nominal Voltage", f"{config.get('voltage_nominal', 'N/A')} V" if config.get('voltage_nominal') else "N/A"),
            ("Confidence", confidence)
        ]
        
        for label, value in config_items:
            html += f"""
                        <div class="config-item">
                            <label>{label}</label>
                            <value>{value}</value>
                        </div>
"""
        
        html += """
                    </div>
"""
        
        # Energy Methods
        energy_methods = logger_data.get("energy_methods", {})
        if energy_methods:
            html += """
                    <h4 style="margin-top: 25px; margin-bottom: 15px; color: #333;">Energy Calculation Methods</h4>
                    <table>
                        <thead>
                            <tr>
                                <th>Method</th>
                                <th>Value</th>
                                <th>Description</th>
                            </tr>
                        </thead>
                        <tbody>
"""
            
            # Sort methods to show primary energy methods first
            sorted_methods = sorted(energy_methods.items(), key=lambda x: (
                x[0] == "avg_power_factor",  # Power factor last
                x[0]
            ))
            
            for method_name, method_data in sorted_methods:
                if method_name == "avg_power_factor":
                    html += f"""
                            <tr>
                                <td><strong>Average Power Factor</strong></td>
                                <td class="metric-value">{method_data:.3f}</td>
                                <td>Ratio of real to apparent energy</td>
                            </tr>
"""
                elif isinstance(method_data, dict) and "value" in method_data:
                    value = method_data["value"]
                    unit = "VAh" if method_data.get("includes_reactive") else "Wh"
                    description = method_data.get("description", "")
                    
                    # Format value with commas
                    value_str = f"{value:,.1f} {unit}"
                    
                    # Add per-phase breakdown if available
                    per_phase = method_data.get("per_phase", {})
                    if per_phase:
                        phase_details = ", ".join([f"{phase}: {val:,.1f} {unit}" for phase, val in per_phase.items()])
                        description += f" <br><small style='color: #666;'>({phase_details})</small>"
                    
                    html += f"""
                            <tr>
                                <td><strong>{method_name.replace('_', ' ').title()}</strong></td>
                                <td class="metric-value">{value_str}</td>
                                <td>{description}</td>
                            </tr>
"""
            
            html += """
                        </tbody>
                    </table>
"""
        
        # Power Statistics
        power_stats = logger_data.get("power_stats", {})
        if power_stats:
            html += """
                    <h4 style="margin-top: 25px; margin-bottom: 15px; color: #333;">Power Statistics</h4>
                    <table>
                        <thead>
                            <tr>
                                <th>Metric</th>
                                <th>Value</th>
                            </tr>
                        </thead>
                        <tbody>
"""
            
            html += f"""
                            <tr>
                                <td><strong>Peak Power</strong></td>
                                <td class="metric-value">{power_stats.get('peak_power_w', 0):,.1f} W</td>
                            </tr>
                            <tr>
                                <td><strong>Average Power</strong></td>
                                <td class="metric-value">{power_stats.get('avg_power_w', 0):,.1f} W</td>
                            </tr>
"""
            
            per_phase = power_stats.get("per_phase", {})
            if per_phase:
                for phase, stats in per_phase.items():
                    html += f"""
                            <tr>
                                <td><strong>Phase {phase} Peak</strong></td>
                                <td class="metric-value">{stats.get('peak_w', 0):,.1f} W</td>
                            </tr>
                            <tr>
                                <td><strong>Phase {phase} Average</strong></td>
                                <td class="metric-value">{stats.get('avg_w', 0):,.1f} W</td>
                            </tr>
"""
            
            html += """
                        </tbody>
                    </table>
"""
        
        # Phase Imbalance
        phase_imbalance = logger_data.get("phase_imbalance_pct")
        if phase_imbalance is not None and phase_imbalance > 0:
            html += f"""
                    <p style="margin-top: 20px;"><strong>Phase Imbalance:</strong> <span class="metric-value">{phase_imbalance:.1f}%</span></p>
"""
        
        # Load Distribution
        load_dist = logger_data.get("load_distribution", {})
        if load_dist:
            html += """
                    <h4 style="margin-top: 25px; margin-bottom: 15px; color: #333;">Load Distribution</h4>
"""
            
            for bin_name in ["0-20%", "20-40%", "40-60%", "60-80%", "80-100%", ">100%"]:
                if bin_name in load_dist:
                    bin_data = load_dist[bin_name]
                    percent = bin_data["percent"]
                    seconds = bin_data["seconds"]
                    minutes = seconds / 60.0
                    
                    html += f"""
                    <div style="margin-bottom: 10px;">
                        <div style="display: flex; justify-content: space-between; margin-bottom: 3px;">
                            <span><strong>{bin_name}</strong></span>
                            <span>{percent}% ({minutes:.1f} min)</span>
                        </div>
                        <div class="load-bar">
                            <div class="load-bar-fill" style="width: {min(percent, 100)}%">
                                {percent}%
                            </div>
                        </div>
                    </div>
"""
        
        html += """
                </div>
            </div>
"""
    
    # Notes section
    if report.get("notes"):
        html += """
            <div class="section">
                <h2>Notes</h2>
                <ul class="notes-list">
"""
        for note in report["notes"]:
            if note["note"]:  # Only show non-empty notes
                note_dt = datetime.fromtimestamp(note["timestamp"] / 1e9)
                html += f"""
                    <li>
                        {note["note"]}
                        <div class="note-meta">
                            {note["system_id"]} - {note_dt.strftime("%Y-%m-%d %H:%M:%S")}
                        </div>
                    </li>
"""
        html += """
                </ul>
            </div>
"""
    
    # Images section
    if report.get("images"):
        image_base = image_base_url.rstrip("/")
        html += f"""
            <div class="section">
                <h2>Images ({len(report["images"])})</h2>
                <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 15px; margin-top: 20px;">
"""
        for img in report["images"]:
            img_time = datetime.fromtimestamp(img["timestamp"] / 1e9).strftime("%Y-%m-%d %H:%M:%S")
            # Use relative path for images that works with REPORT_BASE_URL
            img_url = f"{image_base}/{img['filename']}"
            html += f"""
                    <div style="border: 1px solid #ddd; border-radius: 8px; overflow: hidden; background: white;">
                        <a href="{img_url}" target="_blank" style="text-decoration: none;">
                            <img src="{img_url}" alt="Event image" style="width: 100%; height: 150px; object-fit: cover; display: block;">
                            <div style="padding: 8px; font-size: 12px; color: #666;">
                                <div style="font-weight: 600; color: #333;">{img['system_id']}</div>
                                <div>{img_time}</div>
                            </div>
                        </a>
                    </div>
"""
        html += """
                </div>
            </div>
"""
    
    html += """
        </div>
    </div>
</body>
</html>
"""
    
    return html


def _report_upload_backoff_seconds(attempts: int) -> int:
    exponent = min(max(attempts, 0), 6)
    return min(REPORT_UPLOAD_BACKOFF_MAX, REPORT_UPLOAD_BACKOFF_BASE * (2**exponent))


def _post_report_payload(payload: Dict[str, Any]) -> bool:
    if not REPORT_UPLOAD_URL or not REPORT_UPLOAD_TOKEN:
        return False
    headers = {"X-Report-Token": REPORT_UPLOAD_TOKEN}
    try:
        resp = requests.post(
            REPORT_UPLOAD_URL,
            json=payload,
            headers=headers,
            timeout=REPORT_UPLOAD_TIMEOUT,
        )
    except Exception as exc:
        logger.warning(f"Report upload failed: {exc}")
        return False
    if resp.status_code not in (200, 201):
        logger.warning(
            "Report upload failed: %s %s", resp.status_code, resp.text[:200]
        )
        return False
    return True


def _queue_report_payload(payload: Dict[str, Any], error: str = "") -> None:
    now = int(time.time())
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO report_outbox
                (payload, created_at, updated_at, attempts, next_attempt_at, last_error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                json.dumps(payload),
                now,
                now,
                0,
                now,
                error,
            ),
        )
        conn.commit()


def _process_report_outbox() -> None:
    if not REPORT_UPLOAD_URL or not REPORT_UPLOAD_TOKEN:
        return
    now = int(time.time())
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, payload, attempts
            FROM report_outbox
            WHERE next_attempt_at <= ?
            ORDER BY id
            LIMIT 10
            """,
            (now,),
        ).fetchall()
    for row in rows:
        row_id = row["id"]
        try:
            payload = json.loads(row["payload"])
        except Exception:
            with get_db() as conn:
                conn.execute("DELETE FROM report_outbox WHERE id = ?", (row_id,))
                conn.commit()
            continue
        if _post_report_payload(payload):
            with get_db() as conn:
                conn.execute("DELETE FROM report_outbox WHERE id = ?", (row_id,))
                conn.commit()
        else:
            attempts = int(row["attempts"] or 0) + 1
            next_attempt = now + _report_upload_backoff_seconds(attempts)
            with get_db() as conn:
                conn.execute(
                    """
                    UPDATE report_outbox
                    SET attempts = ?, updated_at = ?, next_attempt_at = ?, last_error = ?
                    WHERE id = ?
                    """,
                    (attempts, now, next_attempt, "upload failed", row_id),
                )
                conn.commit()


def report_upload_worker() -> None:
    while not _heartbeat_stop.is_set():
        try:
            _process_report_outbox()
        except Exception as exc:
            logger.warning(f"Report upload retry failed: {exc}")
        _heartbeat_stop.wait(max(REPORT_UPLOAD_RETRY_INTERVAL, 5))


def _upload_report_json(report: Dict[str, Any], report_html: Optional[str] = None) -> None:
    if not REPORT_UPLOAD_URL or not REPORT_UPLOAD_TOKEN:
        return
    event_id = report.get("event_id")
    temp_event_id = report.get("temp_event_id") or (
        event_id if _is_temp_event_id(event_id or "") else ""
    )
    event_id_original = report.get("event_id_original") or temp_event_id
    payload = {
        "report_type": "event",
        "event_id": event_id,
        "generated_at": report.get("generated_at"),
        "system_id": SYSTEM_ID or None,
        "node_id": NODE_ID or None,
        "deployment_id": DEPLOYMENT_ID or None,
        "report": report,
    }
    if temp_event_id:
        payload["temp_event_id"] = temp_event_id
    if event_id_original:
        payload["event_id_original"] = event_id_original
    if report_html:
        payload["report_html"] = report_html
    if _post_report_payload(payload):
        return
    _queue_report_payload(payload, "initial upload failed")


def generate_event_report(event_id: str) -> Dict[str, Any]:
    """Generate comprehensive report for an event with all loggers."""
    logger.info(f"Generating report for event_id={event_id}")
    
    # Create reports directory if it doesn't exist
    os.makedirs(REPORTS_PATH, exist_ok=True)
    
    # Get event details and all loggers
    with get_db() as conn:
        # Find the most recent contiguous event block for this event_id
        # Get all event_end timestamps for this event_id (end + end_all)
        end_times = conn.execute(
            "SELECT timestamp FROM audit_log WHERE event_id = ? AND action IN ('event_end', 'event_end_all') ORDER BY timestamp DESC",
            (event_id,)
        ).fetchall()

        latest_start_row = conn.execute(
            "SELECT timestamp FROM audit_log WHERE event_id = ? AND action IN ('event_start', 'logger_add') ORDER BY timestamp DESC LIMIT 1",
            (event_id,)
        ).fetchone()

        if not latest_start_row:
            return {"error": "Event not found in audit log"}

        latest_start = latest_start_row["timestamp"]
        end_time = None
        previous_end = None

        if end_times:
            for idx, row in enumerate(end_times):
                ts = row["timestamp"]
                if ts >= latest_start:
                    end_time = ts
                    if idx + 1 < len(end_times):
                        previous_end = end_times[idx + 1]["timestamp"]
                    logger.info(f"Found event_end at {datetime.fromtimestamp(end_time / 1e9)}")
                    break

            if end_time is None:
                end_time = int(time.time() * 1e9)
                previous_end = end_times[0]["timestamp"]
                logger.info("No event_end after latest start, using current time")
        else:
            end_time = int(time.time() * 1e9)
            logger.info("No event_end found, using current time")

        # Find all starts that occurred before this end time (and after the previous end)
        if previous_end:
            start_records = conn.execute(
                "SELECT timestamp, system_id, location FROM audit_log WHERE event_id = ? AND action IN ('event_start', 'logger_add') AND timestamp > ? AND timestamp <= ? ORDER BY timestamp",
                (event_id, previous_end, end_time)
            ).fetchall()
        else:
            start_records = conn.execute(
                "SELECT timestamp, system_id, location FROM audit_log WHERE event_id = ? AND action IN ('event_start', 'logger_add') AND timestamp <= ? ORDER BY timestamp",
                (event_id, end_time)
            ).fetchall()
        
        if not start_records:
            return {"error": "Event not found in audit log"}
        
        # Get the first start (beginning of the event)
        first_start = start_records[0]["timestamp"]
        logger.info(f"Event started at: {datetime.fromtimestamp(first_start / 1e9)}")
        
        # Get all loggers from the entire event period
        logger.info(f"Found {len(start_records)} total logger starts")
        for rec in start_records:
            logger.info(f"  - {rec['system_id']} at {datetime.fromtimestamp(rec['timestamp'] / 1e9)}")
        
        # Build unique logger list
        loggers = []
        for record in start_records:
            if record["system_id"] not in [l["system_id"] for l in loggers]:
                loggers.append({
                    "system_id": record["system_id"],
                    "start_time": record["timestamp"],
                    "location": record["location"]
                })
        
        start_time = first_start
        
        # Trim idle periods from start/end
        original_start = start_time
        original_end = end_time
        start_time, end_time, was_trimmed = trim_event_times(loggers, start_time, end_time)
        
        if was_trimmed:
            logger.info(f"Event times trimmed: {(original_start - start_time) / 1e9:.1f}s from start, {(end_time - original_end) / 1e9:.1f}s from end")
        
        # Get notes for this event (only within trimmed time range)
        notes = conn.execute(
            "SELECT timestamp, system_id, note FROM audit_log WHERE event_id = ? AND note IS NOT NULL AND note != '' AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (event_id, start_time, end_time)
        ).fetchall()
        
        # Get images for this event (only within trimmed time range)
        images = conn.execute(
            "SELECT filename, system_id, timestamp FROM images WHERE event_id = ? AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (event_id, start_time, end_time)
        ).fetchall()
    
    # Build report structure
    report = {
        "event_id": event_id,
        "generated_at": int(time.time() * 1e9),
        "start_time": loggers[0]["start_time"] if loggers else 0,
        "end_time": end_time,
        "duration_seconds": (end_time - loggers[0]["start_time"]) / 1e9 if loggers else 0,
        "loggers": {},
        "notes": [{"timestamp": n["timestamp"], "system_id": n["system_id"], "note": n["note"]} for n in notes],
        "images": [{"filename": img["filename"], "system_id": img["system_id"], "timestamp": img["timestamp"]} for img in images]
    }
    
    # Process each logger
    for logger_info in loggers:
        system_id = logger_info["system_id"]
        start_time = logger_info["start_time"]
        
        logger.info(f"Processing logger {system_id} for report")
        
        # Detect device configuration
        config = detect_device_configuration(system_id, start_time, end_time)
        
        if config["detection_confidence"] == "none":
            logger.info(f"Skipping {system_id} - no metrics found")
            continue
        
        # Calculate energy using all methods
        energy_methods = calculate_energy_all_methods(config, start_time, end_time)
        
        # Calculate power statistics
        power_stats = calculate_power_stats(config, start_time, end_time)
        
        # Calculate phase imbalance
        phase_imbalance = calculate_phase_imbalance(config, start_time, end_time)
        
        # Calculate load distribution
        load_dist = calculate_load_distribution(config, start_time, end_time, power_stats["peak_power_w"])
        
        # Assemble logger report
        report["loggers"][system_id] = {
            "config": config,
            "location": logger_info["location"],
            "energy_methods": energy_methods,
            "power_stats": power_stats,
            "phase_imbalance_pct": phase_imbalance,
            "load_distribution": load_dist
        }
    
    # Save report as JSON
    timestamp_str = datetime.fromtimestamp(report["generated_at"] / 1e9).strftime("%Y%m%d_%H%M%S")
    # Sanitize event_id for filesystem (replace problematic characters)
    safe_event_id = event_id.replace("'", "").replace('"', "").replace("/", "_").replace("\\", "_").replace(" ", "_")
    report_dir = os.path.join(REPORTS_PATH, f"event_{safe_event_id}_{timestamp_str}")
    os.makedirs(report_dir, exist_ok=True)
    
    json_path = os.path.join(report_dir, "data.json")
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)
    
    # Generate HTML report
    html_path = os.path.join(report_dir, "report.html")
    html_content = render_report_html(report)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    logger.info(f"Report saved to {report_dir}")
    
    return {
        "success": True,
        "report_path": report_dir,
        "json_file": json_path,
        "html_file": html_path,
        "event_id": event_id,
        "loggers_processed": len(report["loggers"])
    }


def heartbeat_worker():
    """Background thread that continuously writes active events and locations to VM."""
    logger.info("Heartbeat worker started")
    
    while not _heartbeat_stop.is_set():
        try:
            lines = []
            ts_ns = int(time.time() * 1e9)
            
            with get_db() as conn:
                # Get all active events with their locations
                active_events = conn.execute(
                    "SELECT system_id, event_id, location FROM active_events"
                ).fetchall()
                
                for event in active_events:
                    system_id = escape_tag_value(event["system_id"])
                    event_id = escape_tag_value(event["event_id"])
                    location = escape_tag_value(event["location"] or "-")
                    
                    # Write unified ovr_event metric
                    line = build_event_line(event_id, system_id, location, 1, ts_ns)
                    lines.append(line)
            
            # Write to VictoriaMetrics if we have any active events/locations
            if lines:
                write_to_vm(lines)
            
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
        
        # Sleep for the configured interval
        _heartbeat_stop.wait(HEARTBEAT_INTERVAL)
    
    logger.info("Heartbeat worker stopped")


# ============================================================================
# GX Device D-Bus Control via SSH
# ============================================================================

def ssh_dbus_write(service: str, path: str, value: Any, value_type: str = "int32") -> bool:
    """Write a value to GX D-Bus via persistent SSH connection."""
    if not PARAMIKO_AVAILABLE or _gx_ssh is None:
        logger.error("SSH not available - paramiko not installed")
        return False
    
    command = f'dbus -y {service} {path} com.victronenergy.BusItem.SetValue {value}'
    
    try:
        logger.info(f"Attempting D-Bus write: {service}{path} = {value} (type: {value_type})")
        exit_code, stdout, stderr = _gx_ssh.execute(command, timeout=10)
        
        if exit_code == 0:
            logger.info(f"D-Bus write successful: {service}{path} = {value}")
            logger.debug(f"stdout: {stdout}")
            return True
        else:
            logger.error(f"D-Bus write failed (exit {exit_code}): {stderr}")
            logger.error(f"stdout: {stdout}")
            return False
    except Exception as e:
        logger.error(f"SSH D-Bus write error: {e}")
        return False


# ============================================================================
# Persistent SSH Connection Pool for GX Device
# ============================================================================

class GXSSHConnection:
    """Manages a persistent SSH connection to the GX device with auto-reconnect."""
    
    def __init__(self):
        self.client = None
        self.lock = threading.Lock()
        self.last_used = 0
        self.connection_timeout = 300  # Close after 5 min idle
    
    def _connect(self):
        """Establish SSH connection."""
        if not PARAMIKO_AVAILABLE:
            raise RuntimeError("paramiko not installed")
        
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            client.connect(
                GX_HOST,
                username=GX_USER,
                password=GX_PASSWORD,
                timeout=5,
                allow_agent=False,
                look_for_keys=False
            )
            logger.info(f"SSH connected to {GX_HOST}")
            return client
        except Exception as e:
            logger.error(f"SSH connection failed: {e}")
            raise
    
    def _is_alive(self):
        """Check if connection is still alive."""
        if self.client is None:
            return False
        try:
            transport = self.client.get_transport()
            return transport is not None and transport.is_active()
        except:
            return False
    
    def _ensure_connected(self):
        """Ensure connection is active, reconnect if needed."""
        # Check for stale connection (idle too long)
        if self.client and (time.time() - self.last_used) > self.connection_timeout:
            logger.info("SSH connection idle timeout, closing")
            self._close()
        
        if not self._is_alive():
            if self.client:
                self._close()
            logger.info("SSH connection lost, reconnecting...")
            self.client = self._connect()
        
        self.last_used = time.time()
    
    def _close(self):
        """Close SSH connection."""
        if self.client:
            try:
                self.client.close()
            except:
                pass
            self.client = None
    
    def execute(self, command: str, timeout: int = 10) -> tuple[int, str, str]:
        """Execute command via SSH with auto-reconnect.
        
        Returns:
            (exit_code, stdout, stderr)
        """
        with self.lock:
            try:
                self._ensure_connected()
                stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
                exit_code = stdout.channel.recv_exit_status()
                stdout_str = stdout.read().decode('utf-8', errors='ignore').strip()
                stderr_str = stderr.read().decode('utf-8', errors='ignore').strip()
                return exit_code, stdout_str, stderr_str
            except Exception as e:
                logger.error(f"SSH execute error: {e}")
                # Try one reconnect on failure
                try:
                    self._close()
                    self._ensure_connected()
                    stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
                    exit_code = stdout.channel.recv_exit_status()
                    stdout_str = stdout.read().decode('utf-8', errors='ignore').strip()
                    stderr_str = stderr.read().decode('utf-8', errors='ignore').strip()
                    return exit_code, stdout_str, stderr_str
                except Exception as e2:
                    logger.error(f"SSH reconnect failed: {e2}")
                    raise
    
    def close(self):
        """Explicitly close connection."""
        with self.lock:
            self._close()


# Global SSH connection pool
_gx_ssh = GXSSHConnection() if PARAMIKO_AVAILABLE else None


# GX D-Bus paths for common settings
GX_DBUS_PATHS = {
    "battery_charge_current": {
        "service": "com.victronenergy.vebus.ttyS2",
        "path": "/Dc/0/MaxChargeCurrent",
        "type": "int32",
        "description": "Battery max charge current (A)"
    },
    "inverter_mode": {
        "service": "com.victronenergy.vebus.ttyS2",  # Adjust ttyS2 if needed
        "path": "/Mode",
        "type": "int32",
        "description": "Inverter mode (3=on, 2=inverter only, 1=charger only, 4=off)",
        "values": {"on": 3, "inverter_only": 2, "charger_only": 1, "off": 4}
    },
    "ac_input_current_limit": {
        "service": "com.victronenergy.vebus.ttyS2",
        "path": "/Ac/ActiveIn/CurrentLimit",
        "type": "double",
        "description": "AC input current limit (A)"
    },
    "inverter_output_voltage": {
        "service": "com.victronenergy.vebus.ttyS2",
        "path": "/Settings/InverterOutputVoltage",
        "type": "int32",
        "description": "Inverter AC output voltage (V)"
    }
}


# ============================================================================
# SQLite State Persistence
# ============================================================================

def init_db():
    """Initialize SQLite database schema and ensure directories exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(IMAGES_PATH, exist_ok=True)
    os.makedirs(REPORTS_PATH, exist_ok=True)
    
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS active_events (
                system_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                location TEXT,
                started_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                action TEXT NOT NULL,
                system_id TEXT NOT NULL,
                event_id TEXT,
                location TEXT,
                note TEXT,
                success INTEGER NOT NULL,
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                system_id TEXT NOT NULL,
                event_id TEXT,
                location TEXT,
                caption TEXT,
                timestamp INTEGER NOT NULL,
                file_size INTEGER NOT NULL,
                content_type TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gps_cache (
                system_id TEXT PRIMARY KEY,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS report_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL,
                next_attempt_at INTEGER NOT NULL,
                last_error TEXT
            )
            """
        )
        init_map_tables(conn)
        conn.commit()
    
    logger.info(f"Database initialized at {DB_PATH}")
    logger.info(f"Image storage at {IMAGES_PATH}")


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def get_cached_gps(system_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT latitude, longitude, updated_at FROM gps_cache WHERE system_id = ?",
            (system_id,)
        ).fetchone()
        return dict(row) if row else None


def set_cached_gps(system_id: str, latitude: float, longitude: float, updated_at: int) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO gps_cache (system_id, latitude, longitude, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (system_id, latitude, longitude, updated_at)
        )
        conn.commit()


def get_gps_from_vm(system_id: str) -> tuple[Optional[float], Optional[float]]:
    """Fetch GPS coordinates from VictoriaMetrics in a single query."""
    label = escape_prom_label_value(system_id)
    # Use vector query to get both lat/lon in one API call
    results = vm_query_vector(
        f'{{__name__=~"victron_gps_(latitude|longitude)",system_id="{label}"}}'
    )
    
    lat, lon = None, None
    for series in results:
        metric = series.get("metric", {})
        metric_name = metric.get("__name__", "")
        value = _vm_value_to_float(series.get("value"))
        
        if metric_name == "victron_gps_latitude":
            lat = value
        elif metric_name == "victron_gps_longitude":
            lon = value
    
    return lat, lon


def log_audit(action: str, system_id: str, event_id: Optional[str] = None,
              location: Optional[str] = None, note: Optional[str] = None,
              success: bool = True, error: Optional[str] = None):
    """Log action to audit log."""
    with get_db() as conn:
        conn.execute("""
            INSERT INTO audit_log 
            (timestamp, action, system_id, event_id, location, note, success, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            int(time.time() * 1e9),  # ns
            action,
            system_id,
            event_id,
            location,
            note,
            1 if success else 0,
            error
        ))
        conn.commit()


# ============================================================================
# API Endpoints
# ============================================================================

def check_api_key():
    """Simple API key check if configured."""
    if not API_KEY:
        return True
    provided = request.headers.get("X-API-Key", "")
    return provided == API_KEY


# ============================================================================
# Map Tile Usage
# ============================================================================

_MONTH_KEY_RE = re.compile(r"^\d{4}-\d{2}$")


def _is_valid_month_key(value: Optional[str]) -> bool:
    if not value:
        return False
    return bool(_MONTH_KEY_RE.match(value))


def _month_label(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m")
        return parsed.strftime("%B %Y")
    except Exception:
        return value


def _build_thresholds() -> Dict[str, int]:
    return {
        "mapbox": MAPBOX_FREE_TILES_PER_MONTH,
        "esri": ESRI_FREE_TILES_PER_MONTH,
        "guardrailPct": GUARDRAIL_LIMIT_PCT,
    }


def _cloud_url(path: str) -> Optional[str]:
    if not CLOUD_API_URL:
        return None
    return f"{CLOUD_API_URL.rstrip('/')}{path}"


def _post_cloud_event_node(event_id: str, node_id: str) -> None:
    if not event_id or _is_temp_event_id(event_id):
        return
    url = _cloud_url(f"/api/events/{quote(event_id, safe='')}/nodes")
    if not url:
        return
    try:
        requests.post(
            url,
            json={"node_id": node_id},
            timeout=CLOUD_API_TIMEOUT,
        )
    except Exception as exc:
        logger.warning(f"Cloud event node add failed: {exc}")


def _post_cloud_event_node_end(event_id: str, node_id: str) -> None:
    if not event_id or _is_temp_event_id(event_id):
        return
    url = _cloud_url(f"/api/events/{quote(event_id, safe='')}/nodes/{quote(node_id, safe='')}/end")
    if not url:
        return
    try:
        requests.post(url, timeout=CLOUD_API_TIMEOUT)
    except Exception as exc:
        logger.warning(f"Cloud event node end failed: {exc}")


def _fetch_cloud_status() -> Optional[Dict[str, Any]]:
    url = _cloud_url("/api/tiles/state")
    if not url:
        return None
    params = {}
    if DEPLOYMENT_ID:
        params["deployment_id"] = DEPLOYMENT_ID
    try:
        resp = requests.get(url, params=params, timeout=CLOUD_API_TIMEOUT)
    except Exception as exc:
        logger.warning(f"Cloud status fetch failed: {exc}")
        return None
    if resp.status_code != 200:
        logger.warning(f"Cloud status fetch failed ({resp.status_code})")
        return None
    try:
        return resp.json()
    except Exception as exc:
        logger.warning(f"Cloud status parse failed: {exc}")
        return None


def _post_cloud_preferred(provider: str) -> Optional[Dict[str, Any]]:
    url = _cloud_url("/api/fleet/map-provider/preferred")
    if not url:
        return None
    payload: Dict[str, Any] = {"provider": provider}
    if DEPLOYMENT_ID:
        payload["deployment_id"] = DEPLOYMENT_ID
    try:
        resp = requests.post(url, json=payload, timeout=CLOUD_API_TIMEOUT)
    except Exception as exc:
        logger.warning(f"Cloud preferred update failed: {exc}")
        return None
    try:
        data = resp.json()
    except Exception:
        data = {"error": resp.text}
    data["status_code"] = resp.status_code
    return data


def _post_cloud_tile_usage(provider: str, delta: int, month_key: str) -> bool:
    url = _cloud_url("/api/tiles/usage")
    if not url:
        return False
    node_id = NODE_ID or SYSTEM_ID
    payload: Dict[str, Any] = {
        "provider": provider,
        "delta": delta,
        "month": month_key,
        "node_id": node_id,
    }
    if DEPLOYMENT_ID:
        payload["deployment_id"] = DEPLOYMENT_ID
    try:
        resp = requests.post(url, json=payload, timeout=CLOUD_API_TIMEOUT)
    except Exception as exc:
        logger.warning(f"Cloud tile usage update failed: {exc}")
        return False
    if resp.status_code not in (200, 201):
        logger.warning(f"Cloud tile usage update failed ({resp.status_code})")
        return False
    return True


def _sync_tile_usage_once() -> None:
    if not CLOUD_API_URL:
        return
    month_key = utc_month_key()
    with get_db() as conn:
        totals = get_tile_counts(conn, month_key)
        sent_totals = get_tile_sync_totals(conn, month_key)

    for provider in MAP_TILE_PROVIDERS:
        total = totals.get(provider, 0)
        sent_total = sent_totals.get(provider, 0)
        if total < sent_total:
            with get_db() as conn:
                set_tile_sync_total(conn, month_key, provider, total)
                conn.commit()
            continue
        delta = total - sent_total
        if delta <= 0:
            continue
        if _post_cloud_tile_usage(provider, delta, month_key):
            with get_db() as conn:
                set_tile_sync_total(conn, month_key, provider, total)
                conn.commit()


def tile_usage_sync_worker() -> None:
    while not _heartbeat_stop.is_set():
        try:
            _sync_tile_usage_once()
        except Exception as exc:
            logger.warning(f"Tile usage sync failed: {exc}")
        _heartbeat_stop.wait(max(TILE_USAGE_SYNC_INTERVAL, 5))


@app.route("/api/map-tiles/increment", methods=["POST"])
def api_map_tiles_increment():
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401

    payload = request.get_json(silent=True) or {}
    provider = str(payload.get("provider", "")).strip().lower()
    if not is_valid_provider(provider):
        return jsonify({"error": "provider must be mapbox or esri"}), 400

    count_value = payload.get("count", payload.get("increment_count", 0))
    try:
        count = int(count_value)
    except Exception:
        return jsonify({"error": "count must be an integer"}), 400
    if count <= 0:
        return jsonify({"error": "count must be positive"}), 400

    month_key = str(payload.get("month_key", "")).strip()
    if not _is_valid_month_key(month_key):
        month_key = utc_month_key()

    payload_node_id = str(payload.get("node_id", "")).strip()
    payload_deployment_id = str(payload.get("deployment_id", "")).strip()
    if NODE_ID and payload_node_id and payload_node_id != NODE_ID:
        return jsonify({"error": "node_id mismatch"}), 400
    if DEPLOYMENT_ID and payload_deployment_id and payload_deployment_id != DEPLOYMENT_ID:
        return jsonify({"error": "deployment_id mismatch"}), 400

    with get_db() as conn:
        increment_tile_count(conn, month_key, provider, count)
        conn.commit()

    return jsonify({"ok": True, "provider": provider, "count": count, "month_key": month_key})


@app.route("/api/map-tiles/status", methods=["GET"])
def api_map_tiles_status():
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401

    month_key = utc_month_key()
    thresholds = _build_thresholds()
    local_counts: Dict[str, int] = {}
    with get_db() as conn:
        local_counts = get_tile_counts(conn, month_key)

    cloud_status = _fetch_cloud_status()
    fleet_counts: Dict[str, Optional[int]] = {provider: None for provider in MAP_TILE_PROVIDERS}
    pct: Dict[str, Optional[float]] = {provider: None for provider in MAP_TILE_PROVIDERS}
    blocked: Dict[str, bool] = {provider: False for provider in MAP_TILE_PROVIDERS}
    preferred_provider = None
    recommended_provider = None
    satellite_allowed = None
    warning = None

    if cloud_status:
        fleet = cloud_status.get("fleet") or {}
        for provider in MAP_TILE_PROVIDERS:
            value = fleet.get(provider)
            fleet_counts[provider] = int(value) if value is not None else None
        preferred_provider = cloud_status.get("preferredProvider")
        pct = cloud_status.get("pct") or pct
        blocked = cloud_status.get("blocked") or blocked
        recommended_provider = cloud_status.get("recommendedProvider")
        satellite_allowed = cloud_status.get("satelliteAllowed")
        warning = cloud_status.get("warning")

    if not preferred_provider:
        with get_db() as conn:
            preferred_provider = get_preferred_provider(conn)

    if not preferred_provider or preferred_provider not in MAP_TILE_PROVIDERS:
        preferred_provider = MAP_DEFAULT_PROVIDER if MAP_DEFAULT_PROVIDER in MAP_TILE_PROVIDERS else "esri"

    if preferred_provider and cloud_status:
        with get_db() as conn:
            set_preferred_provider(conn, preferred_provider)
            conn.commit()

    if recommended_provider is None:
        guardrail = build_guardrail_status(
            preferred_provider,
            fleet_counts,
            {"mapbox": MAPBOX_FREE_TILES_PER_MONTH, "esri": ESRI_FREE_TILES_PER_MONTH},
            GUARDRAIL_LIMIT_PCT,
            _month_label(month_key),
        )
        pct = guardrail["pct"]
        blocked = guardrail["blocked"]
        recommended_provider = guardrail["recommended_provider"]
        if guardrail["warning"]:
            warning = guardrail["warning"]

    if not cloud_status:
        warning = warning or "Cloud totals unavailable."
    if satellite_allowed is None:
        satellite_allowed = not (blocked.get("mapbox") and blocked.get("esri"))

    resp = jsonify({
        "month_key": month_key,
        "thresholds": thresholds,
        "local": local_counts,
        "fleet": fleet_counts,
        "pct": pct,
        "blocked": blocked,
        "preferredProvider": preferred_provider,
        "recommendedProvider": recommended_provider,
        "satelliteAllowed": satellite_allowed,
        "warning": warning,
        "providers": MAP_TILE_PROVIDERS_CONFIG,
        "node_id": NODE_ID or None,
        "deployment_id": DEPLOYMENT_ID or None,
    })
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/map-provider/preferred", methods=["POST"])
def api_map_provider_preferred():
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401

    payload = request.get_json(silent=True) or {}
    provider = str(payload.get("provider", "")).strip().lower()
    if not is_valid_provider(provider):
        return jsonify({"error": "provider must be mapbox or esri"}), 400

    cloud_status = _fetch_cloud_status()
    if cloud_status:
        blocked = (cloud_status.get("blocked") or {}).get(provider)
        if blocked:
            recommended = cloud_status.get("recommendedProvider")
            return (
                jsonify(
                    {
                        "error": "provider blocked by guardrail",
                        "recommendedProvider": recommended,
                        "preferredProvider": cloud_status.get("preferredProvider"),
                    }
                ),
                409,
            )

    with get_db() as conn:
        set_preferred_provider(conn, provider)
        conn.commit()

    cloud_resp = _post_cloud_preferred(provider)
    if cloud_resp and cloud_resp.get("status_code") not in (200, 201):
        return (
            jsonify(
                {
                    "error": cloud_resp.get("error", "Cloud rejected preferred provider"),
                    "recommendedProvider": cloud_resp.get("recommendedProvider"),
                    "preferredProvider": cloud_resp.get("preferredProvider"),
                    "warning": cloud_resp.get("warning"),
                }
            ),
            cloud_resp.get("status_code", 409),
        )

    response = {
        "ok": True,
        "provider": provider,
        "warning": None,
    }
    if not cloud_resp and CLOUD_API_URL:
        response["warning"] = "Cloud unavailable; local preference stored."
    return jsonify(response)


@app.route("/metrics", methods=["GET"])
def api_metrics():
    month_key = utc_month_key()
    with get_db() as conn:
        counts = get_tile_counts(conn, month_key)

    thresholds = {
        "mapbox": MAPBOX_FREE_TILES_PER_MONTH,
        "esri": ESRI_FREE_TILES_PER_MONTH,
    }

    def _label_value(value: str) -> str:
        return escape_prom_label_value(value)

    def _format_labels(extra: Dict[str, str]) -> str:
        labels = {}
        labels.update(extra)
        if NODE_ID:
            labels["node_id"] = NODE_ID
        if DEPLOYMENT_ID:
            labels["deployment_id"] = DEPLOYMENT_ID
        if not labels:
            return ""
        pairs = [f'{key}="{_label_value(val)}"' for key, val in labels.items()]
        return "{" + ",".join(pairs) + "}"

    lines = [
        "# HELP map_tiles_month_total Leaflet tile attempts for the current UTC month.",
        "# TYPE map_tiles_month_total counter",
    ]

    for provider in MAP_TILE_PROVIDERS:
        labels = _format_labels({"provider": provider})
        value = counts.get(provider, 0)
        lines.append(f"map_tiles_month_total{labels} {value}")

    lines.extend(
        [
            "# HELP map_tiles_month_pct Percent of free-tier tile budget used for the current UTC month.",
            "# TYPE map_tiles_month_pct gauge",
        ]
    )

    for provider in MAP_TILE_PROVIDERS:
        labels = _format_labels({"provider": provider})
        threshold = thresholds.get(provider, 0)
        value = counts.get(provider, 0)
        pct = (float(value) / float(threshold)) * 100 if threshold else 0.0
        lines.append(f"map_tiles_month_pct{labels} {pct:.4f}")

    resp = make_response("\n".join(lines) + "\n")
    resp.headers["Content-Type"] = "text/plain; version=0.0.4"
    return resp

@app.route("/api/event/start", methods=["POST"])
def api_event_start():
    """Start a new event."""
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    data = request.get_json() or {}
    system_id = canonicalize_system_id(data.get("system_id", "").strip() or SYSTEM_ID)
    event_id = data.get("event_id", "").strip()
    temp_event_id = ""
    location = data.get("location", "").strip()
    note = data.get("note", "").strip()
    ts = data.get("ts")  # optional, for backfill
    
    if not event_id:
        event_id = _generate_temp_event_id(system_id)
        temp_event_id = event_id
    elif _is_temp_event_id(event_id):
        temp_event_id = event_id
    
    # Use current time if not provided
    if ts is None:
        ts = int(time.time() * 1e9)  # nanoseconds
    else:
        ts = int(ts)
    
    # Write to VM
    lines = []
    
    # Write unified ovr_event metric
    lines.append(build_event_line(event_id, system_id, location, 1, ts))
    
    # Write note if provided
    if note:
        lines.append(
            f"{escape_measurement('ovr_event_note')},"
            f"system_id={escape_tag_value(system_id)},"
            f"event_id={escape_tag_value(event_id)} "
            f"active={escape_field_string(note)} {ts}"
        )
    
    success, error = write_to_vm(lines)
    
    if success:
        # Update state in DB
        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO active_events 
                (system_id, event_id, location, started_at)
                VALUES (?, ?, ?, ?)
            """, (system_id, event_id, location or "", ts))
            
            # Add audit note if provided
            if note:
                conn.execute("""
                    INSERT INTO audit_log 
                    (timestamp, action, system_id, event_id, note, success)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (ts, "event_note", system_id, event_id, note, 1))
            
            conn.commit()
        
        log_audit("event_start", system_id, event_id, location, note, True)
        node_key = NODE_ID or SYSTEM_ID
        if node_key:
            _post_cloud_event_node(event_id, node_key)
        return jsonify({
            "success": True,
            "system_id": system_id,
            "event_id": event_id,
            "temp_event_id": temp_event_id or None,
            "location": location,
            "ts": ts
        })
    else:
        log_audit("event_start", system_id, event_id, location, note, False, error)
        return jsonify({"error": error}), 500


@app.route("/api/event/end", methods=["POST"])
def api_event_end():
    """End an event."""
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    data = request.get_json() or {}
    system_id = canonicalize_system_id(data.get("system_id", "").strip() or SYSTEM_ID)
    event_id = data.get("event_id", "").strip()
    ts = data.get("ts")
    
    # If no event_id provided, get current active event
    if not event_id:
        with get_db() as conn:
            row = conn.execute(
                "SELECT event_id FROM active_events WHERE system_id = ?",
                (system_id,)
            ).fetchone()
            if row:
                event_id = row["event_id"]
            else:
                return jsonify({"error": f"No active event for system_id {system_id}"}), 400
    
    if ts is None:
        ts = int(time.time() * 1e9)
    else:
        ts = int(ts)
    
    # Write event end to VM
    location = "-"  # Default if no location found
    with get_db() as conn:
        row = conn.execute(
            "SELECT location FROM active_events WHERE system_id = ? AND event_id = ?",
            (system_id, event_id)
        ).fetchone()
        if row and row["location"]:
            location = row["location"]
    
    line = build_event_line(event_id, system_id, location, 0, ts)
    
    success, error = write_to_vm([line])
    
    if success:
        # Remove from active events
        with get_db() as conn:
            conn.execute(
                "DELETE FROM active_events WHERE system_id = ?",
                (system_id,)
            )
            conn.commit()
        
        log_audit("event_end", system_id, event_id, success=True)
        return jsonify({
            "success": True,
            "system_id": system_id,
            "event_id": event_id,
            "ts": ts
        })
    else:
        log_audit("event_end", system_id, event_id, success=False, error=error)
        return jsonify({"error": error}), 500


@app.route("/api/event/end_all", methods=["POST"])
def api_event_end_all():
    """End ALL loggers for the current event."""
    try:
        if not check_api_key():
            return jsonify({"error": "Invalid API key"}), 401
        
        data = request.get_json() or {}
        event_id = data.get("event_id", "").strip()
        ts = data.get("ts")
        
        if not event_id:
            return jsonify({"error": "event_id required"}), 400
        
        if ts is None:
            ts = int(time.time() * 1e9)
        else:
            ts = int(ts)
        
        # Get all loggers for this event
        with get_db() as conn:
            loggers = conn.execute(
                "SELECT system_id, location FROM active_events WHERE event_id = ?",
                (event_id,)
            ).fetchall()
            
            # Debug: log all active events
            all_active = conn.execute("SELECT event_id, system_id FROM active_events").fetchall()
            logger.info(f"End all request for event_id='{event_id}', found {len(loggers)} loggers")
            logger.info(f"All active events in DB: {[(row['event_id'], row['system_id']) for row in all_active]}")
            
            if not loggers:
                return jsonify({"error": f"No active loggers for event_id '{event_id}'. Active events: {[row['event_id'] for row in all_active]}"}), 400
        
        # Write active=0 for each logger
        lines = []
        for logger_row in loggers:
            system_id = logger_row["system_id"]
            location = logger_row["location"] or "-"
            line = build_event_line(event_id, system_id, location, 0, ts)
            lines.append(line)
        
        success, error = write_to_vm(lines)
        
        if success:
            # Remove all loggers from active_events for this event
            with get_db() as conn:
                conn.execute("DELETE FROM active_events WHERE event_id = ?", (event_id,))
                conn.commit()
            
            log_audit("event_end_all", "-", event_id, success=True)
            node_key = NODE_ID or SYSTEM_ID
            if node_key:
                _post_cloud_event_node_end(event_id, node_key)
            
            # Auto-generate report in background
            try:
                import threading
                def generate_report_background():
                    try:
                        logger.info(f"Auto-generating report for event_id={event_id}")
                        report_result = generate_event_report(event_id)
                        if "error" in report_result:
                            logger.error(f"Report generation failed: {report_result['error']}")
                        else:
                            logger.info(f"Report generated: {report_result.get('html_file', 'N/A')}")
                            json_path = report_result.get("json_file")
                            html_path = report_result.get("html_file")
                            if json_path:
                                try:
                                    with open(json_path, "r") as handle:
                                        report_data = json.load(handle)
                                    report_html = None
                                    if html_path and os.path.exists(html_path):
                                        with open(html_path, "r", encoding="utf-8") as handle:
                                            report_html = handle.read()
                                    _upload_report_json(report_data, report_html)
                                except Exception as exc:
                                    logger.warning(f"Report upload failed: {exc}")
                    except Exception as e:
                        logger.error(f"Report generation exception: {e}")
                
                thread = threading.Thread(target=generate_report_background, daemon=True)
                thread.start()
            except Exception as e:
                logger.warning(f"Could not start report generation thread: {e}")
            
            # Build report URL (configurable via REPORT_BASE_URL env var)
            from urllib.parse import quote
            base_url = os.getenv("REPORT_BASE_URL", "").strip()
            if not base_url:
                base_domain = os.getenv("BASE_DOMAIN", "").strip()
                node_name = NODE_ID or SYSTEM_ID
                if base_domain and node_name:
                    base_url = f"https://{node_name}.{base_domain}"
            if not base_url:
                proto = request.headers.get("X-Forwarded-Proto", request.scheme)
                host = request.headers.get("X-Forwarded-Host", request.host)
                base_url = f"{proto}://{host}"
            base_url = base_url.rstrip("/")
            report_url = f"{base_url}/api/reports/{quote(event_id, safe='')}/html"
            return jsonify({
                "success": True,
                "event_id": event_id,
                "loggers_ended": len(loggers),
                "ts": ts,
                "report_generating": True,
                "report_url": report_url
            })
        else:
            log_audit("event_end_all", "-", event_id, success=False, error=error)
            return jsonify({"error": error}), 500
    
    except Exception as e:
        logger.error(f"Exception in event_end_all: {e}", exc_info=True)
        return jsonify({"error": f"Internal error: {str(e)}"}), 500


@app.route("/api/location/set", methods=["POST"])
def api_location_set():
    """Set/update location for a system."""
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    data = request.get_json() or {}
    system_id = canonicalize_system_id(data.get("system_id", "").strip() or SYSTEM_ID)
    location = data.get("location", "").strip()
    ts = data.get("ts")
    
    if not location:
        return jsonify({"error": "location required"}), 400
    
    if ts is None:
        ts = int(time.time() * 1e9)
    else:
        ts = int(ts)
    
    # Get event_id for this system from active_events
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT event_id FROM active_events WHERE system_id = ?",
                (system_id,)
            ).fetchone()
    except Exception:
        logger.exception(f"Location set DB lookup failed: system_id={system_id}")
        return jsonify({"error": "Database error retrieving active event"}), 500
    
    if not row:
        logger.warning(f"Location set failed: no active event for system_id={system_id}")
        return jsonify({"error": "No active event for this logger"}), 400
    event_id = row["event_id"]
    
    # Write to VM (unified metric)
    line = build_event_line(event_id, system_id, location, 1, ts)
    
    success, error = write_to_vm([line])
    
    if success:
        # Update state - store location in active_events table
        try:
            with get_db() as conn:
                conn.execute("""
                    UPDATE active_events 
                    SET location = ?
                    WHERE system_id = ?
                """, (location, system_id))
                conn.commit()
        except Exception:
            logger.exception(f"Failed to update location in DB: system_id={system_id}, event_id={event_id}")
            return jsonify({"error": "Failed to update location in database"}), 500
        
        log_audit("location_set", system_id, location=location, success=True)
        return jsonify({
            "success": True,
            "system_id": system_id,
            "location": location,
            "ts": ts
        })
    else:
        logger.error(f"Location set VM write failed: system_id={system_id}, event_id={event_id}, error={error}")
        log_audit("location_set", system_id, location=location, success=False, error=error)
        return jsonify({"error": error}), 500


@app.route("/api/location/clear", methods=["POST"])
def api_location_clear():
    """Clear/stop location tracking for a system."""
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    data = request.get_json() or {}
    system_id = canonicalize_system_id(data.get("system_id", "").strip())
    
    if not system_id:
        return jsonify({"error": "system_id required"}), 400
    
    ts = int(time.time() * 1e9)
    
    # Get event_id and location from active_events before clearing
    with get_db() as conn:
        row = conn.execute(
            "SELECT event_id, location FROM active_events WHERE system_id = ?",
            (system_id,)
        ).fetchone()
        
        if row:
            event_id = row["event_id"]
            location = row["location"] or "-"
        else:
            return jsonify({"error": f"No active event for system_id {system_id}"}), 400
    
    # Write 0 to VM to indicate inactive (unified metric)
    line = build_event_line(event_id, system_id, location, 0, ts)
    
    success, error = write_to_vm([line])
    
    if success:
        # Remove logger from active_events (end this logger's participation in the event)
        with get_db() as conn:
            conn.execute("DELETE FROM active_events WHERE system_id = ?", (system_id,))
            conn.commit()
        
        log_audit("location_clear", system_id, success=True)
        return jsonify({
            "success": True,
            "system_id": system_id
        })
    else:
        log_audit("location_clear", system_id, success=False, error=error)
        return jsonify({"error": error}), 500


@app.route("/api/note", methods=["POST"])
def api_note():
    """Add a note/annotation."""
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    data = request.get_json() or {}
    system_id = canonicalize_system_id(data.get("system_id", "").strip() or SYSTEM_ID)
    event_id = data.get("event_id", "").strip()
    msg = data.get("msg", "").strip()
    ts = data.get("ts")
    
    if not msg:
        return jsonify({"error": "msg required"}), 400
    
    # If no event_id, try to get current active event
    if not event_id:
        with get_db() as conn:
            row = conn.execute(
                "SELECT event_id FROM active_events WHERE system_id = ?",
                (system_id,)
            ).fetchone()
            if row:
                event_id = row["event_id"]
            # If still no event_id, use "general"
            if not event_id:
                event_id = "general"
    
    if ts is None:
        ts = int(time.time() * 1e9)
    else:
        ts = int(ts)
    
    # Write to VM (field name "active" so VM creates "ovr_event_note_active" not "ovr_event_note_text")
    line = (
        f"{escape_measurement('ovr_event_note')},"
        f"system_id={escape_tag_value(system_id)},"
        f"event_id={escape_tag_value(event_id)} "
        f"active={escape_field_string(msg)} {ts}"
    )
    
    success, error = write_to_vm([line])
    
    if success:
        log_audit("note", system_id, event_id, note=msg, success=True)
        return jsonify({
            "success": True,
            "system_id": system_id,
            "event_id": event_id,
            "msg": msg,
            "ts": ts
        })
    else:
        log_audit("note", system_id, event_id, note=msg, success=False, error=error)
        return jsonify({"error": error}), 500


@app.route("/api/notes", methods=["GET"])
def api_notes():
    """Get notes for an event or system."""
    event_id = request.args.get("event_id", "").strip()
    system_id = canonicalize_system_id(request.args.get("system_id", "").strip())
    limit = int(request.args.get("limit", "50"))
    
    with get_db() as conn:
        if event_id:
            notes = conn.execute("""
                SELECT id, timestamp, system_id, event_id, note
                FROM audit_log
                WHERE action = 'note' AND event_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (event_id, limit)).fetchall()
        elif system_id:
            notes = conn.execute("""
                SELECT id, timestamp, system_id, event_id, note
                FROM audit_log
                WHERE action = 'note' AND system_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (system_id, limit)).fetchall()
        else:
            notes = conn.execute("""
                SELECT id, timestamp, system_id, event_id, note
                FROM audit_log
                WHERE action = 'note'
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,)).fetchall()
        
        return jsonify({
            "notes": [dict(n) for n in notes]
        })


@app.route("/api/audit/delete", methods=["POST"])
def api_audit_delete():
    """Delete an audit log entry by id or note text."""
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    data = request.get_json() or {}
    system_id = canonicalize_system_id(data.get("system_id", "").strip())
    event_id = data.get("event_id", "").strip()
    note_text = data.get("note_text", "").strip()
    note_id_raw = data.get("note_id")
    note_id = None
    if note_id_raw is not None and str(note_id_raw).strip() != "":
        try:
            note_id = int(note_id_raw)
        except (ValueError, TypeError):
            return jsonify({"error": "note_id must be an integer"}), 400
    
    try:
        if note_id is not None:
            logger.info(f"Deleting note by id: id={note_id}, system_id={system_id}, event_id={event_id}")
            with get_db() as conn:
                cursor = conn.execute(
                    "DELETE FROM audit_log WHERE id = ? AND action IN ('note', 'event_note')",
                    (note_id,)
                )
                conn.commit()
                
                if cursor.rowcount == 0:
                    logger.warning(f"Note id not found: id={note_id}")
                    return jsonify({"error": "Note not found"}), 404
                
                return jsonify({
                    "success": True,
                    "deleted_count": cursor.rowcount
                })
        
        if not note_text:
            return jsonify({"error": "note_id or note_text required"}), 400
        
        logger.info(f"Deleting note by text: system_id={system_id}, event_id={event_id}, note_text={note_text[:50]}...")
        with get_db() as conn:
            cursor = conn.execute(
                "DELETE FROM audit_log WHERE system_id = ? AND event_id = ? AND note = ? AND action IN ('note', 'event_note')",
                (system_id, event_id, note_text)
            )
            conn.commit()
            
            if cursor.rowcount == 0:
                logger.warning(f"Note not found: system_id={system_id}, event_id={event_id}")
                return jsonify({"error": "Note not found"}), 404
            
            return jsonify({
                "success": True,
                "deleted_count": cursor.rowcount
            })
    except Exception as e:
        logger.exception(f"Error deleting audit entry: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/reports/generate", methods=["POST"])
def api_reports_generate():
    """Generate a report for a completed event."""
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    data = request.get_json() or {}
    event_id = data.get("event_id", "").strip()
    
    if not event_id:
        return jsonify({"error": "event_id required"}), 400
    
    try:
        result = generate_event_report(event_id)
        if "error" not in result:
            json_path = result.get("json_file")
            html_path = result.get("html_file")
            if json_path:
                try:
                    with open(json_path, "r") as handle:
                        report_data = json.load(handle)
                    report_html = None
                    if html_path and os.path.exists(html_path):
                        with open(html_path, "r", encoding="utf-8") as handle:
                            report_html = handle.read()
                    _upload_report_json(report_data, report_html)
                except Exception as exc:
                    logger.warning(f"Report upload failed: {exc}")
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error generating report: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/reports", methods=["GET"])
def api_reports_list():
    """List all available reports."""
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    if not os.path.exists(REPORTS_PATH):
        return jsonify({"reports": []})
    
    reports = []
    for dir_name in os.listdir(REPORTS_PATH):
        dir_path = os.path.join(REPORTS_PATH, dir_name)
        if os.path.isdir(dir_path):
            json_file = os.path.join(dir_path, "data.json")
            if os.path.exists(json_file):
                try:
                    with open(json_file, "r") as f:
                        data = json.load(f)
                        reports.append({
                            "event_id": data.get("event_id"),
                            "generated_at": data.get("generated_at"),
                            "duration_seconds": data.get("duration_seconds"),
                            "loggers": list(data.get("loggers", {}).keys()),
                            "report_dir": dir_name
                        })
                except Exception as e:
                    logger.warning(f"Could not load report {json_file}: {e}")
    
    # Sort by generated_at descending (newest first)
    reports.sort(key=lambda x: x.get("generated_at", 0), reverse=True)
    
    return jsonify({"reports": reports})


@app.route("/api/reports/<event_id>", methods=["GET"])
def api_reports_get(event_id):
    """Get a specific report's JSON data."""
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    # Find the report directory for this event_id
    if not os.path.exists(REPORTS_PATH):
        return jsonify({"error": "Reports directory not found"}), 404
    
    # Sanitize event_id for filesystem lookup
    safe_event_id = event_id.replace("'", "").replace('"', "").replace("/", "_").replace("\\", "_").replace(" ", "_")
    
    # Find matching report directory (could be multiple, get latest)
    matching_dirs = [d for d in os.listdir(REPORTS_PATH) if d.startswith(f"event_{safe_event_id}_")]
    
    if not matching_dirs:
        return jsonify({"error": "Report not found"}), 404
    
    # Get the latest one (sorted by timestamp in dirname)
    matching_dirs.sort(reverse=True)
    report_dir = os.path.join(REPORTS_PATH, matching_dirs[0])
    json_file = os.path.join(report_dir, "data.json")
    
    if not os.path.exists(json_file):
        return jsonify({"error": "Report data file not found"}), 404
    
    try:
        with open(json_file, "r") as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        logger.error(f"Error reading report: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/reports/<event_id>/html", methods=["GET"])
def api_reports_html(event_id):
    """Get a specific report as HTML."""
    # No API key check - this is meant to be viewable
    
    # Find the report directory for this event_id
    if not os.path.exists(REPORTS_PATH):
        return "Reports directory not found", 404
    
    # Sanitize event_id for filesystem lookup
    safe_event_id = event_id.replace("'", "").replace('"', "").replace("/", "_").replace("\\", "_").replace(" ", "_")
    
    # Find matching report directory (could be multiple, get latest)
    matching_dirs = [d for d in os.listdir(REPORTS_PATH) if d.startswith(f"event_{safe_event_id}_")]
    
    if not matching_dirs:
        return "Report not found", 404
    
    # Get the latest one (sorted by timestamp in dirname)
    matching_dirs.sort(reverse=True)
    report_dir = os.path.join(REPORTS_PATH, matching_dirs[0])
    html_file = os.path.join(report_dir, "report.html")
    
    if not os.path.exists(html_file):
        return "HTML report not found", 404
    
    try:
        with open(html_file, "r", encoding="utf-8") as f:
            html_content = f.read()
        return html_content, 200, {"Content-Type": "text/html; charset=utf-8"}
    except Exception as e:
        logger.error(f"Error reading HTML report: {e}")
        return f"Error: {e}", 500


@app.route("/api/status", methods=["GET"])
def api_status():
    """Get current status for a system or all systems."""
    system_id = canonicalize_system_id(request.args.get("system_id", "").strip())
    
    with get_db() as conn:
        if system_id:
            # Get specific system status
            active_event = conn.execute(
                "SELECT event_id, location, started_at FROM active_events WHERE system_id = ?",
                (system_id,)
            ).fetchone()
            
            recent_logs = conn.execute("""
                SELECT timestamp, action, event_id, location, note, success, error
                FROM audit_log
                WHERE system_id = ?
                ORDER BY timestamp DESC
                LIMIT 10
            """, (system_id,)).fetchall()
            
            images = conn.execute("""
                SELECT id, filename, original_filename, event_id, location, caption, timestamp, file_size
                FROM images
                WHERE system_id = ?
                ORDER BY timestamp DESC
                LIMIT 20
            """, (system_id,)).fetchall()
            
            return jsonify({
                "system_id": system_id,
                "active_event": dict(active_event) if active_event else None,
                "recent_logs": [dict(r) for r in recent_logs],
                "images": [dict(r) for r in images]
            })
        else:
            # Get all systems - return event_id grouping
            active_events = conn.execute(
                "SELECT system_id, event_id, location, started_at FROM active_events ORDER BY event_id, system_id"
            ).fetchall()
            
            # Group by event_id for easier UI consumption
            events_grouped = {}
            for row in active_events:
                event_id = row["event_id"]
                if event_id not in events_grouped:
                    events_grouped[event_id] = []
                events_grouped[event_id].append({
                    "system_id": row["system_id"],
                    "location": row["location"],
                    "started_at": row["started_at"]
                })
            
            recent_logs = conn.execute("""
                SELECT timestamp, action, system_id, event_id, location, note, success, error
                FROM audit_log
                ORDER BY timestamp DESC
                LIMIT 50
            """).fetchall()
            
            return jsonify({
                "active_events": [dict(r) for r in active_events],
                "events_grouped": events_grouped,
                "recent_logs": [dict(r) for r in recent_logs]
            })


@app.route("/api/summary", methods=["GET"])
def api_summary():
    """Get summary metrics for the current system."""
    system_id = canonicalize_system_id(request.args.get("system_id", "").strip() or SYSTEM_ID)
    label = escape_prom_label_value(system_id)

    soc = vm_query_scalar(
        f'victron_battery_soc{{system_id="{label}"}}'
    )
    pin = vm_query_scalar(
        f'victron_ac_in_power{{system_id="{label}"}}'
    )
    pout = vm_query_scalar(
        f'victron_ac_out_power{{system_id="{label}"}}'
    )
    alarms_series = vm_query_vector(
        f'max_over_time({{__name__=~"victron_alarm_.*",system_id="{label}"}}[5m])'
    )
    alerts = []
    for series in alarms_series:
        value = _vm_value_to_float(series.get("value"))
        if value is None or value < 0.5:
            continue
        metric = series.get("metric", {})
        name = metric.get("name") or metric.get("__name__", "alarm")
        phase = metric.get("phase")
        if phase and phase not in ("-", ""):
            name = f"{name} {phase}"
        alerts.append(name)

    unique_alerts = sorted(set(alerts))
    resp = jsonify({
        "system_id": system_id,
        "soc": soc,
        "pin": pin,
        "pout": pout,
        "alerts": unique_alerts,
        "alerts_count": len(unique_alerts)
    })
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/admin/cleanup", methods=["POST"])
def api_admin_cleanup():
    """Clear all active events (admin use only)."""
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    data = request.get_json() or {}
    system_id = canonicalize_system_id(data.get("system_id", "").strip())
    
    with get_db() as conn:
        if system_id:
            # Clear specific system
            conn.execute("DELETE FROM active_events WHERE system_id = ?", (system_id,))
            conn.commit()
            return jsonify({"success": True, "message": f"Cleared active events for {system_id}"})
        else:
            # Clear all
            conn.execute("DELETE FROM active_events")
            conn.commit()
            return jsonify({"success": True, "message": "Cleared all active events"})


# ============================================================================
# Image Upload API
# ============================================================================

def allowed_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/api/image/upload", methods=["POST"])
def api_image_upload():
    """Upload an image with metadata."""
    try:
        if not check_api_key():
            return jsonify({"error": "Invalid API key"}), 401
        
        if 'image' not in request.files:
            logger.warning("Image upload failed: No image file in request")
            return jsonify({"error": "No image file provided"}), 400
        
        file = request.files['image']
        
        if file.filename == '':
            logger.warning("Image upload failed: Empty filename")
            return jsonify({"error": "No file selected"}), 400
        
        if not allowed_file(file.filename):
            logger.warning(f"Image upload failed: Invalid file type {file.filename}")
            return jsonify({"error": f"File type not allowed. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"}), 400
        
        system_id = canonicalize_system_id(request.form.get('system_id', '').strip() or SYSTEM_ID)
        event_id = request.form.get('event_id', '').strip()
        location = request.form.get('location', '').strip()
        caption = request.form.get('caption', '').strip()
        
        file_content = file.read()
        file_size = len(file_content)
        
        if file_size > MAX_IMAGE_SIZE:
            logger.warning(f"Image upload failed: File too large ({file_size} bytes)")
            return jsonify({"error": f"Image too large (max {MAX_IMAGE_SIZE // (1024*1024)}MB)"}), 400
        
        file_hash = hashlib.sha256(file_content).hexdigest()[:16]
        ts = int(time.time())
        ext = file.filename.rsplit('.', 1)[1].lower()
        filename = f"{system_id}_{ts}_{file_hash}.{ext}"
        
        os.makedirs(IMAGES_PATH, exist_ok=True)
        filepath = os.path.join(IMAGES_PATH, filename)
        
        try:
            with open(filepath, 'wb') as f:
                f.write(file_content)
        except Exception as e:
            logger.error(f"Failed to save image {filename}: {e}")
            return jsonify({"error": f"Failed to save image: {str(e)}"}), 500
        
        ts_ns = ts * int(1e9)
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO images 
                    (filename, original_filename, system_id, event_id, location, caption, timestamp, file_size, content_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    filename,
                    secure_filename(file.filename),
                    system_id,
                    event_id or None,
                    location or None,
                    caption or None,
                    ts_ns,
                    file_size,
                    file.content_type
                ))
                image_id = cursor.lastrowid
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to save image metadata: {e}")
            try:
                os.remove(filepath)
            except:
                pass
            return jsonify({"error": f"Failed to save metadata: {str(e)}"}), 500
        
        log_audit("image_upload", system_id, event_id, location, caption, True)
        logger.info(f"Image uploaded: {filename} ({file_size} bytes) for {system_id}")
        
        return jsonify({
            "success": True,
            "id": image_id,
            "filename": filename,
            "system_id": system_id,
            "event_id": event_id,
            "size": file_size,
            "timestamp": ts_ns
        })
    
    except Exception as e:
        logger.error(f"Unexpected error in image upload: {e}", exc_info=True)
        return jsonify({"error": f"Server error: {str(e)}"}), 500


@app.route("/api/images", methods=["GET"])
def api_images_list():
    """List images with optional filters."""
    system_id = canonicalize_system_id(request.args.get("system_id", "").strip())
    event_id = request.args.get("event_id", "").strip()
    limit = int(request.args.get("limit", "50"))
    
    with get_db() as conn:
        if system_id and event_id:
            images = conn.execute("""
                SELECT id, filename, original_filename, system_id, event_id, location, caption, timestamp, file_size
                FROM images
                WHERE system_id = ? AND event_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (system_id, event_id, limit)).fetchall()
        elif system_id:
            images = conn.execute("""
                SELECT id, filename, original_filename, system_id, event_id, location, caption, timestamp, file_size
                FROM images
                WHERE system_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (system_id, limit)).fetchall()
        else:
            images = conn.execute("""
                SELECT id, filename, original_filename, system_id, event_id, location, caption, timestamp, file_size
                FROM images
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,)).fetchall()
    
    return jsonify({
        "images": [dict(img) for img in images]
    })


@app.route("/api/image/<int:image_id>/caption", methods=["POST"])
def api_image_caption_update(image_id):
    """Update an image caption."""
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    data = request.get_json(silent=True) or {}
    caption = (data.get("caption") or "").strip()
    
    with get_db() as conn:
        image = conn.execute(
            "SELECT system_id, event_id, location FROM images WHERE id = ?",
            (image_id,)
        ).fetchone()
        
        if not image:
            return jsonify({"error": "Image not found"}), 404
        
        conn.execute(
            "UPDATE images SET caption = ? WHERE id = ?",
            (caption or None, image_id)
        )
        conn.commit()
    
    log_audit("image_caption_update", image["system_id"], image["event_id"], image["location"], caption, True)
    return jsonify({"success": True, "id": image_id, "caption": caption})


@app.route("/api/image/<int:image_id>", methods=["DELETE"])
def api_image_delete(image_id):
    """Delete an image."""
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    with get_db() as conn:
        image = conn.execute(
            "SELECT filename, system_id FROM images WHERE id = ?",
            (image_id,)
        ).fetchone()
        
        if not image:
            return jsonify({"error": "Image not found"}), 404
        
        filename = image["filename"]
        system_id = image["system_id"]
        
        filepath = os.path.join(IMAGES_PATH, filename)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception as e:
            logger.error(f"Failed to delete image file: {e}")
        
        conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
        conn.commit()
    
    log_audit("image_delete", system_id, success=True)
    
    return jsonify({"success": True, "id": image_id})


@app.route("/images/<filename>")
def serve_image(filename):
    """Serve image file."""
    if '..' in filename or '/' in filename:
        return "Invalid filename", 400
    
    return send_from_directory(IMAGES_PATH, filename)


@app.route("/sw.js")
def service_worker():
    """Service worker for map tile caching."""
    tile_prefixes = MAP_TILE_CACHE_PREFIXES
    cache_enabled = len(tile_prefixes) > 0
    
    js = f"""
const CACHE_NAME = "ovr-map-tiles-v1";
const TILE_PREFIXES = {json.dumps(tile_prefixes)};
const CACHE_ENABLED = {json.dumps(cache_enabled)};

self.addEventListener("install", (event) => {{
  event.waitUntil(caches.open(CACHE_NAME));
  self.skipWaiting();
}});

self.addEventListener("activate", (event) => {{
  event.waitUntil(self.clients.claim());
}});

self.addEventListener("fetch", (event) => {{
  if (event.request.method !== "GET") return;
  if (!CACHE_ENABLED) return;
  const url = event.request.url || "";
  if (!TILE_PREFIXES.some((prefix) => url.startsWith(prefix))) return;

  event.respondWith(
    caches.open(CACHE_NAME).then((cache) =>
      cache.match(event.request).then((cached) => {{
        if (cached) return cached;
        return fetch(event.request).then((response) => {{
          cache.put(event.request, response.clone());
          return response;
        }});
      }})
    )
  );
}});
"""
    resp = make_response(js)
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ============================================================================
# GX Device Control API
# ============================================================================

@app.route("/api/gx/gps", methods=["GET"])
def api_gx_gps():
    """Get last known GPS coordinates for a system."""
    system_id = canonicalize_system_id(request.args.get("system_id", "").strip() or SYSTEM_ID)
    refresh = request.args.get("refresh", "").strip().lower() in ("1", "true", "yes", "y")

    if not refresh:
        cached = get_cached_gps(system_id)
        if cached:
            return jsonify({
                "system_id": system_id,
                "latitude": cached["latitude"],
                "longitude": cached["longitude"],
                "updated_at": cached["updated_at"],
                "cached": True
            })

    lat, lon = get_gps_from_vm(system_id)
    if lat is None or lon is None:
        return jsonify({
            "system_id": system_id,
            "latitude": None,
            "longitude": None,
            "updated_at": None,
            "cached": False,
            "error": "GPS not available"
        })

    updated_at = int(time.time() * 1e9)
    set_cached_gps(system_id, lat, lon, updated_at)
    return jsonify({
        "system_id": system_id,
        "latitude": lat,
        "longitude": lon,
        "updated_at": updated_at,
        "cached": False
    })


@app.route("/api/gx/settings", methods=["GET"])
def api_gx_settings_get():
    """Get current GX device settings from VictoriaMetrics."""
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    system_id = canonicalize_system_id(request.args.get("system_id", "").strip() or SYSTEM_ID)
    label = escape_prom_label_value(system_id)
    
    # Map settings to their VictoriaMetrics metric names
    metric_map = {
        "battery_charge_current": "victron_dc_max_charge_current",
        "inverter_mode": "victron_mode_mode",
        "ac_input_current_limit": "victron_ac_in_current_limit",
        "inverter_output_voltage": "victron_settings_inverter_output_voltage"
    }
    
    settings = {}
    for key, metric_name in metric_map.items():
        try:
            # Query VictoriaMetrics for the latest value
            query = f'{metric_name}{{job=~"victron|gx_fast|gx_slow",system_id="{label}"}}'
            resp = requests.get(f"{VM_QUERY_URL}/api/v1/query", params={"query": query}, timeout=5)
            
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("data", {}).get("result", [])
                if results:
                    latest = max(results, key=lambda r: float(r["value"][0]))
                    value = float(latest["value"][1])
                    updated_at = int(float(latest["value"][0]) * 1e9)
                    settings[key] = {
                        "value": value,
                        "description": GX_DBUS_PATHS[key]["description"],
                        "updated_at": updated_at
                    }
                else:
                    settings[key] = {
                        "value": None,
                        "description": GX_DBUS_PATHS[key]["description"],
                        "updated_at": None
                    }
            else:
                settings[key] = {
                    "value": None,
                    "description": GX_DBUS_PATHS[key]["description"],
                    "updated_at": None
                }
        except Exception as e:
            logger.error(f"Error querying VM for {key}: {e}")
            settings[key] = {
                "value": None,
                "description": GX_DBUS_PATHS[key]["description"],
                "updated_at": None
            }
    
    return jsonify(settings)


@app.route("/api/gx/setting", methods=["POST"])
def api_gx_setting_set():
    """Set a GX device setting."""
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    data = request.get_json()
    setting_name = data.get("setting")
    value = data.get("value")
    
    if not setting_name or value is None:
        return jsonify({"error": "setting and value required"}), 400
    
    if setting_name not in GX_DBUS_PATHS:
        return jsonify({"error": f"Unknown setting: {setting_name}"}), 400
    
    config = GX_DBUS_PATHS[setting_name]
    
    # Handle inverter mode special case (convert string to int)
    if setting_name == "inverter_mode" and isinstance(value, str):
        if value in config["values"]:
            value = config["values"][value]
        else:
            return jsonify({"error": f"Invalid mode. Valid: {list(config['values'].keys())}"}), 400
    
    success = ssh_dbus_write(config["service"], config["path"], value, config["type"])
    
    if success:
        return jsonify({"message": f"Successfully set {setting_name} to {value}", "setting": setting_name, "value": value})
    else:
        return jsonify({"error": "Failed to write to D-Bus"}), 500


# ============================================================================
# Web UI
# ============================================================================

WEB_UI_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
    <title>Overdrive Event Logger</title>
    <link
        rel="stylesheet"
        href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
        integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
        crossorigin=""
    />
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #f5f5f5;
            padding: 10px;
            font-size: 16px;
        }
        .container {
            max-width: 600px;
            margin: 0 auto;
            background: white;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .events-layout {
            display: block;
        }
        .note-entry {
            margin-bottom: 20px;
        }
        .note-entry-desktop {
            display: none;
        }
        .note-entry-mobile {
            display: block;
        }
        .note-button-desktop {
            display: none;
        }
        h1 {
            font-size: 24px;
            margin-bottom: 20px;
            color: #333;
            display: none; /* Hidden - using logo instead */
        }
        .logo-header {
            text-align: center;
            margin-bottom: 20px;
        }
        .logo-header img {
            max-width: 280px;
            height: auto;
            filter: drop-shadow(0 2px 8px rgba(0,0,0,0.15));
        }
        .form-group {
            margin-bottom: 15px;
        }
        .logger-entry {
            background: #f9fafb;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            padding: 12px;
            margin-bottom: 12px;
        }
        .active-logger-item {
            background: #f0fdf4;
            padding: 12px;
            border-radius: 6px;
            margin-bottom: 10px;
            border: 1px solid #86efac;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .active-logger-info {
            flex: 1;
        }
        .active-logger-label {
            font-weight: 600;
            color: #059669;
            font-size: 0.95em;
        }
        .active-logger-location {
            color: #6b7280;
            font-size: 0.9em;
            margin-top: 4px;
        }
        .btn-remove-logger {
            background: #ef4444;
            color: white;
            border: none;
            padding: 6px 12px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.9em;
            transition: background 0.2s;
        }
        .btn-remove-logger:hover {
            background: #dc2626;
        }
        .logger-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
        }
        .logger-header label {
            margin: 0;
            flex: 1;
        }
        .logger-title {
            font-weight: 600;
            color: #1f2937;
            font-size: 14px;
        }
        .remove-logger {
            background: #ef4444;
            color: white;
            border: none;
            padding: 4px 12px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 12px;
            font-weight: 600;
        }
        .remove-logger:hover {
            background: #dc2626;
        }
        .add-logger-btn {
            background: #10b981;
            color: white;
            border: none;
            padding: 10px 16px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
            width: 100%;
            margin-bottom: 16px;
        }
        .add-logger-btn:hover {
            background: #059669;
        }
        .form-group {
            margin-bottom: 15px;
        }
        label {
            display: block;
            font-weight: 600;
            margin-bottom: 5px;
            color: #555;
            font-size: 14px;
        }
        input, select, textarea {
            width: 100%;
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 16px;
        }
        textarea {
            resize: vertical;
            min-height: 60px;
        }
        .btn {
            width: 100%;
            padding: 14px;
            margin-bottom: 10px;
            border: none;
            border-radius: 4px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: opacity 0.2s;
        }
        .btn:active { opacity: 0.7; }
        .btn-start { background: #10b981; color: white; }
        .btn-end { background: #ef4444; color: white; }
        .btn-location { background: #3b82f6; color: white; }
        .btn-note { background: #8b5cf6; color: white; }
        .status {
            margin-top: 20px;
            padding: 15px;
            border-radius: 4px;
            font-size: 14px;
        }
        .status.success {
            background: #d1fae5;
            color: #065f46;
            border: 1px solid #10b981;
        }
        .status.error {
            background: #fee2e2;
            color: #991b1b;
            border: 1px solid #ef4444;
        }
        .info-box {
            background: #f3f4f6;
            padding: 15px;
            border-radius: 4px;
            margin-bottom: 20px;
            font-size: 14px;
        }
        .info-box h3 {
            font-size: 16px;
            margin-bottom: 8px;
            color: #374151;
        }
        .info-item {
            padding: 5px 0;
            color: #6b7280;
        }
        .tabs {
            display: flex;
            margin-bottom: 20px;
            border-bottom: 2px solid #e5e7eb;
        }
        .tab {
            padding: 10px 20px;
            background: none;
            border: none;
            cursor: pointer;
            font-size: 16px;
            color: #6b7280;
            border-bottom: 3px solid transparent;
            transition: all 0.2s;
            flex: 1;
            text-align: center;
        }
        .tab.active {
            color: #3b82f6;
            border-bottom-color: #3b82f6;
            font-weight: 600;
        }
        .tab-content {
            display: none;
        }
        .tab-content.active {
            display: block;
        }
        .control-item {
            background: #f9fafb;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 15px;
            border: 1px solid #e5e7eb;
        }
        .control-grid {
            display: grid;
            gap: 15px;
        }
        .control-grid .control-item {
            margin-bottom: 0;
        }
        .control-item label {
            display: block;
            font-size: 14px;
            font-weight: 600;
            margin-bottom: 8px;
            color: #374151;
        }
        .control-item .description {
            font-size: 12px;
            color: #6b7280;
            margin-bottom: 10px;
        }
        .control-item .current-value {
            font-size: 13px;
            color: #059669;
            margin-bottom: 10px;
            font-weight: 500;
        }
        .gx-status {
            margin: 12px 0 16px;
            padding: 10px 12px;
            border-radius: 6px;
            font-size: 13px;
            border: 1px solid #e5e7eb;
            background: #f9fafb;
            color: #374151;
        }
        .gx-status.gx-info {
            background: #eff6ff;
            border-color: #bfdbfe;
            color: #1e3a8a;
        }
        .gx-status.gx-warn {
            background: #fef9c3;
            border-color: #fde047;
            color: #854d0e;
        }
        .gx-status.gx-success {
            background: #dcfce7;
            border-color: #86efac;
            color: #166534;
        }
        .control-row {
            display: flex;
            gap: 10px;
            align-items: center;
        }
        .control-row input {
            flex: 1;
        }
        .control-input {
            width: 100%;
            padding: 14px;
            font-size: 16px;
            border: 2px solid #d1d5db;
            border-radius: 6px;
            box-sizing: border-box;
            transition: border-color 0.2s;
        }
        .control-input:focus {
            outline: none;
            border-color: #3b82f6;
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
        }
        .control-row button {
            flex-shrink: 0;
        }
        .info-item strong {
            color: #111827;
        }
        .top-summary {
            display: grid;
            grid-template-columns: 1fr;
            gap: 10px;
            margin-top: -10px;
            margin-bottom: 16px;
        }
        @media (min-width: 640px) {
            .top-summary {
                grid-template-columns: 1fr 1fr;
            }
        }
        @media (min-width: 1024px) {
            body {
                padding: 20px;
            }
            .container {
                max-width: 1200px;
                padding: 24px 28px;
            }
            .top-summary {
                grid-template-columns: repeat(4, 1fr);
            }
            .events-layout {
                display: grid;
                grid-template-columns: 1.2fr 0.8fr;
                gap: 24px;
                align-items: start;
            }
            .note-entry-desktop {
                display: block;
            }
            .note-entry-mobile {
                display: none;
            }
            .note-button-desktop {
                display: inline-block;
            }
            .note-button-mobile {
                display: none;
            }
            .control-grid {
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }
        }
        .summary-item {
            background: #f9fafb;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            padding: 12px 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 14px;
            gap: 10px;
        }
        .summary-item.soc-good {
            background: #dcfce7;
            border-color: #86efac;
        }
        .summary-item.soc-warn {
            background: #fef9c3;
            border-color: #fde047;
        }
        .summary-item.soc-bad {
            background: #fee2e2;
            border-color: #fecaca;
        }
        .summary-label {
            color: #6b7280;
        }
        .summary-value {
            color: #111827;
            font-weight: 600;
            text-align: right;
        }
        .summary-alert {
            color: #b91c1c;
        }
        .summary-item.alert-bad {
            background: #fee2e2;
            border-color: #fecaca;
        }
        .summary-item.alert-bad .summary-value {
            color: #b91c1c;
        }
        .map-container {
            height: 320px;
            border-radius: 10px;
            overflow: hidden;
            border: 1px solid #e5e7eb;
            margin-top: 10px;
        }
        .map-actions {
            display: flex;
            gap: 10px;
            margin-top: 12px;
            align-items: center;
            flex-wrap: wrap;
        }
        .map-actions a {
            color: #1d4ed8;
            font-weight: 600;
            text-decoration: none;
        }
        .map-meta {
            font-size: 12px;
            color: #6b7280;
            margin-top: 8px;
        }
    </style>
    <script
        src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
        crossorigin=""
    ></script>
</head>
<body>
    <div class="container">
        <div class="logo-header">
            <img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 640'%3E%3Crect fill='%23000' width='640' height='640'/%3E%3Ctext x='50%25' y='35%25' fill='%23fff' font-size='180' font-weight='900' text-anchor='middle' font-family='Arial, sans-serif'%3EOVR%3C/text%3E%3Crect x='82' y='275' width='476' height='20' fill='%2340C463'/%3E%3Ctext x='50%25' y='75%25' fill='%23fff' font-size='180' font-weight='900' text-anchor='middle' font-family='Arial, sans-serif'%3EDRV%3C/text%3E%3Ctext x='50%25' y='87%25' fill='%2340C463' font-size='32' font-weight='600' text-anchor='middle' font-family='Arial, sans-serif'%3EENERGY SOLUTIONS%3C/text%3E%3C/svg%3E" 
                 alt="Overdrive Energy Solutions" 
                 onerror="this.style.display='none'; document.querySelector('h1').style.display='block';">
        </div>
        <h1>OVR Event Marker</h1>
        <div class="top-summary">
            <div class="summary-item">
                <span class="summary-label">SOC</span>
                <span class="summary-value" id="summary_soc">--</span>
            </div>
            <div class="summary-item">
                <span class="summary-label">Alerts</span>
                <span class="summary-value summary-alert" id="summary_alerts">--</span>
            </div>
            <div class="summary-item">
                <span class="summary-label">P<sub>in</sub></span>
                <span class="summary-value" id="summary_pin">--</span>
            </div>
            <div class="summary-item">
                <span class="summary-label">P<sub>out</sub></span>
                <span class="summary-value" id="summary_pout">--</span>
            </div>
        </div>
        
        <div class="tabs">
            <button class="tab active" onclick="switchTab('events')">Events</button>
            <button class="tab" onclick="switchTab('control')">GX Control</button>
            <button class="tab" onclick="switchTab('map')">Map</button>
        </div>
        
        <div id="tab-events" class="tab-content active">
            <div class="info-box" id="statusBox" style="display:none;">
                <h3>Active Event: <span id="statusEventId">-</span></h3>
                <div id="activeLoggersContainer"></div>
            </div>

            <div class="events-layout">
                <div class="events-main">
        
        <div class="form-group">
            <label for="eventId">Site *</label>
            <input type="text" id="eventId" placeholder="e.g., warehouse, customer_site_a">
        </div>
        
        <div id="loggersContainer">
            <div class="logger-entry" data-logger-index="0">
                <div class="form-group">
                    <label for="service_0">Service</label>
                    <select id="service_0" name="service_0">
                        <option value="Pro6005-2">Pro6005-2</option>
                        <option value="Logger 0">Logger 0</option>
                        <option value="Logger 1">Logger 1</option>
                        <option value="Logger 2">Logger 2</option>
                        <option value="Logger 3">Logger 3</option>
                        <option value="Logger 4">Logger 4</option>
                        <option value="Logger 5">Logger 5</option>
                        <option value="Logger 6">Logger 6</option>
                        <option value="Logger 7">Logger 7</option>
                        <option value="Logger 8">Logger 8</option>
                        <option value="Logger 9">Logger 9</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="location_0">Location (optional)</label>
                    <input type="text" id="location_0" name="location_0" placeholder="e.g., bay_3, north_yard" list="locationList_0">
                    <datalist id="locationList_0"></datalist>
                </div>
            </div>
        </div>
        
        <button class="add-logger-btn" onclick="addLogger()">+ Add Additional Logger</button>
        
        <div class="form-group note-entry note-entry-mobile">
            <label for="noteMobile">Note (optional)</label>
            <div style="position: relative;">
                <select id="noteServiceMobile" onchange="syncNoteFields('mobile')"
                        style="margin-bottom: 8px; font-size: 14px; padding: 8px;">
                    <option value="">General note (all loggers)</option>
                </select>
                <textarea id="noteMobile" placeholder="Add a note or observation..."
                          oninput="syncNoteFields('mobile')"></textarea>
            </div>
        </div>
        
        <button class="btn btn-start" onclick="startEvent()">START EVENT</button>
        <button class="btn btn-end" onclick="endAllLoggers()" style="display:none;">End Event</button>
        <button class="btn btn-location" onclick="setLocation()">SET LOCATION</button>
        <button class="btn btn-note note-button-mobile" onclick="addNote()">ADD NOTE</button>
        
                </div>
            <div class="events-side">

        <div class="form-group note-entry note-entry-desktop">
            <label for="noteDesktop">Note (optional)</label>
            <div style="position: relative;">
                <select id="noteServiceDesktop" onchange="syncNoteFields('desktop')"
                        style="margin-bottom: 8px; font-size: 14px; padding: 8px;">
                    <option value="">General note (all loggers)</option>
                </select>
                <textarea id="noteDesktop" placeholder="Add a note or observation..."
                          oninput="syncNoteFields('desktop')"></textarea>
            </div>
            <button class="btn btn-note note-button-desktop" onclick="addNote()">ADD NOTE</button>
        </div>
        
        <!-- Notes History Section -->
        <div id="notesSection" style="margin-top: 30px; display:none;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                <h3 style="color: white; margin: 0;">Event Notes</h3>
                <div style="display: flex; gap: 10px; align-items: center;">
                    <button id="deleteSelectedBtn" class="btn" onclick="deleteSelectedNotes()" 
                            style="padding: 8px 16px; font-size: 14px; background: #ef4444; display: none;">
                        Delete Selected
                    </button>
                    <button class="btn" onclick="toggleNotes()" style="padding: 8px 16px; font-size: 14px;">
                        <span id="notesToggleText">Hide Notes</span>
                    </button>
                </div>
            </div>
            <div id="notesContainer" style="max-height: 400px; overflow-y: auto; background: rgba(255,255,255,0.05); border-radius: 12px; padding: 15px;">
                <div id="notesListHeader" style="display: none; margin-bottom: 10px; padding: 8px; background: rgba(255,255,255,0.1); border-radius: 6px;">
                    <label style="color: white; cursor: pointer; display: flex; align-items: center; gap: 8px;">
                        <input type="checkbox" id="selectAllNotes" onchange="toggleSelectAll()" style="cursor: pointer; width: 18px; height: 18px;">
                        <span>Select All</span>
                    </label>
                </div>
                <div id="notesList"></div>
            </div>
        </div>
        
        <div class="form-group" style="margin-top: 20px;">
            <label for="imageFile">Upload Image (optional)</label>
            <input type="file" id="imageFile" accept="image/*" capture="environment" 
                   onchange="previewImage(event)" style="margin-bottom: 10px;">
            <div id="imagePreview" style="display:none; margin-bottom: 10px;">
                <img id="previewImg" style="max-width: 100%; max-height: 200px; border-radius: 8px;">
            </div>
            <input type="text" id="imageCaption" placeholder="Image caption (optional)" 
                   style="margin-bottom: 10px;">
            <button class="btn" onclick="uploadImage()" 
                    style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);">
                UPLOAD IMAGE
            </button>
        </div>
        
        <div class="form-group" style="margin-top: 20px;">
            <button class="btn" onclick="loadImages()" 
                    style="background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);">
                VIEW IMAGES
            </button>
        </div>
        
        <div id="imageGallery" style="display:none; margin-top: 20px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                <h3 style="margin: 0; color: white;">Image Gallery</h3>
                <button id="deleteSelectedImagesBtn" class="btn" onclick="deleteSelectedImages()"
                        style="padding: 8px 16px; font-size: 14px; background: #ef4444; display: none;" disabled>
                    Delete Selected
                </button>
            </div>
            <div id="imagesListHeader" style="display: none; margin-bottom: 10px; padding: 8px; background: rgba(255,255,255,0.1); border-radius: 6px;">
                <label style="color: white; cursor: pointer; display: flex; align-items: center; gap: 8px;">
                    <input type="checkbox" id="selectAllImages" onchange="toggleSelectAllImages()" style="cursor: pointer; width: 18px; height: 18px;">
                    <span>Select All</span>
                </label>
            </div>
            <div id="galleryGrid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px;">
            </div>
        </div>

                </div>
            </div>

        <div id="statusMsg" style="display:none;"></div>
        </div>

        <div id="tab-map" class="tab-content">
            <div class="info-box">
                <h3>System Map</h3>
                <div class="info-item"><strong>GPS:</strong> <span id="mapCoords">Loading...</span></div>
                <div class="info-item"><strong>Last Fix:</strong> <span id="mapUpdated">-</span></div>
            </div>
            <div id="map" class="map-container"></div>
            <div class="map-actions">
                <button class="btn" onclick="refreshGps()">Refresh GPS</button>
                <a id="mapLink" href="#" target="_blank" rel="noopener">Open in Google Maps</a>
            </div>
            <div class="map-meta" id="mapNote">Map tiles are cached in your browser after first load.</div>
        </div>
        
        <div id="tab-control" class="tab-content">
            <div class="info-box" style="background: #fef3c7; border: 1px solid #f59e0b;">
                <strong>Warning:</strong> Changes affect the GX device immediately. Use with caution.
            </div>
            <div id="gxStatus" class="gx-status gx-info" style="display:none;"></div>
            
            <div class="control-grid">
            <div class="control-item">
                <label>Battery Charge Current Limit</label>
                <div class="description">Maximum charging current from grid/generator (Amps)</div>
                <div class="current-value" id="current_battery_charge_current">Current: Loading...</div>
                <input type="number" id="input_battery_charge_current" class="control-input" placeholder="e.g., 50" min="0" max="600">
                <button class="btn" onclick="setGXSetting('battery_charge_current', 'input_battery_charge_current')" style="width: 100%; margin-top: 10px;">
                    SET
                </button>
            </div>
            
            <div class="control-item">
                <label>Inverter Mode</label>
                <div class="description">Control inverter operation mode</div>
                <div class="current-value" id="current_inverter_mode">Current: Loading...</div>
                <select id="input_inverter_mode" class="control-input" style="padding: 12px;">
                    <option value="on">On (Inverter + Charger)</option>
                    <option value="inverter_only">Inverter Only</option>
                    <option value="charger_only">Charger Only</option>
                    <option value="off">Off</option>
                </select>
                <button class="btn" onclick="setGXSettingSelect('inverter_mode', 'input_inverter_mode')" style="width: 100%; margin-top: 10px;">
                    SET
                </button>
            </div>
            
            <div class="control-item">
                <label>AC Input Current Limit</label>
                <div class="description">Maximum current draw from AC input (Amps)</div>
                <div class="current-value" id="current_ac_input_current_limit">Current: Loading...</div>
                <input type="number" id="input_ac_input_current_limit" class="control-input" placeholder="e.g., 30" min="0" max="200" step="0.1">
                <button class="btn" onclick="setGXSetting('ac_input_current_limit', 'input_ac_input_current_limit')" style="width: 100%; margin-top: 10px;">
                    SET
                </button>
            </div>
            
            <div class="control-item">
                <label>Inverter Output Voltage</label>
                <div class="description">AC output voltage setpoint (Volts)</div>
                <div class="current-value" id="current_inverter_output_voltage">Current: Loading...</div>
                <input type="number" id="input_inverter_output_voltage" class="control-input" placeholder="e.g., 120" min="100" max="240" step="1">
                <button class="btn" onclick="setGXSetting('inverter_output_voltage', 'input_inverter_output_voltage')" style="width: 100%; margin-top: 10px;">
                    SET
                </button>
            </div>
            </div>
            
            <button class="btn" onclick="loadGXSettings()" style="background: linear-gradient(135deg, #10b981 0%, #059669 100%);">
                REFRESH VALUES
            </button>
        </div>
        
        <div id="statusMsg" style="display:none;"></div>
    </div>
    
    <script>
        const SYSTEM_ID = {{ SYSTEM_ID | tojson }};
        const MAP_TILE_URL = {{ MAP_TILE_URL | tojson }};
        const MAP_TILE_ATTRIBUTION = {{ MAP_TILE_ATTRIBUTION | tojson }};
        const MAP_DEFAULT_ZOOM = {{ MAP_DEFAULT_ZOOM }};
        let mapInstance = null;
        let mapMarker = null;
        let mapInitialized = false;
        let lastGps = null;

        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js').catch((err) => {
                console.warn('Service worker registration failed:', err);
            });
        }

        function showStatus(msg, isError) {
            // Remove any existing status modals
            const existing = document.getElementById('statusModal');
            if (existing) existing.remove();
            
            // Create centered modal overlay
            const modal = document.createElement('div');
            modal.id = 'statusModal';
            modal.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 2000; display: flex; align-items: center; justify-content: center; padding: 20px;';
            
            const msgBox = document.createElement('div');
            msgBox.style.cssText = `
                background: ${isError ? 'linear-gradient(135deg, #ef4444 0%, #dc2626 100%)' : 'linear-gradient(135deg, #10b981 0%, #059669 100%)'};
                color: white;
                padding: 30px 40px;
                border-radius: 12px;
                font-size: 18px;
                font-weight: 600;
                text-align: center;
                box-shadow: 0 10px 40px rgba(0,0,0,0.5);
                max-width: 90%;
                animation: fadeIn 0.2s ease-in;
            `;
            msgBox.textContent = msg;
            
            modal.appendChild(msgBox);
            document.body.appendChild(modal);
            
            // Auto-dismiss after 3 seconds
            setTimeout(() => {
                modal.style.opacity = '0';
                modal.style.transition = 'opacity 0.3s';
                setTimeout(() => modal.remove(), 300);
            }, 3000);
            
            // Also dismiss on click
            modal.onclick = () => {
                modal.style.opacity = '0';
                modal.style.transition = 'opacity 0.3s';
                setTimeout(() => modal.remove(), 300);
            };
        }
        
        // Logger management
        let loggerCount = 1;
        
        function addLogger() {
            const container = document.getElementById('loggersContainer');
            const newLoggerHtml = `
                <div class="logger-entry" data-logger-index="${loggerCount}">
                    <div class="form-group">
                        <div class="logger-header">
                            <label for="service_${loggerCount}">Service</label>
                            <button class="remove-logger" onclick="removeLogger(${loggerCount})">Remove</button>
                        </div>
                        <select id="service_${loggerCount}" name="service_${loggerCount}">
                            <option value="">-- Select Service --</option>
                            <option value="Pro6005-2">Pro6005-2</option>
                            <option value="Logger 0">Logger 0</option>
                            <option value="Logger 1">Logger 1</option>
                            <option value="Logger 2">Logger 2</option>
                            <option value="Logger 3">Logger 3</option>
                            <option value="Logger 4">Logger 4</option>
                            <option value="Logger 5">Logger 5</option>
                            <option value="Logger 6">Logger 6</option>
                            <option value="Logger 7">Logger 7</option>
                            <option value="Logger 8">Logger 8</option>
                            <option value="Logger 9">Logger 9</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label for="location_${loggerCount}">Location (optional)</label>
                        <input type="text" id="location_${loggerCount}" name="location_${loggerCount}" 
                               placeholder="e.g., bay_3, north_yard" list="locationList_${loggerCount}">
                        <datalist id="locationList_${loggerCount}"></datalist>
                    </div>
                </div>
            `;
            container.insertAdjacentHTML('beforeend', newLoggerHtml);
            
            // Add change listener to new dropdown
            const newSelect = document.getElementById(`service_${loggerCount}`);
            if (newSelect) {
                newSelect.addEventListener('change', (e) => { 
                    if (e.isTrusted) updateServiceOptions(); 
                });
            }
            
            loggerCount++;
            updateServiceOptions(); // Disable already-selected services
        }
        
        async function removeLogger(index) {
            // This removes a logger from the ADD form (before it's added to event)
            const logger = document.querySelector(`[data-logger-index="${index}"]`);
            if (!logger) return;
            
            // Just remove from DOM
            logger.remove();
            updateServiceOptions(); // Re-enable service in other dropdowns
        }
        
        function updateServiceOptions() {
            // Get all currently selected services (from form AND active loggers)
            const selected = new Set();
            
            // Add services from active loggers in status area
            document.querySelectorAll('.active-logger-label').forEach(label => {
                const serviceName = label.textContent.trim();
                console.log('Active logger found:', serviceName);
                selected.add(serviceName);
            });
            
            // Build map of which services are selected in which dropdowns
            const dropdownSelections = new Map();
            document.querySelectorAll('.logger-entry').forEach(entry => {
                const index = entry.getAttribute('data-logger-index');
                const select = document.getElementById(`service_${index}`);
                if (select && select.value) { // Only count if a real service is selected
                    dropdownSelections.set(index, select.value);
                }
            });
            
            console.log('All selected services:', Array.from(selected));
            console.log('Dropdown selections:', Object.fromEntries(dropdownSelections));
            
            // Update all dropdowns to disable already-selected options
            document.querySelectorAll('.logger-entry').forEach(entry => {
                const index = entry.getAttribute('data-logger-index');
                const select = document.getElementById(`service_${index}`);
                if (select) {
                    const currentValue = select.value;
                    Array.from(select.options).forEach(option => {
                        // Skip the placeholder option
                        if (option.value === '') {
                            option.disabled = false;
                            return;
                        }
                        
                        // Disable if:
                        // 1. It's in an active logger, OR
                        // 2. It's selected in a DIFFERENT dropdown
                        const inActiveLoggers = selected.has(option.value);
                        const inOtherDropdown = Array.from(dropdownSelections.entries())
                            .some(([idx, val]) => idx !== index && val === option.value);
                        
                        const shouldDisable = inActiveLoggers || inOtherDropdown;
                        option.disabled = shouldDisable;
                        
                        if (shouldDisable) {
                            console.log(`Disabling ${option.value} in dropdown ${index} (active=${inActiveLoggers}, other=${inOtherDropdown})`);
                        }
                    });
                    
                    // If current selection is now disabled (and not empty), switch to first available option
                    if (currentValue && currentValue !== '') {
                        const currentOption = Array.from(select.options).find(opt => opt.value === currentValue);
                        if (currentOption && currentOption.disabled) {
                            const firstAvailable = Array.from(select.options).find(opt => !opt.disabled && opt.value !== '');
                            if (firstAvailable) {
                                select.value = firstAvailable.value;
                                console.log(`Switched dropdown ${index} from ${currentValue} to ${firstAvailable.value}`);
                            }
                        }
                    } else if (currentValue === '') {
                        // If placeholder is selected, auto-select first available
                        const firstAvailable = Array.from(select.options).find(opt => !opt.disabled && opt.value !== '');
                        if (firstAvailable) {
                            select.value = firstAvailable.value;
                            console.log(`Auto-selected ${firstAvailable.value} in dropdown ${index}`);
                        }
                    }
                }
            });
        }
        
        function getAllLoggers() {
            const loggers = [];
            const entries = document.querySelectorAll('.logger-entry');
            entries.forEach((entry, idx) => {
                const index = entry.getAttribute('data-logger-index');
                const service = document.getElementById(`service_${index}`).value;
                const location = document.getElementById(`location_${index}`).value.trim();
                // Only include if a service is actually selected (not the placeholder)
                if (service && service !== '') {
                    loggers.push({ service, location, index });
                }
            });
            return loggers;
        }
        
        async function loadActiveLocations() {
            // Load active event and show in status area
            try {
                const resp = await fetch('/api/status');
                const data = await resp.json();
                
                const activeEvents = data.active_events || [];
                if (activeEvents.length === 0) {
                    updateUIForNoEvent();
                    return;
                }
                
                // Get event_id from first active event
                const currentEventId = activeEvents[0].event_id;
                document.getElementById('eventId').value = currentEventId;
                
                // Get all loggers for this event
                const loggers = activeEvents.filter(e => e.event_id === currentEventId);
                
                // Show active loggers in status area
                displayActiveLoggers(currentEventId, loggers);
                
                // Clear the form area - it's now only for ADDING new loggers
                clearLoggerForm();
                
                updateUIForActiveEvent();
                
                // Load notes for this event
                loadNotes();
                
                // Call updateServiceOptions after a small delay to ensure DOM is ready
                setTimeout(() => updateServiceOptions(), 50);
            } catch (e) {
                console.log('No active event:', e);
                updateUIForNoEvent();
            }
        }
        
        function displayActiveLoggers(eventId, loggers) {
            const statusBox = document.getElementById('statusBox');
            const container = document.getElementById('activeLoggersContainer');
            
            document.getElementById('statusEventId').textContent = eventId;
            
            container.innerHTML = '';
            loggers.forEach(logger => {
                const div = document.createElement('div');
                div.className = 'active-logger-item';
                div.innerHTML = `
                    <div class=\"active-logger-info\">
                        <div class=\"active-logger-label\">${logger.system_id}</div>
                        <div class="active-logger-location">Location: ${logger.location || 'No location set'}</div>
                    </div>
                    <button class=\"btn-remove-logger\" onclick=\"removeActiveLogger('${logger.system_id}')\">Remove</button>
                `;
                container.appendChild(div);
            });
            
            statusBox.style.display = 'block';
            
            // Call updateServiceOptions after DOM update to disable active services
            setTimeout(() => {
                console.log('displayActiveLoggers: calling updateServiceOptions');
                updateServiceOptions();
                updateNoteServiceOptions(); // Update note service selector
            }, 10);
        }
        
        function clearLoggerForm() {
            // Reset form to single empty logger entry
            const container = document.getElementById('loggersContainer');
            container.innerHTML = `
                <div class=\"logger-entry\" data-logger-index=\"0\">
                    <div class=\"form-group\">
                        <label for=\"service_0\">Service</label>
                        <select id=\"service_0\" name=\"service_0\">
                            <option value=\"\">-- Select Service --</option>
                            <option value=\"Pro6005-2\">Pro6005-2</option>
                            <option value=\"Logger 0\">Logger 0</option>
                            <option value=\"Logger 1\">Logger 1</option>
                            <option value=\"Logger 2\">Logger 2</option>
                            <option value=\"Logger 3\">Logger 3</option>
                            <option value=\"Logger 4\">Logger 4</option>
                            <option value=\"Logger 5\">Logger 5</option>
                            <option value=\"Logger 6\">Logger 6</option>
                            <option value=\"Logger 7\">Logger 7</option>
                            <option value=\"Logger 8\">Logger 8</option>
                            <option value=\"Logger 9\">Logger 9</option>
                        </select>
                    </div>
                    <div class=\"form-group\">
                        <label for=\"location_0\">Location (optional)</label>
                        <input type=\"text\" id=\"location_0\" name=\"location_0\" placeholder=\"e.g., bay_3, north_yard\" list=\"locationList_0\">
                        <datalist id=\"locationList_0\"></datalist>
                    </div>
                </div>
            `;
            
            // Re-add listener but PREVENT it from firing during programmatic changes
            const select = document.getElementById('service_0');
            select.addEventListener('change', (e) => {
                // Only call updateServiceOptions if this was a user-initiated change
                if (e.isTrusted) {
                    updateServiceOptions();
                }
            });
            
            loggerCount = 1;
        }
        
        async function removeActiveLogger(systemId) {
            if (!confirm(`Remove ${systemId} from this event?`)) return;
            
            try {
                await apiCall('/api/location/clear', { system_id: systemId });
                showStatus(`Removed ${systemId}`, false);
                await loadActiveLocations();  // Refresh display
                loadStatus();
            } catch (e) {
                showStatus('Error: ' + e.message, true);
            }
        }
        
        function updateUIForActiveEvent() {
            document.querySelector('.btn-start').textContent = '+ ADD LOGGER';
            document.querySelector('.btn-start').onclick = addLoggerToEvent;
            document.querySelector('.btn-end').style.display = 'inline-block';
            
            // Hide event ID field and label
            const eventIdGroup = document.getElementById('eventId').closest('.form-group');
            if (eventIdGroup) eventIdGroup.style.display = 'none';
        }
        
        function updateUIForNoEvent() {
            document.querySelector('.btn-start').textContent = 'START EVENT';
            document.querySelector('.btn-start').onclick = startEvent;
            document.querySelector('.btn-end').style.display = 'none';
            document.getElementById('statusBox').style.display = 'none';
            
            // Show event ID field
            const eventIdGroup = document.getElementById('eventId').closest('.form-group');
            if (eventIdGroup) eventIdGroup.style.display = 'block';
            
            clearLoggerForm();
        }
        
        async function apiCall(endpoint, data) {
            try {
                const resp = await fetch(endpoint, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });
                
                const text = await resp.text();
                let result;
                
                try {
                    result = JSON.parse(text);
                } catch (jsonErr) {
                    throw new Error(text || resp.statusText || 'Request failed');
                }
                
                if (!resp.ok) {
                    throw new Error(result.error || 'Request failed');
                }
                return result;
            } catch (e) {
                throw e;
            }
        }

        function formatPercent(value) {
            if (value === null || value === undefined || Number.isNaN(value)) return '--';
            return `${Number(value).toFixed(1)}%`;
        }

        function formatPower(value) {
            if (value === null || value === undefined || Number.isNaN(value)) return '--';
            const num = Number(value);
            const abs = Math.abs(num);
            if (abs >= 1000) {
                return `${(num / 1000).toFixed(2)} kW`;
            }
            return `${num.toFixed(0)} W`;
        }

        function formatTimestampNs(tsNs) {
            if (!tsNs) return '-';
            const date = new Date(tsNs / 1000000);
            return date.toLocaleString();
        }

        function updateAlerts(alerts) {
            const el = document.getElementById('summary_alerts');
            const card = el ? el.closest('.summary-item') : null;
            if (card) card.classList.remove('alert-bad');
            if (!alerts || alerts.length === 0) {
                el.textContent = 'None';
                el.title = '';
                return;
            }
            if (card) card.classList.add('alert-bad');
            const list = alerts.join(', ');
            el.title = list;
            el.textContent = alerts.length <= 2 ? list : `${alerts.length} active`;
        }

        function updateSocIndicator(value) {
            const socEl = document.getElementById('summary_soc');
            if (!socEl) return;
            const card = socEl.closest('.summary-item');
            if (!card) return;
            card.classList.remove('soc-good', 'soc-warn', 'soc-bad');

            if (value === null || value === undefined || Number.isNaN(value)) {
                return;
            }

            const soc = Number(value);
            if (soc >= 40) {
                card.classList.add('soc-good');
            } else if (soc >= 25) {
                card.classList.add('soc-warn');
            } else {
                card.classList.add('soc-bad');
            }
        }

        async function loadSummary() {
            try {
            const resp = await fetch('/api/summary', { cache: 'no-store' });
                if (!resp.ok) throw new Error('Failed to load summary');
                const data = await resp.json();

                document.getElementById('summary_soc').textContent = formatPercent(data.soc);
                updateSocIndicator(data.soc);
                document.getElementById('summary_pin').textContent = formatPower(data.pin);
                document.getElementById('summary_pout').textContent = formatPower(data.pout);
                updateAlerts(data.alerts || []);
            } catch (e) {
                console.warn('Failed to load summary:', e);
            }
        }
        
        async function loadSummaryFast() {
            // Fast update: Pin, Pout, Alerts only
            try {
            const resp = await fetch('/api/summary', { cache: 'no-store' });
                if (!resp.ok) throw new Error('Failed to load summary');
                const data = await resp.json();

                document.getElementById('summary_pin').textContent = formatPower(data.pin);
                document.getElementById('summary_pout').textContent = formatPower(data.pout);
                updateAlerts(data.alerts || []);
            } catch (e) {
                console.warn('Failed to load summary:', e);
            }
        }
        
        async function loadSummarySlow() {
            // Slow update: SOC only
            try {
            const resp = await fetch('/api/summary', { cache: 'no-store' });
                if (!resp.ok) throw new Error('Failed to load summary');
                const data = await resp.json();

                document.getElementById('summary_soc').textContent = formatPercent(data.soc);
                updateSocIndicator(data.soc);
            } catch (e) {
                console.warn('Failed to load summary:', e);
            }
        }

        function initMap() {
            if (mapInitialized) return;
            if (typeof L === 'undefined') {
                document.getElementById('mapNote').textContent = 'Map library failed to load.';
                return;
            }
            mapInstance = L.map('map', { zoomControl: true });
            L.tileLayer(MAP_TILE_URL, {
                attribution: MAP_TILE_ATTRIBUTION,
                maxZoom: 20
            }).addTo(mapInstance);
            mapInstance.setView([0, 0], 2);
            mapInitialized = true;
        }

        function updateMapPin(lat, lon) {
            if (!mapInitialized) return;
            const coords = [lat, lon];
            if (!mapMarker) {
                mapMarker = L.marker(coords).addTo(mapInstance);
            } else {
                mapMarker.setLatLng(coords);
            }
            mapInstance.setView(coords, MAP_DEFAULT_ZOOM);
        }

        function updateMapLink(lat, lon) {
            const link = document.getElementById('mapLink');
            link.href = `https://www.google.com/maps/dir/?api=1&destination=${lat},${lon}`;
        }

        async function loadGps(refresh = false) {
            try {
                const url = refresh ? '/api/gx/gps?refresh=1' : '/api/gx/gps';
                const resp = await fetch(url);
                const data = await resp.json();
                if (!resp.ok || data.error) {
                    throw new Error(data.error || 'GPS not available');
                }

                const lat = Number(data.latitude);
                const lon = Number(data.longitude);
                if (Number.isNaN(lat) || Number.isNaN(lon)) {
                    throw new Error('Invalid GPS coordinates');
                }
                lastGps = { latitude: lat, longitude: lon, updated_at: data.updated_at };
                document.getElementById('mapCoords').textContent = `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
                document.getElementById('mapUpdated').textContent = formatTimestampNs(data.updated_at);
                updateMapLink(lat, lon);
                updateMapPin(lat, lon);
            } catch (e) {
                document.getElementById('mapCoords').textContent = 'No GPS fix';
                document.getElementById('mapUpdated').textContent = '-';
                console.warn('Failed to load GPS:', e);
            }
        }

        function refreshGps() {
            return loadGps(true);
        }
        
        async function startEvent() {
            const event_id = document.getElementById('eventId').value.trim();
            const noteField = getVisibleField('noteDesktop', 'noteMobile');
            const note = noteField ? noteField.value.trim() : '';
            
            if (!event_id) {
                showStatus('Event ID is required', true);
                return;
            }
            
            // Get all loggers
            const loggers = getAllLoggers();
            console.log('Starting event with loggers:', loggers);
            
            if (loggers.length === 0) {
                showStatus('At least one logger required', true);
                return;
            }
            
            try {
                // Start event with each logger
                for (const logger of loggers) {
                    console.log('Starting logger:', logger);
                    await apiCall('/api/event/start', {
                        system_id: logger.service,
                        event_id,
                        location: logger.location || '',
                        note: note && logger === loggers[0] ? note : ''  // Only add note to first logger
                    });
                }
                
                showStatus(`Event "${event_id}" started with ${loggers.length} logger(s)`, false);
                resetNoteFields();  // Clear note field after use
                await loadActiveLocations();  // Refresh UI
                loadStatus();
                refreshGps();
            } catch (e) {
                console.error('Start event error:', e);
                showStatus('Error: ' + e.message, true);
            }
        }
        
        async function addLoggerToEvent() {
            // Add new loggers to existing event
            const event_id = document.getElementById('eventId').value.trim();
            const loggers = getAllLoggers();
            
            try {
                for (const logger of loggers) {
                    await apiCall('/api/event/start', {
                        system_id: logger.service,
                        event_id,
                        location: logger.location || ''
                    });
                }
                
                showStatus(`${loggers.length} logger(s) added to event "${event_id}"`, false);
                await loadActiveLocations();
                loadStatus();
            } catch (e) {
                showStatus('Error: ' + e.message, true);
            }
        }
        
        async function endAllLoggers() {
            const event_id = document.getElementById('eventId').value.trim();
            
            if (!event_id) {
                showStatus('Event ID not found', true);
                return;
            }
            
            console.log(`Ending event: "${event_id}"`);
            
            if (!confirm(`End ALL loggers for event "${event_id}"?`)) return;
            
            try {
                const result = await apiCall('/api/event/end_all', { event_id });
                console.log('End all result:', result);
                
                // Create custom success message with report button
                const reportLink = result.report_url;
                if (reportLink) {
                    const modal = document.createElement('div');
                    modal.id = 'statusModal';
                    modal.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 2000; display: flex; align-items: center; justify-content: center; padding: 20px;';
                    
                    const msgBox = document.createElement('div');
                    msgBox.style.cssText = `
                        background: linear-gradient(135deg, #10b981 0%, #059669 100%);
                        color: white;
                        padding: 30px 40px;
                        border-radius: 12px;
                        font-size: 18px;
                        font-weight: 600;
                        text-align: center;
                        box-shadow: 0 10px 40px rgba(0,0,0,0.5);
                        max-width: 90%;
                    `;
                    
                    const message = document.createElement('div');
                    message.textContent = `Event "${event_id}" ended (${result.loggers_ended} loggers)`;
                    message.style.marginBottom = '20px';
                    
                    const reportBtn = document.createElement('button');
                    reportBtn.className = 'btn btn-primary';
                    reportBtn.textContent = 'View Report';
                    reportBtn.style.cssText = 'background: white; color: #059669; padding: 12px 24px; border: none; border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer; margin-top: 10px;';
                    reportBtn.onclick = (e) => {
                        e.stopPropagation();
                        window.open(reportLink, '_blank');
                    };
                    
                    msgBox.appendChild(message);
                    msgBox.appendChild(reportBtn);
                    modal.appendChild(msgBox);
                    document.body.appendChild(modal);
                    
                    // Dismiss on click (but not on button click)
                    modal.onclick = () => {
                        modal.style.opacity = '0';
                        modal.style.transition = 'opacity 0.3s';
                        setTimeout(() => modal.remove(), 300);
                    };
                } else {
                    showStatus(`Event "${event_id}" ended (${result.loggers_ended} loggers)`, false);
                }
                
                // Clear form
                document.getElementById('eventId').value = '';
                document.getElementById('location_0').value = '';
                resetNoteFields();
                
                // Remove extra loggers from UI
                const container = document.getElementById('loggersContainer');
                const entries = container.querySelectorAll('.logger-entry');
                for (let i = 1; i < entries.length; i++) entries[i].remove();
                loggerCount = 1;
                
                updateUIForNoEvent();
                loadStatus();
            } catch (e) {
                showStatus('Error: ' + e.message, true);
            }
        }
        
        async function endEvent() {
            const event_id = document.getElementById('eventId').value.trim();
            
            try {
                const result = await apiCall('/api/event/end', {
                    event_id: event_id || undefined
                });
                showStatus('Event ended', false);
                loadStatus();
                updateNoteServiceOptions(); // Clear service dropdown when event ends
            } catch (e) {
                showStatus('Error: ' + e.message, true);
            }
        }
        
        async function setLocation() {
            // Update locations for all loggers
            const loggers = getAllLoggers();
            let successCount = 0;
            
            try {
                for (const logger of loggers) {
                    if (logger.location) {
                        await apiCall('/api/location/set', { 
                            system_id: logger.service,
                            location: logger.location 
                        });
                        successCount++;
                    }
                }
                
                if (successCount > 0) {
                    showStatus(`${successCount} location(s) set`, false);
                } else {
                    showStatus('No locations to set', true);
                }
                loadStatus();
            } catch (e) {
                showStatus('Error: ' + e.message, true);
            }
        }
        
        function getVisibleField(desktopId, mobileId) {
            const desktop = document.getElementById(desktopId);
            if (desktop && desktop.offsetParent !== null) return desktop;
            const mobile = document.getElementById(mobileId);
            return mobile || desktop;
        }

        function syncNoteFields(source) {
            const noteMobile = document.getElementById('noteMobile');
            const noteDesktop = document.getElementById('noteDesktop');
            const serviceMobile = document.getElementById('noteServiceMobile');
            const serviceDesktop = document.getElementById('noteServiceDesktop');

            if (source === 'mobile') {
                if (noteMobile && noteDesktop) noteDesktop.value = noteMobile.value;
                if (serviceMobile && serviceDesktop) serviceDesktop.value = serviceMobile.value;
            } else if (source === 'desktop') {
                if (noteDesktop && noteMobile) noteMobile.value = noteDesktop.value;
                if (serviceDesktop && serviceMobile) serviceMobile.value = serviceDesktop.value;
            }
        }

        function resetNoteFields() {
            const noteMobile = document.getElementById('noteMobile');
            const noteDesktop = document.getElementById('noteDesktop');
            const serviceMobile = document.getElementById('noteServiceMobile');
            const serviceDesktop = document.getElementById('noteServiceDesktop');
            if (noteMobile) noteMobile.value = '';
            if (noteDesktop) noteDesktop.value = '';
            if (serviceMobile) serviceMobile.value = '';
            if (serviceDesktop) serviceDesktop.value = '';
        }

        function updateNoteServiceOptions() {
            const selects = [
                document.getElementById('noteServiceMobile'),
                document.getElementById('noteServiceDesktop')
            ].filter(Boolean);
            if (selects.length === 0) return;
            
            const activeLoggers = document.querySelectorAll('.active-logger-label');
            
            selects.forEach(select => {
                const currentValue = select.value; // Preserve selection
                select.innerHTML = '<option value="">General note (all loggers)</option>';
                
                // Add active loggers as options
                activeLoggers.forEach(label => {
                    const serviceName = label.textContent.trim();
                    const option = document.createElement('option');
                    option.value = serviceName;
                    option.textContent = serviceName;
                    select.appendChild(option);
                });
                
                // Restore previous selection if still valid
                if (currentValue && Array.from(select.options).some(opt => opt.value === currentValue)) {
                    select.value = currentValue;
                }
            });
        }
        
        async function addNote() {
            const event_id = document.getElementById('eventId').value.trim();
            const noteField = getVisibleField('noteDesktop', 'noteMobile');
            const serviceField = getVisibleField('noteServiceDesktop', 'noteServiceMobile');
            let msg = noteField ? noteField.value.trim() : '';
            const selectedService = serviceField ? serviceField.value : '';
            
            console.log('addNote called - event_id:', event_id, 'msg:', msg, 'service:', selectedService);
            
            if (!msg) {
                showStatus('Note required', true);
                return;
            }
            
            // Prepend service name if specific logger selected
            if (selectedService) {
                msg = `[${selectedService}] ${msg}`;
            }
            
            try {
                console.log('Calling /api/note with:', { event_id: event_id || undefined, msg });
                const result = await apiCall('/api/note', {
                    event_id: event_id || undefined, msg
                });
                console.log('Note added successfully:', result);
                showStatus('Note added', false);
                resetNoteFields();
                loadNotes(); // Refresh notes display
                loadStatus();
            } catch (e) {
                console.error('addNote error:', e);
                showStatus('Error: ' + e.message, true);
            }
        }
        
        async function loadNotes() {
            const event_id = document.getElementById('eventId').value.trim();
            if (!event_id) {
                document.getElementById('notesSection').style.display = 'none';
                return;
            }
            
            try {
                const resp = await fetch(`/api/notes?event_id=${encodeURIComponent(event_id)}&limit=50`);
                const data = await resp.json();
                
                const notesList = document.getElementById('notesList');
                const notesSection = document.getElementById('notesSection');
                
                if (!data.notes || data.notes.length === 0) {
                    notesSection.style.display = 'none';
                    return;
                }
                
                notesSection.style.display = 'block';
                
                // Show/hide multi-select controls if more than one note
                const showMultiSelect = data.notes.length > 1;
                document.getElementById('notesListHeader').style.display = showMultiSelect ? 'block' : 'none';
                document.getElementById('deleteSelectedBtn').style.display = showMultiSelect ? 'block' : 'none';
                
                notesList.innerHTML = data.notes.map((note, index) => {
                    const date = new Date(note.timestamp / 1e6); // Convert from nanoseconds to milliseconds
                    const dateStr = date.toLocaleString();
                    const checkboxHtml = showMultiSelect ? `
                        <input type="checkbox" class="note-checkbox" data-note-index="${index}" data-note-id="${note.id}" 
                               data-system-id="${escapeHtml(note.system_id)}" 
                               data-event-id="${escapeHtml(note.event_id)}"
                               data-note-text="${escapeHtml(note.note)}"
                               onchange="updateDeleteButton()"
                               style="cursor: pointer; width: 18px; height: 18px; margin-right: 12px;">
                    ` : '';
                    return `
                        <div style="background: #f0fdf4; padding: 12px; border-radius: 6px; margin-bottom: 10px; border: 1px solid #86efac; display: flex; justify-content: space-between; align-items: center;">
                            ${checkboxHtml}
                            <div style="flex: 1;">
                                <div style="color: #059669; font-weight: 600; font-size: 12px; margin-bottom: 4px;">${escapeHtml(note.system_id)} - ${dateStr}</div>
                                <div style="color: #374151; white-space: pre-wrap; word-wrap: break-word; line-height: 1.5;">${escapeHtml(note.note)}</div>
                            </div>
                            <button onclick="deleteNote(${note.id})" 
                                    data-note-id="${note.id}" data-system-id="${escapeHtml(note.system_id)}" 
                                    data-event-id="${escapeHtml(note.event_id)}"
                                    data-note-text="${escapeHtml(note.note)}"
                                    style="background: #ef4444; color: white; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; margin-left: 10px; flex-shrink: 0;">
                                Delete
                            </button>
                        </div>
                    `;
                }).join('');
            } catch (e) {
                console.error('Failed to load notes:', e);
            }
        }
        
        function toggleNotes() {
            const container = document.getElementById('notesContainer');
            const toggleText = document.getElementById('notesToggleText');
            if (container.style.display === 'none') {
                container.style.display = 'block';
                toggleText.textContent = 'Hide Notes';
            } else {
                container.style.display = 'none';
                toggleText.textContent = 'Show Notes';
            }
        }
        
        async function deleteNote(noteId) {
            if (!confirm('Delete this note?')) return;
            
            try {
                // Get data from button's data attributes
                const button = event.target;
                const system_id = button.getAttribute('data-system-id');
                const event_id = button.getAttribute('data-event-id');
                const note_id = button.getAttribute('data-note-id') || noteId;
                const note_text = button.getAttribute('data-note-text');
                
                console.log('deleteNote - Raw values:', { system_id, event_id, note_id, note_text });
                console.log('deleteNote - note_text length:', note_text ? note_text.length : 'null');
                
                if (!note_id && !note_text) {
                    console.error('note_id and note_text are empty!');
                    showStatus('Error: Note id/text not found', true);
                    return;
                }
                
                // Delete by id when available, fallback to note text
                const requestBody = { system_id, event_id };
                if (note_id) {
                    requestBody.note_id = note_id;
                }
                if (note_text) {
                    requestBody.note_text = note_text;
                }
                console.log('Sending delete request:', requestBody);
                
                const resp = await fetch('/api/audit/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(requestBody)
                });
                
                if (resp.ok) {
                    const result = await resp.json();
                    console.log('Delete result:', result);
                    showStatus('Note deleted', false);
                    loadNotes();
                } else {
                    const error = await resp.json();
                    console.error('Delete failed:', error);
                    showStatus('Error: ' + (error.error || 'Failed to delete'), true);
                }
            } catch (e) {
                console.error('Delete note error:', e);
                showStatus('Error: ' + e.message, true);
            }
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        function toggleSelectAll() {
            const selectAllCheckbox = document.getElementById('selectAllNotes');
            const checkboxes = document.querySelectorAll('.note-checkbox');
            checkboxes.forEach(cb => {
                cb.checked = selectAllCheckbox.checked;
            });
            updateDeleteButton();
        }
        
        function updateDeleteButton() {
            const checkboxes = document.querySelectorAll('.note-checkbox');
            const anyChecked = Array.from(checkboxes).some(cb => cb.checked);
            const deleteBtn = document.getElementById('deleteSelectedBtn');
            if (deleteBtn) {
                deleteBtn.disabled = !anyChecked;
                deleteBtn.style.opacity = anyChecked ? '1' : '0.5';
            }
        }
        
        async function deleteSelectedNotes() {
            const checkboxes = document.querySelectorAll('.note-checkbox:checked');
            if (checkboxes.length === 0) {
                showStatus('No notes selected', true);
                return;
            }
            
            const noteCount = checkboxes.length;
            if (!confirm(`Delete ${noteCount} note${noteCount > 1 ? 's' : ''}?`)) {
                return;
            }
            
            let successCount = 0;
            let failCount = 0;
            
            for (const checkbox of checkboxes) {
                try {
                    const system_id = checkbox.getAttribute('data-system-id');
                    const event_id = checkbox.getAttribute('data-event-id');
                    const note_id = checkbox.getAttribute('data-note-id');
                    const note_text = checkbox.getAttribute('data-note-text');
                    
                    const requestBody = { system_id, event_id };
                    if (note_id) {
                        requestBody.note_id = note_id;
                    }
                    if (note_text) {
                        requestBody.note_text = note_text;
                    }
                    const resp = await fetch('/api/audit/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(requestBody)
                    });
                    
                    if (resp.ok) {
                        successCount++;
                    } else {
                        failCount++;
                        console.error('Failed to delete note:', note_text);
                    }
                } catch (e) {
                    failCount++;
                    console.error('Delete error:', e);
                }
            }
            
            if (successCount > 0) {
                showStatus(`Deleted ${successCount} note${successCount > 1 ? "s" : ""}`, false);
                loadNotes();
            }
            if (failCount > 0) {
                showStatus(`Failed to delete ${failCount} note${failCount > 1 ? 's' : ''}`, true);
            }
        }
        
        async function loadStatus() {
            // Status is now displayed in the active loggers area
            // This function is kept for compatibility but does nothing
        }
        
        // Load status on page load
        loadStatus();
        loadSummary();
        loadGps(false);
        loadActiveLocations();
        setInterval(loadSummaryFast, 1000);  // Update Pin, Pout, Alerts every 1 second
        setInterval(loadSummarySlow, 10000); // Update SOC every 10 seconds
        
        // Add change listener to primary service dropdown (only for user changes)
        document.getElementById('service_0').addEventListener('change', (e) => {
            if (e.isTrusted) updateServiceOptions();
        });
        
        // Load all systems for datalist
        async function loadAllSystems() {
            try {
                const resp = await fetch('/api/status');
                const data = await resp.json();
                
                const systemIds = new Set();
                if (data.active_events) {
                    data.active_events.forEach(e => systemIds.add(e.system_id));
                }
                
                const datalist = document.getElementById('systemIdList');
                if (datalist) {
                    datalist.innerHTML = '';
                    systemIds.forEach(id => {
                        const option = document.createElement('option');
                        option.value = id;
                        datalist.appendChild(option);
                    });
                }
                
                const locationIds = new Set();
                if (data.active_events) {
                    data.active_events.forEach(e => {
                        if (e.location) locationIds.add(e.location);
                    });
                }
                
                const locationList = document.getElementById('locationList');
                if (locationList) {
                    locationList.innerHTML = '';
                    locationIds.forEach(loc => {
                        const option = document.createElement('option');
                        option.value = loc;
                        locationList.appendChild(option);
                    });
                }
            } catch (e) {
                console.error('Failed to load systems:', e);
            }
        }
        
        loadAllSystems();
        setInterval(loadAllSystems, 30000);  // Refresh every 30s
        
        // ========== Image Upload Functions ==========
        
        let selectedImageFile = null;
        
        function previewImage(event) {
            const file = event.target.files[0];
            if (!file) return;
            
            selectedImageFile = file;
            
            const reader = new FileReader();
            reader.onload = function(e) {
                document.getElementById('previewImg').src = e.target.result;
                document.getElementById('imagePreview').style.display = 'block';
            };
            reader.readAsDataURL(file);
        }
        
        async function uploadImage() {
            if (!selectedImageFile) {
                showStatus('Please select an image first', true);
                return;
            }
            
            const eventIdEl = document.getElementById('eventId');
            const event_id = eventIdEl ? eventIdEl.value.trim() : '';
            const caption = document.getElementById('imageCaption').value.trim();
            
            const formData = new FormData();
            formData.append('image', selectedImageFile);
            if (event_id) formData.append('event_id', event_id);
            if (caption) formData.append('caption', caption);
            
            try {
                const resp = await fetch('/api/image/upload', {
                    method: 'POST',
                    body: formData
                });
                
                if (!resp.ok) {
                    let errorMsg = 'Upload failed';
                    try {
                        const err = await resp.json();
                        errorMsg = err.error || errorMsg;
                    } catch (jsonErr) {
                        errorMsg = await resp.text() || resp.statusText || errorMsg;
                    }
                    throw new Error(errorMsg);
                }
                
                const result = await resp.json();
                showStatus(`Image uploaded (${(result.size / 1024).toFixed(1)}KB)`, false);
                
                document.getElementById('imageFile').value = '';
                document.getElementById('imageCaption').value = '';
                document.getElementById('imagePreview').style.display = 'none';
                selectedImageFile = null;
                
                if (document.getElementById('imageGallery').style.display !== 'none') {
                    loadImages();
                }
            } catch (e) {
                showStatus('Upload error: ' + e.message, true);
            }
        }

        async function updateImageCaption(imageId, caption) {
            const resp = await fetch(`/api/image/${imageId}/caption`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ caption })
            });
            if (!resp.ok) {
                let errorMsg = 'Update failed';
                try {
                    const err = await resp.json();
                    errorMsg = err.error || errorMsg;
                } catch (jsonErr) {
                    errorMsg = await resp.text() || resp.statusText || errorMsg;
                }
                throw new Error(errorMsg);
            }
            return resp.json();
        }
        
        async function loadImages() {
            try {
                const resp = await fetch('/api/images?limit=50');
                const data = await resp.json();
                
                const gallery = document.getElementById('imageGallery');
                const grid = document.getElementById('galleryGrid');
                const listHeader = document.getElementById('imagesListHeader');
                const deleteSelectedBtn = document.getElementById('deleteSelectedImagesBtn');
                const selectAllImages = document.getElementById('selectAllImages');
                
                if (data.images.length === 0) {
                    grid.innerHTML = '<p style="color: #ccc; grid-column: 1/-1;">No images uploaded yet</p>';
                    if (listHeader) listHeader.style.display = 'none';
                    if (deleteSelectedBtn) deleteSelectedBtn.style.display = 'none';
                    gallery.style.display = 'block';
                    return;
                }
                
                const showMultiSelect = data.images.length > 1;
                if (listHeader) listHeader.style.display = showMultiSelect ? 'block' : 'none';
                if (deleteSelectedBtn) {
                    deleteSelectedBtn.style.display = showMultiSelect ? 'block' : 'none';
                    deleteSelectedBtn.disabled = true;
                    deleteSelectedBtn.style.opacity = '0.5';
                }
                if (selectAllImages) selectAllImages.checked = false;
                
                grid.innerHTML = '';
                data.images.forEach(img => {
                    const div = document.createElement('div');
                    div.style.cssText = 'background: rgba(255,255,255,0.1); border-radius: 8px; padding: 8px; position: relative; cursor: pointer;';
                    div.onclick = () => {
                        const modal = document.createElement('div');
                        modal.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.9); z-index: 1000; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 20px;';
                        modal.onclick = () => modal.remove();
                        
                        const imgEl = document.createElement('img');
                        imgEl.src = '/images/' + img.filename;
                        imgEl.style.cssText = 'max-width: 90%; max-height: 70vh; border-radius: 8px;';
                        
                        const captionDiv = document.createElement('div');
                        captionDiv.style.cssText = 'color: white; font-size: 16px; margin-top: 20px; max-width: 90%; text-align: center; background: rgba(0,0,0,0.7); padding: 10px 20px; border-radius: 8px;';
                        captionDiv.textContent = img.caption || img.original_filename;
                        
                        const dateDiv = document.createElement('div');
                        dateDiv.style.cssText = 'color: #ccc; font-size: 12px; margin-top: 10px;';
                        const date = new Date(img.timestamp / 1e6);
                        dateDiv.textContent = date.toLocaleString();
                        
                        modal.appendChild(imgEl);
                        modal.appendChild(captionDiv);
                        modal.appendChild(dateDiv);
                        document.body.appendChild(modal);
                    };
                    
                    if (showMultiSelect) {
                        const checkbox = document.createElement('input');
                        checkbox.type = 'checkbox';
                        checkbox.className = 'image-checkbox';
                        checkbox.setAttribute('data-image-id', img.id);
                        checkbox.style.cssText = 'position: absolute; top: 6px; left: 6px; width: 18px; height: 18px; cursor: pointer;';
                        checkbox.onchange = updateImageDeleteButton;
                        checkbox.onclick = (e) => e.stopPropagation();
                        div.appendChild(checkbox);
                    }
                    
                    const imgEl = document.createElement('img');
                    imgEl.src = '/images/' + img.filename;
                    imgEl.style.cssText = 'width: 100%; height: 120px; object-fit: cover; border-radius: 4px;';
                    
                    const captionWrap = document.createElement('div');
                    captionWrap.style.cssText = 'display: flex; flex-direction: column; gap: 4px;';

                    const caption = document.createElement('div');
                    caption.style.cssText = 'color: white; font-size: 12px; margin-top: 5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;';
                    caption.textContent = img.caption || img.original_filename;
                    caption.title = img.caption || img.original_filename;
                    captionWrap.appendChild(caption);

                    const editBtn = document.createElement('button');
                    editBtn.textContent = img.caption ? 'Edit caption' : 'Add caption';
                    editBtn.style.cssText = 'background: none; border: none; padding: 0; color: #93c5fd; font-size: 12px; cursor: pointer; text-align: left;';

                    const editWrap = document.createElement('div');
                    editWrap.style.cssText = 'display: none; margin-top: 6px;';

                    const captionInput = document.createElement('input');
                    captionInput.type = 'text';
                    captionInput.value = img.caption || '';
                    captionInput.placeholder = 'Add a caption...';
                    captionInput.style.cssText = 'width: 100%; padding: 6px 8px; border-radius: 6px; border: 1px solid #d1d5db; font-size: 12px;';
                    captionInput.onclick = (e) => e.stopPropagation();

                    const editActions = document.createElement('div');
                    editActions.style.cssText = 'display: flex; gap: 6px; margin-top: 6px;';

                    const saveBtn = document.createElement('button');
                    saveBtn.textContent = 'Save';
                    saveBtn.style.cssText = 'background: #e5e7eb; border: none; border-radius: 6px; padding: 6px 8px; font-size: 12px; cursor: pointer;';
                    saveBtn.onclick = async (e) => {
                        e.stopPropagation();
                        try {
                            await updateImageCaption(img.id, captionInput.value.trim());
                            showStatus('Caption updated', false);
                            loadImages();
                        } catch (err) {
                            showStatus('Caption update error: ' + err.message, true);
                        }
                    };

                    const cancelBtn = document.createElement('button');
                    cancelBtn.textContent = 'Cancel';
                    cancelBtn.style.cssText = 'background: #f3f4f6; border: none; border-radius: 6px; padding: 6px 8px; font-size: 12px; cursor: pointer;';
                    cancelBtn.onclick = (e) => {
                        e.stopPropagation();
                        editWrap.style.display = 'none';
                        captionWrap.style.display = 'flex';
                    };

                    editBtn.onclick = (e) => {
                        e.stopPropagation();
                        captionInput.value = img.caption || '';
                        editWrap.style.display = 'block';
                        captionWrap.style.display = 'none';
                        captionInput.focus();
                    };

                    editActions.appendChild(saveBtn);
                    editActions.appendChild(cancelBtn);
                    editWrap.appendChild(captionInput);
                    editWrap.appendChild(editActions);
                    captionWrap.appendChild(editBtn);
                    
                    const timestamp = document.createElement('div');
                    timestamp.style.cssText = 'color: #ccc; font-size: 10px; margin-top: 2px;';
                    const date = new Date(img.timestamp / 1e6);
                    timestamp.textContent = date.toLocaleString();
                    
                    const delBtn = document.createElement('button');
                    delBtn.textContent = 'x';
                    delBtn.style.cssText = 'position: absolute; top: 5px; right: 5px; background: rgba(255,0,0,0.8); color: white; border: none; border-radius: 50%; width: 24px; height: 24px; cursor: pointer; font-size: 18px; line-height: 1;';
                    delBtn.onclick = (e) => {
                        e.stopPropagation();
                        deleteImage(img.id);
                    };
                    
                    div.appendChild(imgEl);
                    div.appendChild(captionWrap);
                    div.appendChild(editWrap);
                    div.appendChild(timestamp);
                    div.appendChild(delBtn);
                    grid.appendChild(div);
                });
                
                gallery.style.display = 'block';
            } catch (e) {
                showStatus('Failed to load images: ' + e.message, true);
            }
        }
        
        function toggleSelectAllImages() {
            const selectAllCheckbox = document.getElementById('selectAllImages');
            const checkboxes = document.querySelectorAll('.image-checkbox');
            checkboxes.forEach(cb => {
                cb.checked = selectAllCheckbox.checked;
            });
            updateImageDeleteButton();
        }
        
        function updateImageDeleteButton() {
            const checkboxes = document.querySelectorAll('.image-checkbox');
            const anyChecked = Array.from(checkboxes).some(cb => cb.checked);
            const deleteBtn = document.getElementById('deleteSelectedImagesBtn');
            if (deleteBtn) {
                deleteBtn.disabled = !anyChecked;
                deleteBtn.style.opacity = anyChecked ? '1' : '0.5';
            }
        }
        
        async function deleteSelectedImages() {
            const checkboxes = document.querySelectorAll('.image-checkbox:checked');
            if (checkboxes.length === 0) {
                showStatus('No images selected', true);
                return;
            }
            
            const imageCount = checkboxes.length;
            if (!confirm(`Delete ${imageCount} image${imageCount > 1 ? 's' : ''}?`)) {
                return;
            }
            
            let successCount = 0;
            let failCount = 0;
            
            for (const checkbox of checkboxes) {
                try {
                    const imageId = checkbox.getAttribute('data-image-id');
                    const resp = await fetch('/api/image/' + imageId, {
                        method: 'DELETE'
                    });
                    
                    if (resp.ok) {
                        successCount++;
                    } else {
                        failCount++;
                        console.error('Failed to delete image:', imageId);
                    }
                } catch (e) {
                    failCount++;
                    console.error('Delete error:', e);
                }
            }
            
            if (successCount > 0) {
                showStatus(`Deleted ${successCount} image${successCount > 1 ? 's' : ''}`, false);
            }
            if (failCount > 0) {
                showStatus(`Failed to delete ${failCount} image${failCount > 1 ? 's' : ''}`, true);
            }
            
            loadImages();
        }
        
        async function deleteImage(imageId) {
            if (!confirm('Delete this image?')) return;
            
            try {
                const resp = await fetch('/api/image/' + imageId, {
                    method: 'DELETE'
                });
                
                if (!resp.ok) {
                    let errorMsg = 'Delete failed';
                    try {
                        const err = await resp.json();
                        errorMsg = err.error || errorMsg;
                    } catch (jsonErr) {
                        errorMsg = await resp.text() || resp.statusText || errorMsg;
                    }
                    throw new Error(errorMsg);
                }
                
                showStatus('Image deleted', false);
                loadImages();
            } catch (e) {
                showStatus('Delete error: ' + e.message, true);
            }
        }
        
        // Tab switching
        function switchTab(tabName) {
            // Hide all tabs
            document.querySelectorAll('.tab-content').forEach(el => {
                el.classList.remove('active');
            });
            document.querySelectorAll('.tab').forEach(el => {
                el.classList.remove('active');
            });
            
            // Show selected tab
            document.getElementById('tab-' + tabName).classList.add('active');
            document.querySelector('.tab[onclick*="' + tabName + '"]').classList.add('active');
            
            // Load GX settings when switching to control tab
            if (tabName === 'control') {
                loadGXSettings();
            }
            if (tabName === 'map') {
                initMap();
                if (lastGps) {
                    updateMapPin(lastGps.latitude, lastGps.longitude);
                } else {
                    loadGps(false);
                }
                setTimeout(() => {
                    if (mapInstance) mapInstance.invalidateSize();
                }, 200);
            }
        }
        
        // GX Control Functions
        let gxPollHandle = null;
        let gxPollStartedAt = 0;
        const GX_POLL_INTERVAL_MS = 2000;
        const GX_POLL_TIMEOUT_MS = 20000;

        function setGxStatus(message, level) {
            const el = document.getElementById('gxStatus');
            if (!el) return;
            el.textContent = message || '';
            el.classList.remove('gx-info', 'gx-warn', 'gx-success');
            if (level === 'success') {
                el.classList.add('gx-success');
            } else if (level === 'warn') {
                el.classList.add('gx-warn');
            } else {
                el.classList.add('gx-info');
            }
            el.style.display = message ? 'block' : 'none';
        }

        async function fetchGXSettings() {
            const resp = await fetch('/api/gx/settings', { cache: 'no-store' });
            if (!resp.ok) throw new Error('Failed to load settings');
            return resp.json();
        }

        function formatAge(tsNs) {
            if (!tsNs) return '';
            const ageSec = Math.max(0, Math.round((Date.now() - (tsNs / 1e6)) / 1000));
            if (ageSec < 60) return ` (updated ${ageSec}s ago)`;
            const ageMin = Math.round(ageSec / 60);
            if (ageMin < 60) return ` (updated ${ageMin}m ago)`;
            const ageHr = Math.round(ageMin / 60);
            return ` (updated ${ageHr}h ago)`;
        }

        function formatGxValue(value, suffix) {
            if (value === null || value === undefined || Number.isNaN(value)) return '--';
            return `${value} ${suffix}`;
        }

        function applyGXSettings(settings) {
            if (settings.battery_charge_current) {
                const age = formatAge(settings.battery_charge_current.updated_at);
                document.getElementById('current_battery_charge_current').textContent =
                    `Current: ${formatGxValue(settings.battery_charge_current.value, 'A')}${age}`;
            }

            if (settings.inverter_mode) {
                const modeLabels = {3: 'On', 2: 'Inverter Only', 1: 'Charger Only', 4: 'Off'};
                const modeValue = Number(settings.inverter_mode.value);
                const modeLabel = modeLabels[modeValue] || 'Unknown';
                const age = formatAge(settings.inverter_mode.updated_at);
                document.getElementById('current_inverter_mode').textContent =
                    `Current: ${modeLabel}${age}`;
                const modeMap = {'3': 'on', '1': 'charger_only', '2': 'inverter_only', '4': 'off'};
                document.getElementById('input_inverter_mode').value =
                    modeMap[settings.inverter_mode.value] || 'on';
            }

            if (settings.ac_input_current_limit) {
                const age = formatAge(settings.ac_input_current_limit.updated_at);
                document.getElementById('current_ac_input_current_limit').textContent =
                    `Current: ${formatGxValue(settings.ac_input_current_limit.value, 'A')}${age}`;
            }

            if (settings.inverter_output_voltage) {
                const age = formatAge(settings.inverter_output_voltage.updated_at);
                document.getElementById('current_inverter_output_voltage').textContent =
                    `Current: ${formatGxValue(settings.inverter_output_voltage.value, 'V')}${age}`;
            }
        }

        async function loadGXSettings() {
            try {
                const settings = await fetchGXSettings();
                applyGXSettings(settings);
                showStatus('Settings loaded', false);
            } catch (e) {
                showStatus('Error loading settings: ' + e.message, true);
            }
        }

        function normalizeExpectedValue(settingName, value) {
            if (settingName === 'inverter_mode') {
                const modeMap = {on: 3, charger_only: 1, inverter_only: 2, off: 4};
                if (typeof value === 'string' && value in modeMap) {
                    return modeMap[value];
                }
            }
            const num = Number(value);
            return Number.isFinite(num) ? num : null;
        }

        function isSettingApplied(settingName, expectedValue, settings) {
            const setting = settings[settingName];
            if (!setting || setting.value === null || setting.value === undefined) return false;
            const actual = Number(setting.value);
            if (!Number.isFinite(actual) || expectedValue === null) return false;
            const tolerance = settingName === 'inverter_output_voltage' ? 0.5 : 0.1;
            return Math.abs(actual - expectedValue) <= tolerance;
        }

        function stopGxSettingPoll() {
            if (gxPollHandle) {
                clearTimeout(gxPollHandle);
                gxPollHandle = null;
            }
        }

        function startGxSettingPoll(settingName, expectedValue) {
            stopGxSettingPoll();
            gxPollStartedAt = Date.now();
            const expected = normalizeExpectedValue(settingName, expectedValue);
            setGxStatus('Update sent. Current values may take a moment to refresh. Polling for confirmation...', 'info');

            const tick = async () => {
                try {
                    const settings = await fetchGXSettings();
                    applyGXSettings(settings);
                    if (expected !== null && isSettingApplied(settingName, expected, settings)) {
                        setGxStatus('Current value updated.', 'success');
                        stopGxSettingPoll();
                        return;
                    }
                } catch (e) {
                    console.warn('GX settings poll failed:', e);
                }

                if (Date.now() - gxPollStartedAt >= GX_POLL_TIMEOUT_MS) {
                    setGxStatus('Update sent. Current values may still be syncing. Refreshing the page to re-sync...', 'warn');
                    stopGxSettingPoll();
                    setTimeout(() => window.location.reload(), 1500);
                    return;
                }

                gxPollHandle = setTimeout(tick, GX_POLL_INTERVAL_MS);
            };

            gxPollHandle = setTimeout(tick, 1000);
        }
        
        async function setGXSetting(settingName, inputId) {
            const value = document.getElementById(inputId).value.trim();
            if (!value) {
                showStatus('Please enter a value', true);
                return;
            }
            
            if (!confirm(`Set ${settingName.replace(/_/g, ' ')} to ${value}?`)) return;
            
            try {
                const resp = await fetch('/api/gx/setting', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ setting: settingName, value: value })
                });
                
                if (!resp.ok) {
                    const err = await resp.json();
                    throw new Error(err.error || 'Failed to set value');
                }
                
                const result = await resp.json();
                showStatus(`${result.message}`, false);
                
                // Clear input and poll until values reflect the change
                document.getElementById(inputId).value = '';
                startGxSettingPoll(settingName, value);
            } catch (e) {
                showStatus('Error: ' + e.message, true);
            }
        }
        
        async function setGXSettingSelect(settingName, inputId) {
            const value = document.getElementById(inputId).value;
            
            if (!confirm(`Change inverter mode to "${value.replace(/_/g, ' ')}"?`)) return;
            
            try {
                const resp = await fetch('/api/gx/setting', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ setting: settingName, value: value })
                });
                
                if (!resp.ok) {
                    const err = await resp.json();
                    throw new Error(err.error || 'Failed to set value');
                }
                
                const result = await resp.json();
                showStatus(`${result.message}`, false);
                
                // Poll until values reflect the change
                startGxSettingPoll(settingName, value);
            } catch (e) {
                showStatus('Error: ' + e.message, true);
            }
        }
    </script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def web_ui():
    """Serve the touch-friendly web UI."""
    return render_template_string(
        WEB_UI_HTML,
        SYSTEM_ID=SYSTEM_ID,
        MAP_TILE_URL=MAP_TILE_URL,
        MAP_TILE_ATTRIBUTION=MAP_TILE_ATTRIBUTION,
        MAP_DEFAULT_ZOOM=MAP_DEFAULT_ZOOM
    )


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    version = os.getenv("APP_VERSION") or os.getenv("EDGE_VERSION") or "dev"
    git_sha = os.getenv("GIT_SHA", "")
    return jsonify({"status": "ok", "vm_url": VM_WRITE_URL, "version": version, "git_sha": git_sha})


# ============================================================================
# Main
# ============================================================================

# Initialize database on module load (for gunicorn workers)
init_db()

# Start heartbeat worker thread
heartbeat_thread = threading.Thread(target=heartbeat_worker, daemon=True, name="heartbeat")
heartbeat_thread.start()
logger.info("Heartbeat thread started")

report_thread = threading.Thread(target=report_upload_worker, daemon=True, name="report-upload")
report_thread.start()
logger.info("Report upload worker started")

tile_usage_thread = threading.Thread(target=tile_usage_sync_worker, daemon=True, name="tile-usage-sync")
tile_usage_thread.start()
logger.info("Tile usage sync worker started")

if __name__ == "__main__":
    # Only used for local development: python app.py
    # Production uses: gunicorn -w 2 -b 0.0.0.0:8088 app:app
    logger.info("OVR Event Service starting (dev mode)...")
    logger.info(f"VictoriaMetrics write URL: {VM_WRITE_URL}")
    logger.info(f"Database: {DB_PATH}")
    
    port = int(os.environ.get("PORT", "8088"))
    app.run(host="0.0.0.0", port=port, debug=False)
