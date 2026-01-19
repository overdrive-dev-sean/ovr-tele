import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
from urllib.parse import quote

import jwt
import requests
from flask import Flask, jsonify, request, send_from_directory

from map_tiles import (
    PROVIDERS as MAP_TILE_PROVIDERS,
    utc_month_key,
    init_map_tables,
    set_preferred_provider,
    get_preferred_provider,
    build_guardrail_status,
    is_valid_provider,
)

app = Flask(__name__)
logger = logging.getLogger(__name__)

VM_URL = os.getenv("FLEET_VM_URL", "http://victoria-metrics:8428").rstrip("/")
METRIC_SUFFIX = os.getenv("FLEET_METRIC_SUFFIX", ":avg")
DEPLOYMENT_ID = os.getenv("FLEET_DEPLOYMENT_ID", "").strip()
NODE_URL_TEMPLATE = os.getenv("FLEET_NODE_URL_TEMPLATE", "").strip()
NODE_DOMAIN = os.getenv("FLEET_NODE_DOMAIN", "").strip()
NODE_SUBDOMAIN = os.getenv("FLEET_NODE_SUBDOMAIN", "").strip().strip(".")
BASE_DOMAIN = os.getenv("BASE_DOMAIN", "").strip()
LOOKBACK_SECONDS = int(os.getenv("FLEET_LOOKBACK_SECONDS", "3600"))
GPS_STALE_SECONDS = int(os.getenv("FLEET_GPS_STALE_SECONDS", "1800"))
DB_PATH = os.getenv("FLEET_DB_PATH", "/data/fleet.db")
VM_TIMEOUT_SECONDS = float(os.getenv("FLEET_VM_TIMEOUT", "5"))
MAP_DEFAULT_PROVIDER = os.getenv("MAP_DEFAULT_PROVIDER", "esri").strip().lower()
MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN", "").strip()
MAPBOX_TOKEN_FILE = os.getenv("MAPBOX_TOKEN_FILE", "").strip()
ESRI_TOKEN = os.getenv("ESRI_TOKEN", "").strip()
MAPBOX_TILE_URL_TEMPLATE = os.getenv(
    "MAPBOX_TILE_URL",
    "https://api.mapbox.com/styles/v1/mapbox/satellite-v9/tiles/256/{z}/{x}/{y}?access_token={token}",
)
ESRI_TILE_URL = os.getenv(
    "ESRI_TILE_URL",
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
)
MAPBOX_ATTRIBUTION = os.getenv("MAPBOX_ATTRIBUTION", "Imagery (c) Mapbox")
ESRI_ATTRIBUTION = os.getenv("ESRI_ATTRIBUTION", "Tiles (c) Esri")
MAPBOX_MAX_ZOOM = int(os.getenv("MAPBOX_MAX_ZOOM", "20"))
ESRI_MAX_ZOOM = int(os.getenv("ESRI_MAX_ZOOM", "19"))
MAPBOX_FREE_TILES_PER_MONTH = int(os.getenv("MAPBOX_FREE_TILES_PER_MONTH", "750000"))
ESRI_FREE_TILES_PER_MONTH = int(os.getenv("ESRI_FREE_TILES_PER_MONTH", "2000000"))
GUARDRAIL_LIMIT_PCT = float(os.getenv("GUARDRAIL_LIMIT_PCT", "0.95"))
MAP_TILES_LOOKBACK_SECONDS = int(
    os.getenv("FLEET_MAP_TILES_LOOKBACK_SECONDS", str(LOOKBACK_SECONDS))
)
REPORTS_PATH = os.getenv("FLEET_REPORTS_PATH", "/data/reports").strip()
REPORTS_UPLOAD_TOKEN = os.getenv("FLEET_REPORTS_UPLOAD_TOKEN", "").strip()
REPORTS_TIMEZONE_NAME = os.getenv(
    "FLEET_REPORTS_TIMEZONE", "America/Los_Angeles"
).strip()
PLACEHOLDER_DEPLOYMENT_IDS = {"__DEPLOYMENT_ID__", "__DEPLOYEMENT_ID__", ""}
PLACEHOLDER_NODE_IDS = {"__NODE_ID__", ""}

ACCESS_AUD = os.getenv("FLEET_ACCESS_AUD", "").strip()
ACCESS_TEAM_DOMAIN = os.getenv("FLEET_ACCESS_TEAM_DOMAIN", "").strip()
ACCESS_CERTS_URL = os.getenv("FLEET_ACCESS_CERTS_URL", "").strip()
ACCESS_JWT_HEADER = (
    os.getenv("FLEET_ACCESS_JWT_HEADER", "Cf-Access-Jwt-Assertion").strip()
    or "Cf-Access-Jwt-Assertion"
)
ACCESS_JWKS_TTL = int(os.getenv("FLEET_ACCESS_JWKS_TTL") or "3600")
ACCESS_JWT_LEEWAY = int(os.getenv("FLEET_ACCESS_JWT_LEEWAY") or "60")
ACCESS_ISSUER = os.getenv("FLEET_ACCESS_ISSUER", "").strip()
ACCESS_BYPASS_PATHS = {
    item.strip()
    for item in os.getenv(
        "FLEET_ACCESS_BYPASS_PATHS", "/api/health,/api/reports/upload"
    ).split(",")
    if item.strip()
}

if not MAPBOX_TOKEN and MAPBOX_TOKEN_FILE:
    try:
        with open(MAPBOX_TOKEN_FILE, "r") as handle:
            MAPBOX_TOKEN = handle.read().strip()
    except Exception as exc:
        logger.warning(f"Failed to read Mapbox token file: {exc}")

try:
    REPORTS_TIMEZONE = ZoneInfo(REPORTS_TIMEZONE_NAME or "UTC")
except Exception as exc:
    logger.warning(f"Invalid reports timezone '{REPORTS_TIMEZONE_NAME}': {exc}")
    REPORTS_TIMEZONE = ZoneInfo("UTC")


SCHEMA = """
CREATE TABLE IF NOT EXISTS manual_locations (
  system_id TEXT PRIMARY KEY,
  latitude REAL NOT NULL,
  longitude REAL NOT NULL,
  label TEXT,
  updated_at INTEGER NOT NULL
);
"""


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _get_db() as conn:
        conn.executescript(SCHEMA)
        init_map_tables(conn)
        conn.commit()


_init_db()


# ============================================================================
# Cloudflare Access JWT
# ============================================================================

def _normalize_access_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value.rstrip("/")
    return f"https://{value.rstrip('/')}"


def _access_certs_url() -> str:
    if ACCESS_CERTS_URL:
        return _normalize_access_url(ACCESS_CERTS_URL)
    if ACCESS_TEAM_DOMAIN:
        base = _normalize_access_url(ACCESS_TEAM_DOMAIN)
        if base:
            return f"{base}/cdn-cgi/access/certs"
    return ""


def _access_issuer() -> str:
    if ACCESS_ISSUER:
        return _normalize_access_url(ACCESS_ISSUER)
    if ACCESS_TEAM_DOMAIN:
        return _normalize_access_url(ACCESS_TEAM_DOMAIN)
    return ""


ACCESS_CERTS_ENDPOINT = _access_certs_url()
ACCESS_ISSUER_VALUE = _access_issuer()
ACCESS_ENABLED = bool(ACCESS_AUD and ACCESS_CERTS_ENDPOINT)

_access_jwks_cache = {"fetched_at": 0.0, "keys": {}}
_access_jwks_lock = threading.Lock()


def _fetch_access_keys(force: bool = False) -> Dict[str, Any]:
    if not ACCESS_ENABLED:
        return {}
    now = time.time()
    with _access_jwks_lock:
        cached = _access_jwks_cache["keys"]
        if (
            cached
            and not force
            and now - _access_jwks_cache["fetched_at"] < ACCESS_JWKS_TTL
        ):
            return cached

    try:
        resp = requests.get(ACCESS_CERTS_ENDPOINT, timeout=VM_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return _access_jwks_cache["keys"]
        payload = resp.json()
    except Exception:
        return _access_jwks_cache["keys"]

    keys: Dict[str, Any] = {}
    for jwk in payload.get("keys", []):
        kid = jwk.get("kid")
        if not kid:
            continue
        try:
            keys[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        except Exception:
            continue

    if keys:
        with _access_jwks_lock:
            _access_jwks_cache["keys"] = keys
            _access_jwks_cache["fetched_at"] = now
        return keys

    return _access_jwks_cache["keys"]


def _extract_access_token() -> str:
    token = request.headers.get(ACCESS_JWT_HEADER, "").strip()
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    return token


def _verify_access_token(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    try:
        header = jwt.get_unverified_header(token)
    except Exception:
        return None
    kid = header.get("kid")
    if not kid:
        return None
    keys = _fetch_access_keys()
    key = keys.get(kid)
    if not key:
        keys = _fetch_access_keys(force=True)
        key = keys.get(kid)
    if not key:
        return None
    options = {"require": ["exp", "iat", "aud"]}
    decode_args: Dict[str, Any] = {
        "key": key,
        "algorithms": ["RS256"],
        "audience": ACCESS_AUD,
        "options": options,
        "leeway": ACCESS_JWT_LEEWAY,
    }
    if ACCESS_ISSUER_VALUE:
        decode_args["issuer"] = ACCESS_ISSUER_VALUE
    try:
        return jwt.decode(token, **decode_args)
    except Exception:
        return None


@app.before_request
def _enforce_access_jwt() -> Optional[Any]:
    if not ACCESS_ENABLED:
        return None
    if request.path in ACCESS_BYPASS_PATHS:
        return None
    if not request.path.startswith("/api/"):
        return None
    token = _extract_access_token()
    if not token:
        return jsonify({"error": "access_token_required"}), 401
    claims = _verify_access_token(token)
    if not claims:
        return jsonify({"error": "access_token_invalid"}), 401
    return None


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

def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _escape_regex(value: str) -> str:
    return re.escape(value)

_LABEL_UNESCAPE_RE = re.compile(r"\\([ ,=\\])")


def _normalize_label_value(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    return _LABEL_UNESCAPE_RE.sub(r"\1", value)


def _clean_label_value(value: Optional[str], placeholders: set[str]) -> Optional[str]:
    if not value:
        return None
    trimmed = value.strip()
    if not trimmed or trimmed in placeholders:
        return None
    return trimmed


def _is_placeholder_deployment(value: Optional[str]) -> bool:
    if not value:
        return True
    trimmed = value.strip()
    if not trimmed:
        return True
    if trimmed in PLACEHOLDER_DEPLOYMENT_IDS:
        return True
    return trimmed.startswith("__DEPLOYMENT") or trimmed.startswith("__DEPLOYEMENT")


def _assign_label_value(
    node: Dict[str, Any], key: str, value: Optional[str], placeholders: set[str]
) -> None:
    cleaned = _clean_label_value(value, placeholders)
    if not cleaned:
        return
    current = _clean_label_value(node.get(key), placeholders)
    if not current:
        node[key] = cleaned


def _system_id_variants(system_id: str) -> List[str]:
    variants: List[str] = []
    if "." in system_id:
        variants.append(system_id.replace(".", "-"))
    if "-" in system_id:
        variants.append(system_id.replace("-", "."))
    return variants


_ACUVIM_DEVICE_RE = re.compile(r"^acuvim_1(\d+)$")


def _node_merge_score(node: Dict[str, Any]) -> int:
    score = 0
    for key in (
        "soc",
        "pout",
        "acuvim_vavg",
        "acuvim_iavg",
        "acuvim_p",
        "latitude",
        "longitude",
        "event_id",
        "location",
        "node_id",
    ):
        value = node.get(key)
        if value not in (None, "", []):
            score += 1
    return score


def _merge_node_records(primary: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(primary)

    _assign_label_value(merged, "node_id", incoming.get("node_id"), PLACEHOLDER_NODE_IDS)
    _assign_label_value(
        merged, "deployment_id", incoming.get("deployment_id"), PLACEHOLDER_DEPLOYMENT_IDS
    )

    if not merged.get("location") and incoming.get("location"):
        merged["location"] = incoming["location"]

    if incoming.get("alerts"):
        base_alerts = merged.get("alerts") or []
        merged["alerts"] = sorted(set(base_alerts).union(incoming.get("alerts") or []))

    incoming_event_ts = incoming.get("event_updated_at") or 0
    primary_event_ts = merged.get("event_updated_at") or 0
    if incoming.get("event_id") and (
        not merged.get("event_id") or incoming_event_ts >= primary_event_ts
    ):
        merged["event_id"] = incoming.get("event_id")
        merged["event_updated_at"] = incoming.get("event_updated_at")

    for key in ("soc", "pout", "acuvim_vavg", "acuvim_iavg", "acuvim_p"):
        if merged.get(key) is None and incoming.get(key) is not None:
            merged[key] = incoming.get(key)

    if merged.get("acuvim_updated_at") is None and incoming.get("acuvim_updated_at") is not None:
        merged["acuvim_updated_at"] = incoming.get("acuvim_updated_at")
    elif (
        incoming.get("acuvim_updated_at") is not None
        and merged.get("acuvim_updated_at") is not None
        and incoming.get("acuvim_updated_at") > merged.get("acuvim_updated_at")
    ):
        merged["acuvim_updated_at"] = incoming.get("acuvim_updated_at")

    incoming_has_gps = (
        incoming.get("latitude") is not None and incoming.get("longitude") is not None
    )
    primary_has_gps = (
        merged.get("latitude") is not None and merged.get("longitude") is not None
    )
    incoming_gps_ts = incoming.get("gps_updated_at") or 0
    primary_gps_ts = merged.get("gps_updated_at") or 0
    if incoming_has_gps and (
        not primary_has_gps or incoming_gps_ts >= primary_gps_ts
    ):
        merged["latitude"] = incoming.get("latitude")
        merged["longitude"] = incoming.get("longitude")
        merged["gps_updated_at"] = incoming.get("gps_updated_at")
        merged["gps_inherited"] = incoming.get("gps_inherited")

    if incoming.get("is_logger"):
        merged["is_logger"] = True

    return merged


def _merge_system_id_variants(nodes: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[str]] = {}
    for system_id in nodes.keys():
        canonical = system_id.replace(".", "-")
        grouped.setdefault(canonical, []).append(system_id)

    merged_nodes: Dict[str, Dict[str, Any]] = {}
    for canonical, ids in grouped.items():
        if len(ids) == 1:
            merged_nodes[ids[0]] = nodes[ids[0]]
            continue

        node_ids = {
            nodes[system_id].get("node_id")
            for system_id in ids
            if nodes[system_id].get("node_id")
        }
        if len(node_ids) > 1:
            for system_id in ids:
                merged_nodes[system_id] = nodes[system_id]
            continue

        base_id = ids[0]
        base_score = -1
        for system_id in ids:
            score = _node_merge_score(nodes[system_id])
            if score > base_score or (score == base_score and system_id == canonical):
                base_id = system_id
                base_score = score

        merged = dict(nodes[base_id])
        for system_id in ids:
            if system_id == base_id:
                continue
            merged = _merge_node_records(merged, nodes[system_id])

        merged["system_id"] = canonical
        merged_nodes[canonical] = merged

    return merged_nodes


def _acuvim_device_to_logger(device: Optional[str]) -> Optional[str]:
    if not device:
        return None
    match = _ACUVIM_DEVICE_RE.match(device)
    if not match:
        return None
    try:
        logger_num = int(match.group(1))
    except ValueError:
        return None
    return f"Logger {logger_num}"


def _merge_event_only_variants(nodes: Dict[str, Dict[str, Any]]) -> None:
    to_remove: List[str] = []

    def _has_primary_data(node: Dict[str, Any]) -> bool:
        if (
            node.get("soc") is not None
            or node.get("pout") is not None
            or node.get("acuvim_vavg") is not None
            or node.get("acuvim_iavg") is not None
            or node.get("acuvim_p") is not None
        ):
            return True
        return node.get("gps_updated_at") is not None and not node.get("gps_inherited")

    for system_id, node in list(nodes.items()):
        if not node.get("event_id"):
            continue
        if _has_primary_data(node):
            continue
        node_id = _clean_label_value(node.get("node_id"), PLACEHOLDER_NODE_IDS)
        for variant in _system_id_variants(system_id):
            target = nodes.get(variant)
            if not target:
                continue
            if node_id:
                target_node_id = _clean_label_value(
                    target.get("node_id"), PLACEHOLDER_NODE_IDS
                )
                if target_node_id and target_node_id != node_id:
                    continue
            elif not _has_primary_data(target):
                continue
            node_ts = node.get("event_updated_at") or 0
            target_ts = target.get("event_updated_at") or 0
            if node_ts >= target_ts:
                target["event_id"] = node.get("event_id")
                target["event_updated_at"] = node_ts
            if node.get("location") and not target.get("location"):
                target["location"] = node.get("location")
            to_remove.append(system_id)
            break

    for system_id in to_remove:
        nodes.pop(system_id, None)

def _metric_name(base: str) -> str:
    if not METRIC_SUFFIX:
        return base
    return f"{base}{METRIC_SUFFIX}"


def _event_metric_names() -> List[str]:
    base = "ovr_event_active"
    names = [_metric_name(base)]
    if METRIC_SUFFIX and base not in names:
        names.append(base)
    return names


def _event_metric_regex() -> str:
    names = _event_metric_names()
    return "|".join(re.escape(name) for name in names)


def _map_tiles_metric_names() -> List[str]:
    base = "map_tiles_month_total"
    names = [_metric_name(base)]
    if METRIC_SUFFIX and base not in names:
        names.append(base)
    return names


def _build_selector(
    extra_labels: Optional[List[str]] = None,
    deployment_ids: Optional[List[str]] = None,
) -> str:
    labels = []
    if deployment_ids:
        if len(deployment_ids) == 1:
            labels.append(f'deployment_id="{_escape_label(deployment_ids[0])}"')
        else:
            pattern = "|".join(_escape_regex(item) for item in deployment_ids)
            labels.append(f'deployment_id=~"{pattern}"')
    elif DEPLOYMENT_ID:
        labels.append(f'deployment_id="{_escape_label(DEPLOYMENT_ID)}"')
    if extra_labels:
        labels.extend(extra_labels)
    if not labels:
        return ""
    return "{" + ",".join(labels) + "}"


def _build_event_labels(event_ids: Optional[List[str]]) -> List[str]:
    if not event_ids:
        return []
    if len(event_ids) == 1:
        return [f'event_id="{_escape_label(event_ids[0])}"']
    pattern = "|".join(_escape_regex(item) for item in event_ids)
    return [f'event_id=~"{pattern}"']


def _vm_get(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    url = f"{VM_URL}{path}"
    try:
        resp = requests.get(url, params=params, timeout=VM_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return None
        payload = resp.json()
        if payload.get("status") != "success":
            return None
        return payload.get("data")
    except Exception:
        return None


def _vm_query_vector(expr: str) -> List[Dict[str, Any]]:
    data = _vm_get("/api/v1/query", {"query": expr})
    if not data or data.get("resultType") != "vector":
        return []
    return data.get("result") or []


def _vm_query_vector_at(expr: str, ts: int) -> List[Dict[str, Any]]:
    data = _vm_get("/api/v1/query", {"query": expr, "time": ts})
    if not data or data.get("resultType") != "vector":
        return []
    return data.get("result") or []


def _vm_query_vector_status(expr: str) -> tuple[bool, List[Dict[str, Any]]]:
    data = _vm_get("/api/v1/query", {"query": expr})
    if not data or data.get("resultType") != "vector":
        return False, []
    return True, data.get("result") or []


def _vm_label_values(label: str, match: Optional[str]) -> List[str]:
    params: Dict[str, Any] = {}
    if match:
        params["match[]"] = match
    data = _vm_get(f"/api/v1/label/{label}/values", params)
    if not data:
        return []
    return data or []


def _vm_label_values_range(
    label: str,
    match: Optional[str],
    start: Optional[int],
    end: Optional[int],
) -> List[str]:
    params: Dict[str, Any] = {}
    if match:
        params["match[]"] = match
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    data = _vm_get(f"/api/v1/label/{label}/values", params)
    if not data:
        return []
    return data or []


def _default_deployment_ids() -> List[str]:
    if DEPLOYMENT_ID:
        return [DEPLOYMENT_ID]
    values = [
        value
        for value in _vm_label_values("deployment_id", None)
        if not _is_placeholder_deployment(value)
    ]
    unique = sorted(set(values))
    if len(unique) == 1:
        return [unique[0]]
    return []


_REPORT_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_MONTHLY_METRICS = {
    "soc": "victron_battery_soc",
    "pout": "victron_ac_out_power",
}


def _safe_report_slug(value: Optional[str]) -> str:
    if not value:
        return ""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def _parse_month_key(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    if not _REPORT_MONTH_RE.match(value):
        return None
    year_str, month_str = value.split("-")
    try:
        year = int(year_str)
        month = int(month_str)
    except ValueError:
        return None
    if month < 1 or month > 12:
        return None
    return f"{year:04d}-{month:02d}"


def _month_bounds(month_key: str) -> tuple[int, int]:
    year, month = (int(part) for part in month_key.split("-"))
    start = datetime(year, month, 1, tzinfo=REPORTS_TIMEZONE)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=REPORTS_TIMEZONE)
    else:
        end = datetime(year, month + 1, 1, tzinfo=REPORTS_TIMEZONE)
    return int(start.timestamp()), int(end.timestamp())


def _report_base_url() -> str:
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}".rstrip("/")


def _report_node_key(node_id: Optional[str], system_id: Optional[str]) -> str:
    return _safe_report_slug(node_id or system_id or "")


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _find_system_ids_with_data(
    start_ts: int,
    end_ts: int,
    selector: str,
) -> List[str]:
    system_ids: set[str] = set()
    for metric_key in _MONTHLY_METRICS.values():
        match = f"{_metric_name(metric_key)}{selector}"
        values = _vm_label_values_range("system_id", match, start_ts, end_ts)
        for value in values:
            normalized = _normalize_label_value(value)
            if normalized:
                system_ids.add(normalized)
    return sorted(system_ids)


def _apply_stat(target: Dict[str, Dict[str, float]], series: List[Dict[str, Any]], field: str) -> None:
    for entry in series:
        metric = entry.get("metric", {})
        system_id = _normalize_label_value(metric.get("system_id"))
        if not system_id:
            continue
        value = _value_to_float(entry.get("value"))
        if value is None:
            continue
        bucket = target.setdefault(system_id, {})
        bucket[field] = value


def _monthly_metric_stats(
    metric_key: str,
    selector: str,
    range_seconds: int,
    end_ts: int,
) -> Dict[str, Dict[str, float]]:
    metric_name = _metric_name(metric_key)
    range_str = f"{range_seconds}s"
    stats: Dict[str, Dict[str, float]] = {}
    avg_expr = f"avg by (system_id)(avg_over_time({metric_name}{selector}[{range_str}]))"
    min_expr = f"min by (system_id)(min_over_time({metric_name}{selector}[{range_str}]))"
    max_expr = f"max by (system_id)(max_over_time({metric_name}{selector}[{range_str}]))"
    count_expr = f"sum by (system_id)(count_over_time({metric_name}{selector}[{range_str}]))"
    _apply_stat(stats, _vm_query_vector_at(avg_expr, end_ts), "avg")
    _apply_stat(stats, _vm_query_vector_at(min_expr, end_ts), "min")
    _apply_stat(stats, _vm_query_vector_at(max_expr, end_ts), "max")
    _apply_stat(stats, _vm_query_vector_at(count_expr, end_ts), "samples")
    return stats


def _generate_monthly_reports(
    month_key: str,
    deployment_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    month_key = _parse_month_key(month_key)
    if not month_key:
        return []
    start_ts, end_ts = _month_bounds(month_key)
    selector = _build_selector(deployment_ids=deployment_ids)
    range_seconds = end_ts - start_ts
    if range_seconds <= 0:
        return []

    system_ids = _find_system_ids_with_data(start_ts, end_ts, selector)
    if not system_ids:
        return []

    nodes = _load_nodes(deployment_ids)
    node_by_system = {node["system_id"]: node for node in nodes}

    soc_stats = _monthly_metric_stats(
        _MONTHLY_METRICS["soc"], selector, range_seconds, end_ts
    )
    pout_stats = _monthly_metric_stats(
        _MONTHLY_METRICS["pout"], selector, range_seconds, end_ts
    )

    stats_by_system: Dict[str, Dict[str, Dict[str, float]]] = {}
    for system_id in system_ids:
        stats_by_system[system_id] = {
            "soc": soc_stats.get(system_id, {}),
            "pout": pout_stats.get(system_id, {}),
        }

    groups: Dict[str, Dict[str, Any]] = {}
    for system_id in system_ids:
        node = node_by_system.get(system_id)
        node_id = node.get("node_id") if node else None
        group_key = node_id or system_id
        entry = groups.setdefault(
            group_key,
            {
                "node_id": node_id,
                "deployment_id": node.get("deployment_id") if node else None,
                "host_system_id": None,
                "system_ids": [],
            },
        )
        entry["system_ids"].append(system_id)
        if node and not node.get("is_logger") and not entry["host_system_id"]:
            entry["host_system_id"] = system_id

    reports: List[Dict[str, Any]] = []
    generated_at = int(time.time() * 1e9)
    for entry in groups.values():
        system_ids = entry["system_ids"]
        host_system_id = entry["host_system_id"] or system_ids[0]
        node = node_by_system.get(host_system_id, {})
        node_id = entry["node_id"] or node.get("node_id")
        deployment_id = entry["deployment_id"] or node.get("deployment_id")
        node_key = _report_node_key(node_id, host_system_id)
        if not node_key:
            continue
        report = {
            "report_type": "monthly",
            "month": month_key,
            "timezone": REPORTS_TIMEZONE.key,
            "start_time": start_ts,
            "end_time": end_ts,
            "generated_at": generated_at,
            "node_id": node_id,
            "system_id": host_system_id,
            "deployment_id": deployment_id,
            "metrics": stats_by_system.get(host_system_id, {}),
            "loggers": {},
        }
        for system_id in system_ids:
            if system_id == host_system_id:
                continue
            report["loggers"][system_id] = {
                "metrics": stats_by_system.get(system_id, {}),
            }

        report_dir = os.path.join(REPORTS_PATH, "monthly", month_key, node_key)
        _write_json(os.path.join(report_dir, "data.json"), report)
        _write_json(
            os.path.join(report_dir, "meta.json"),
            {
                "report_type": "monthly",
                "month": month_key,
                "node_id": node_id,
                "system_id": host_system_id,
                "deployment_id": deployment_id,
                "generated_at": generated_at,
                "node_key": node_key,
            },
        )
        reports.append(report)
    return reports


def _list_monthly_reports(month_key: str) -> List[Dict[str, Any]]:
    month_key = _parse_month_key(month_key)
    if not month_key:
        return []
    root = os.path.join(REPORTS_PATH, "monthly", month_key)
    if not os.path.isdir(root):
        return []
    reports: List[Dict[str, Any]] = []
    for node_key in os.listdir(root):
        meta = _read_json(os.path.join(root, node_key, "meta.json"))
        if not meta:
            continue
        meta["node_key"] = node_key
        reports.append(meta)
    reports.sort(key=lambda item: item.get("generated_at", 0), reverse=True)
    return reports


def _list_event_reports(event_id: str) -> List[Dict[str, Any]]:
    safe_event = _safe_report_slug(event_id)
    root = os.path.join(REPORTS_PATH, "event", safe_event)
    if not os.path.isdir(root):
        return []
    reports: List[Dict[str, Any]] = []
    for node_key in os.listdir(root):
        node_root = os.path.join(root, node_key)
        if not os.path.isdir(node_root):
            continue
        latest = None
        for stamp in os.listdir(node_root):
            meta = _read_json(os.path.join(node_root, stamp, "meta.json"))
            if not meta:
                continue
            meta["node_key"] = node_key
            meta["event_id"] = meta.get("event_id", event_id)
            if not latest or meta.get("generated_at", 0) > latest.get("generated_at", 0):
                latest = meta
        if latest:
            reports.append(latest)
    reports.sort(key=lambda item: item.get("generated_at", 0), reverse=True)
    return reports


def _list_report_month_keys() -> List[str]:
    root = os.path.join(REPORTS_PATH, "monthly")
    if not os.path.isdir(root):
        return []
    months = [name for name in os.listdir(root) if _parse_month_key(name)]
    return sorted(set(months), reverse=True)


def _list_report_event_ids() -> List[Dict[str, Any]]:
    root = os.path.join(REPORTS_PATH, "event")
    if not os.path.isdir(root):
        return []
    events: List[Dict[str, Any]] = []
    for event_slug in os.listdir(root):
        event_root = os.path.join(root, event_slug)
        if not os.path.isdir(event_root):
            continue
        latest_event: Optional[Dict[str, Any]] = None
        for node_key in os.listdir(event_root):
            node_root = os.path.join(event_root, node_key)
            if not os.path.isdir(node_root):
                continue
            for stamp in os.listdir(node_root):
                meta = _read_json(os.path.join(node_root, stamp, "meta.json"))
                if not meta:
                    continue
                if not latest_event or meta.get("generated_at", 0) > latest_event.get("generated_at", 0):
                    latest_event = meta
        if latest_event:
            events.append(
                {
                    "event_id": latest_event.get("event_id", event_slug),
                    "generated_at": latest_event.get("generated_at", 0),
                }
            )
    events.sort(key=lambda item: item.get("generated_at", 0), reverse=True)
    return events


def _parse_deployment_filter() -> List[str]:
    values = request.args.getlist("deployment_id")
    if not values:
        raw = request.args.get("deployment_ids", "")
        if raw:
            values = [raw]

    deployment_ids: List[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                deployment_ids.append(part)

    seen = set()
    deduped: List[str] = []
    for value in deployment_ids:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    filtered = [value for value in deduped if not _is_placeholder_deployment(value)]
    return filtered or _default_deployment_ids()


def _parse_event_filter() -> List[str]:
    values = request.args.getlist("event_id")
    if not values:
        raw = request.args.get("event_ids", "")
        if raw:
            values = [raw]

    event_ids: List[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                event_ids.append(part)

    seen = set()
    deduped: List[str] = []
    for value in event_ids:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return deduped


def _value_to_float(value: Any) -> Optional[float]:
    if isinstance(value, list) and len(value) >= 2:
        try:
            return float(value[1])
        except Exception:
            return None
    return None


def _value_ts(value: Any) -> Optional[float]:
    if isinstance(value, list) and len(value) >= 1:
        try:
            return float(value[0])
        except Exception:
            return None
    return None


def _month_label(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m")
        return parsed.strftime("%B %Y")
    except Exception:
        return value


def _preference_deployment_id(deployment_ids: Optional[List[str]]) -> str:
    if deployment_ids and len(deployment_ids) == 1:
        return deployment_ids[0]
    if DEPLOYMENT_ID:
        return DEPLOYMENT_ID
    return ""


def _map_tiles_total(provider: str, deployment_ids: Optional[List[str]]) -> tuple[Optional[int], bool]:
    lookback = f"{MAP_TILES_LOOKBACK_SECONDS}s"
    selector = _build_selector([f'provider="{_escape_label(provider)}"'], deployment_ids)
    vm_ok = False
    for metric in _map_tiles_metric_names():
        expr = f"sum(max_over_time({metric}{selector}[{lookback}]))"
        ok, result = _vm_query_vector_status(expr)
        if not ok:
            continue
        vm_ok = True
        if not result:
            return 0, True
        value = _value_to_float(result[0].get("value"))
        if value is None:
            continue
        return int(value), True
    return None, vm_ok


def _load_manual_locations() -> Dict[str, Dict[str, Any]]:
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT system_id, latitude, longitude, label, updated_at FROM manual_locations"
        ).fetchall()
    manual = {}
    for row in rows:
        system_id = _normalize_label_value(row["system_id"])
        if not system_id:
            continue
        manual[system_id] = {
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "label": row["label"],
            "updated_at": row["updated_at"],
        }
    return manual


_NODE_NUM_RE = re.compile(r"(\d+)$")


def _extract_node_num(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    match = _NODE_NUM_RE.search(value)
    if not match:
        return None
    return match.group(1)


def _build_node_url(system_id: str, node_id: Optional[str], deployment_id: Optional[str]) -> Optional[str]:
    node_id = _clean_label_value(node_id, PLACEHOLDER_NODE_IDS)
    deployment_id = _clean_label_value(deployment_id, PLACEHOLDER_DEPLOYMENT_IDS)
    node_num = _extract_node_num(node_id) or _extract_node_num(system_id)

    if NODE_URL_TEMPLATE:
        context = {
            "system_id": system_id,
            "node_id": node_id or "",
            "node_num": node_num or "",
            "deployment_id": deployment_id or "",
        }
        try:
            url = NODE_URL_TEMPLATE.format(**context)
        except Exception:
            return None
        if not url or "{" in url or "}" in url:
            return None
        return url

    node_domain = NODE_DOMAIN
    if not node_domain and BASE_DOMAIN:
        node_domain = f"{NODE_SUBDOMAIN}.{BASE_DOMAIN}" if NODE_SUBDOMAIN else BASE_DOMAIN

    host_id = node_id or system_id
    if not node_domain or not host_id:
        return None

    return f"https://{host_id}.{node_domain}"


def _base_node(system_id: str) -> Dict[str, Any]:
    return {
        "system_id": system_id,
        "node_id": None,
        "deployment_id": None,
        "latitude": None,
        "longitude": None,
        "event_id": None,
        "event_updated_at": None,
        "gps_updated_at": None,
        "gps_age_sec": None,
        "gps_source": "none",
        "gps_inherited": False,
        "soc": None,
        "pout": None,
        "acuvim_vavg": None,
        "acuvim_iavg": None,
        "acuvim_p": None,
        "acuvim_updated_at": None,
        "alerts": [],
        "alerts_count": 0,
        "location": None,
        "node_url": None,
        "manual": None,
        "is_logger": False,
        "host_system_id": None,
    }


def _apply_series(nodes: Dict[str, Dict[str, Any]], series_list: List[Dict[str, Any]], field: str) -> None:
    for series in series_list:
        metric = series.get("metric", {})
        system_id = _normalize_label_value(metric.get("system_id"))
        if not system_id:
            continue
        node = nodes.setdefault(system_id, _base_node(system_id))
        _assign_label_value(node, "node_id", metric.get("node_id"), PLACEHOLDER_NODE_IDS)
        _assign_label_value(
            node, "deployment_id", metric.get("deployment_id"), PLACEHOLDER_DEPLOYMENT_IDS
        )
        value = _value_to_float(series.get("value"))
        if value is None:
            continue
        node[field] = value
        if field in ("latitude", "longitude"):
            ts = _value_ts(series.get("value"))
            if ts:
                node["gps_updated_at"] = max(node.get("gps_updated_at") or 0, ts)


def _build_gps_by_node_id(series_list: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    gps_by_node: Dict[str, Dict[str, float]] = {}
    for series in series_list:
        metric = series.get("metric", {})
        node_id = _clean_label_value(metric.get("node_id"), PLACEHOLDER_NODE_IDS)
        if not node_id:
            continue
        value = _value_to_float(series.get("value"))
        if value is None:
            continue
        ts = _value_ts(series.get("value")) or 0
        current = gps_by_node.get(node_id)
        if not current or ts >= current.get("ts", 0):
            gps_by_node[node_id] = {"value": value, "ts": ts}
    return gps_by_node


def _apply_events(nodes: Dict[str, Dict[str, Any]], series_list: List[Dict[str, Any]]) -> None:
    for series in series_list:
        metric = series.get("metric", {})
        system_id = _normalize_label_value(metric.get("system_id"))
        if not system_id:
            continue
        event_id = _normalize_label_value(metric.get("event_id"))
        value = _value_to_float(series.get("value"))
        if value is None or value < 0.5:
            continue
        ts = _value_ts(series.get("value")) or 0
        node = nodes.setdefault(system_id, _base_node(system_id))
        prev_ts = node.get("event_updated_at") or 0
        if ts < prev_ts:
            continue
        _assign_label_value(node, "node_id", metric.get("node_id"), PLACEHOLDER_NODE_IDS)
        _assign_label_value(
            node, "deployment_id", metric.get("deployment_id"), PLACEHOLDER_DEPLOYMENT_IDS
        )
        node["event_id"] = event_id
        node["event_updated_at"] = ts
        location = _normalize_label_value(metric.get("location"))
        if location and location != "-":
            node["location"] = location


def _apply_alerts(nodes: Dict[str, Dict[str, Any]], series_list: List[Dict[str, Any]]) -> None:
    for series in series_list:
        metric = series.get("metric", {})
        system_id = _normalize_label_value(metric.get("system_id"))
        if not system_id:
            continue
        value = _value_to_float(series.get("value"))
        if value is None or value < 0.5:
            continue
        node = nodes.setdefault(system_id, _base_node(system_id))
        _assign_label_value(node, "node_id", metric.get("node_id"), PLACEHOLDER_NODE_IDS)
        _assign_label_value(
            node, "deployment_id", metric.get("deployment_id"), PLACEHOLDER_DEPLOYMENT_IDS
        )
        name = metric.get("name") or metric.get("__name__", "alarm")
        if name.endswith(":avg"):
            name = name[:-4]
        phase = metric.get("phase")
        if phase and phase not in ("-", ""):
            name = f"{name} {phase}"
        node["alerts"].append(name)


def _apply_acuvim_series(nodes: Dict[str, Dict[str, Any]], series_list: List[Dict[str, Any]], field: str) -> None:
    for series in series_list:
        metric = series.get("metric", {})
        system_id = _acuvim_device_to_logger(metric.get("device"))
        if not system_id:
            continue
        node = nodes.setdefault(system_id, _base_node(system_id))
        node["is_logger"] = True
        location = metric.get("location")
        if location and location != "-" and not node.get("location"):
            node["location"] = location
        _assign_label_value(node, "node_id", metric.get("node_id"), PLACEHOLDER_NODE_IDS)
        _assign_label_value(
            node, "deployment_id", metric.get("deployment_id"), PLACEHOLDER_DEPLOYMENT_IDS
        )
        value = _value_to_float(series.get("value"))
        if value is None:
            continue
        node[field] = value
        ts = _value_ts(series.get("value")) or 0
        if ts:
            node["acuvim_updated_at"] = max(node.get("acuvim_updated_at") or 0, ts)


def _finalize_nodes(nodes: Dict[str, Dict[str, Any]], manual_locations: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    now = time.time()
    host_by_node_id: Dict[str, str] = {}
    for node in nodes.values():
        node_id = _clean_label_value(node.get("node_id"), PLACEHOLDER_NODE_IDS)
        if not node_id:
            continue
        if node.get("is_logger"):
            continue
        if (
            node.get("soc") is None
            and node.get("pout") is None
            and node.get("gps_updated_at") is None
        ):
            continue
        host_by_node_id[node_id] = node["system_id"]

    for node in nodes.values():
        alerts = sorted(set(node["alerts"]))
        node["alerts"] = alerts
        node["alerts_count"] = len(alerts)
        gps_ts = node.get("gps_updated_at")
        if gps_ts:
            node["gps_age_sec"] = max(0, int(now - gps_ts))
        manual = manual_locations.get(node["system_id"])
        if not manual:
            for variant in _system_id_variants(node["system_id"]):
                manual = manual_locations.get(variant)
                if manual:
                    break
        node["manual"] = manual

        gps_valid = node.get("latitude") is not None and node.get("longitude") is not None
        gps_stale = node.get("gps_age_sec") is not None and node["gps_age_sec"] > GPS_STALE_SECONDS

        if (not gps_valid or gps_stale) and manual:
            node["latitude"] = manual["latitude"]
            node["longitude"] = manual["longitude"]
            node["gps_source"] = "manual"
            node["gps_age_sec"] = max(0, int(now - manual["updated_at"]))
        elif gps_valid:
            node["gps_source"] = "gps"

        if node.get("system_id", "").startswith("Logger"):
            node["is_logger"] = True

        node_id = _clean_label_value(node.get("node_id"), PLACEHOLDER_NODE_IDS)
        host_system_id = host_by_node_id.get(node_id) if node_id else None
        node["host_system_id"] = host_system_id or node["system_id"]
        node["node_url"] = _build_node_url(
            node["host_system_id"], node["node_id"], node["deployment_id"]
        )

    return sorted(nodes.values(), key=lambda item: item["system_id"])


def _load_nodes(
    deployment_ids: Optional[List[str]] = None,
    event_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    selector = _build_selector(deployment_ids=deployment_ids)
    event_labels = _build_event_labels(event_ids)
    event_selector = _build_selector(event_labels, deployment_ids=deployment_ids)
    if event_ids:
        event_match = _build_selector(
            [f'__name__=~"{_event_metric_regex()}"'] + event_labels,
            deployment_ids=deployment_ids,
        )
        match = event_match
    else:
        match = selector if selector else None
    system_ids = _vm_label_values("system_id", match)

    nodes: Dict[str, Dict[str, Any]] = {}
    for system_id in system_ids:
        normalized = _normalize_label_value(system_id)
        if not normalized or normalized in nodes:
            continue
        nodes[normalized] = _base_node(normalized)

    lookback = f"{LOOKBACK_SECONDS}s"

    lat_series = _vm_query_vector(
        f"last_over_time({_metric_name('victron_gps_latitude')}{selector}[{lookback}])"
    )
    lon_series = _vm_query_vector(
        f"last_over_time({_metric_name('victron_gps_longitude')}{selector}[{lookback}])"
    )
    soc_series = _vm_query_vector(
        f"last_over_time({_metric_name('victron_battery_soc')}{selector}[{lookback}])"
    )
    pout_series = _vm_query_vector(
        f"last_over_time({_metric_name('victron_ac_out_power')}{selector}[{lookback}])"
    )

    alarm_selector = _build_selector(
        ["__name__=~\"victron_alarm_.*\""],
        deployment_ids=deployment_ids,
    )
    alarms_series = _vm_query_vector(
        f"last_over_time({alarm_selector}[{lookback}])"
    )

    acuvim_vavg_series = _vm_query_vector(
        f"last_over_time({_metric_name('acuvim_Vln')}{selector}[{lookback}])"
    )
    acuvim_iavg_series = _vm_query_vector(
        f"last_over_time({_metric_name('acuvim_I')}{selector}[{lookback}])"
    )
    acuvim_p_series = _vm_query_vector(
        f"last_over_time({_metric_name('acuvim_P')}{selector}[{lookback}])"
    )

    event_series: List[Dict[str, Any]] = []
    for metric in _event_metric_names():
        event_series.extend(
            _vm_query_vector(
                f"last_over_time({metric}{event_selector}[{lookback}])"
            )
        )

    _apply_series(nodes, lat_series, "latitude")
    _apply_series(nodes, lon_series, "longitude")
    _apply_series(nodes, soc_series, "soc")
    _apply_series(nodes, pout_series, "pout")
    _apply_alerts(nodes, alarms_series)
    _apply_acuvim_series(nodes, acuvim_vavg_series, "acuvim_vavg")
    _apply_acuvim_series(nodes, acuvim_iavg_series, "acuvim_iavg")
    _apply_acuvim_series(nodes, acuvim_p_series, "acuvim_p")
    _apply_events(nodes, event_series)

    lat_by_node = _build_gps_by_node_id(lat_series)
    lon_by_node = _build_gps_by_node_id(lon_series)
    for node in nodes.values():
        if node.get("latitude") is not None and node.get("longitude") is not None:
            continue
        node_id = _clean_label_value(node.get("node_id"), PLACEHOLDER_NODE_IDS)
        if not node_id:
            continue
        lat_entry = lat_by_node.get(node_id)
        lon_entry = lon_by_node.get(node_id)
        if not lat_entry or not lon_entry:
            continue
        node["latitude"] = lat_entry["value"]
        node["longitude"] = lon_entry["value"]
        gps_ts = max(
            node.get("gps_updated_at") or 0,
            lat_entry.get("ts", 0),
            lon_entry.get("ts", 0),
        )
        if gps_ts:
            node["gps_updated_at"] = gps_ts
        node["gps_inherited"] = True
        node["is_logger"] = True

    node_event_by_node_id: Dict[str, Dict[str, Any]] = {}
    for node in nodes.values():
        node_id = _clean_label_value(node.get("node_id"), PLACEHOLDER_NODE_IDS)
        if not node_id or not node.get("event_id"):
            continue
        ts = node.get("event_updated_at") or 0
        current = node_event_by_node_id.get(node_id)
        if not current or ts > current.get("ts", 0):
            node_event_by_node_id[node_id] = {
                "event_id": node["event_id"],
                "ts": ts,
            }

    if node_event_by_node_id:
        for node in nodes.values():
            if node.get("event_id"):
                continue
            node_id = _clean_label_value(node.get("node_id"), PLACEHOLDER_NODE_IDS)
            if not node_id:
                continue
            if node.get("is_logger"):
                continue
            entry = node_event_by_node_id.get(node_id)
            if not entry:
                continue
            if (
                node.get("soc") is None
                and node.get("pout") is None
                and node.get("gps_updated_at") is None
            ):
                continue
            node["event_id"] = entry["event_id"]
            node["event_updated_at"] = entry["ts"]

    _merge_event_only_variants(nodes)
    nodes = _merge_system_id_variants(nodes)

    if event_ids:
        nodes = {
            system_id: node
            for system_id, node in nodes.items()
            if node.get("event_id") in event_ids
        }

    manual_locations = _load_manual_locations()
    return _finalize_nodes(nodes, manual_locations)


def _load_active_events(deployment_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    selector = _build_selector(deployment_ids=deployment_ids)
    lookback = f"{LOOKBACK_SECONDS}s"
    series_list: List[Dict[str, Any]] = []
    for metric in _event_metric_names():
        series_list.extend(
            _vm_query_vector(
                f"last_over_time({metric}{selector}[{lookback}])"
            )
        )

    events: Dict[str, Dict[str, Any]] = {}
    for series in series_list:
        metric = series.get("metric", {})
        value = _value_to_float(series.get("value"))
        if value is None or value < 0.5:
            continue
        event_id = _normalize_label_value(metric.get("event_id"))
        if not event_id:
            continue
        location = _normalize_label_value(metric.get("location"))
        entry = events.setdefault(
            event_id,
            {"event_id": event_id, "systems": set(), "locations": set()},
        )
        system_id = _normalize_label_value(metric.get("system_id"))
        if system_id:
            entry["systems"].add(system_id)
        if location and location != "-":
            entry["locations"].add(location)

    result = []
    for event_id in sorted(events.keys()):
        entry = events[event_id]
        result.append(
            {
                "event_id": event_id,
                "count": len(entry["systems"]),
                "systems": sorted(entry["systems"]),
                "locations": sorted(entry["locations"]),
            }
        )
    return result


@app.route("/api/fleet/map-tiles/status", methods=["GET"])
def api_fleet_map_tiles_status() -> Any:
    deployment_ids = _parse_deployment_filter()
    month_key = utc_month_key()
    thresholds = {
        "mapbox": MAPBOX_FREE_TILES_PER_MONTH,
        "esri": ESRI_FREE_TILES_PER_MONTH,
        "guardrailPct": GUARDRAIL_LIMIT_PCT,
    }

    fleet_counts: Dict[str, Optional[int]] = {}
    vm_ok = True
    for provider in MAP_TILE_PROVIDERS:
        total, ok = _map_tiles_total(provider, deployment_ids if deployment_ids else None)
        if not ok:
            vm_ok = False
        fleet_counts[provider] = total if ok else None

    deployment_id = _preference_deployment_id(deployment_ids if deployment_ids else None)
    with _get_db() as conn:
        preferred_provider = get_preferred_provider(conn, deployment_id)

    if not preferred_provider or preferred_provider not in MAP_TILE_PROVIDERS:
        preferred_provider = MAP_DEFAULT_PROVIDER if MAP_DEFAULT_PROVIDER in MAP_TILE_PROVIDERS else "esri"

    guardrail = build_guardrail_status(
        preferred_provider,
        fleet_counts,
        {"mapbox": MAPBOX_FREE_TILES_PER_MONTH, "esri": ESRI_FREE_TILES_PER_MONTH},
        GUARDRAIL_LIMIT_PCT,
        _month_label(month_key),
    )

    warning = guardrail["warning"]
    if not vm_ok:
        warning = warning or "Fleet totals unavailable."

    resp = jsonify({
        "month_key": month_key,
        "thresholds": thresholds,
        "local": None,
        "fleet": fleet_counts,
        "pct": guardrail["pct"],
        "blocked": guardrail["blocked"],
        "preferredProvider": preferred_provider,
        "recommendedProvider": guardrail["recommended_provider"],
        "warning": warning,
        "providers": MAP_TILE_PROVIDERS_CONFIG,
    })
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/fleet/map-provider/preferred", methods=["GET"])
def api_fleet_map_provider_get() -> Any:
    deployment_ids = _parse_deployment_filter()
    deployment_id = _preference_deployment_id(deployment_ids if deployment_ids else None)
    with _get_db() as conn:
        preferred_provider = get_preferred_provider(conn, deployment_id)

    if not preferred_provider or preferred_provider not in MAP_TILE_PROVIDERS:
        preferred_provider = MAP_DEFAULT_PROVIDER if MAP_DEFAULT_PROVIDER in MAP_TILE_PROVIDERS else "esri"

    return jsonify({"preferredProvider": preferred_provider})


@app.route("/api/fleet/map-provider/preferred", methods=["POST"])
def api_fleet_map_provider_set() -> Any:
    payload = request.get_json(silent=True) or {}
    provider = str(payload.get("provider", "")).strip().lower()
    if not is_valid_provider(provider):
        return jsonify({"error": "provider must be mapbox or esri"}), 400

    deployment_id = str(payload.get("deployment_id", "")).strip() or DEPLOYMENT_ID
    deployment_ids = [deployment_id] if deployment_id else None

    fleet_counts: Dict[str, Optional[int]] = {}
    vm_ok = True
    for name in MAP_TILE_PROVIDERS:
        total, ok = _map_tiles_total(name, deployment_ids)
        if not ok:
            vm_ok = False
        fleet_counts[name] = total if ok else None

    preferred_provider = provider
    guardrail = build_guardrail_status(
        preferred_provider,
        fleet_counts,
        {"mapbox": MAPBOX_FREE_TILES_PER_MONTH, "esri": ESRI_FREE_TILES_PER_MONTH},
        GUARDRAIL_LIMIT_PCT,
    )

    if guardrail["blocked"].get(provider):
        return (
            jsonify(
                {
                    "error": "provider blocked by guardrail",
                    "preferredProvider": preferred_provider,
                    "recommendedProvider": guardrail["recommended_provider"],
                    "warning": guardrail["warning"],
                    "fleet": fleet_counts,
                }
            ),
            409,
        )

    with _get_db() as conn:
        set_preferred_provider(conn, deployment_id, provider)
        conn.commit()

    response = {
        "ok": True,
        "provider": provider,
        "warning": guardrail["warning"],
    }
    if not vm_ok:
        response["warning"] = response["warning"] or "Fleet totals unavailable."
    return jsonify(response)


@app.route("/api/health", methods=["GET"])
def api_health() -> Any:
    version = os.getenv("APP_VERSION") or "dev"
    git_sha = os.getenv("GIT_SHA", "")
    return jsonify({"status": "ok", "version": version, "git_sha": git_sha})


@app.route("/api/deployments", methods=["GET"])
def api_deployments() -> Any:
    deployments = sorted(set(_vm_label_values("deployment_id", None)))
    deployments = [
        value
        for value in deployments
        if value and not _is_placeholder_deployment(value)
    ]
    if not deployments and DEPLOYMENT_ID:
        deployments = [DEPLOYMENT_ID]
    return jsonify({"deployments": deployments})


@app.route("/api/nodes", methods=["GET"])
def api_nodes() -> Any:
    deployment_ids = _parse_deployment_filter()
    event_ids = _parse_event_filter()
    nodes = _load_nodes(
        deployment_ids if deployment_ids else None,
        event_ids if event_ids else None,
    )
    resp = jsonify({
        "generated_at": int(time.time()),
        "nodes": nodes,
    })
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/events", methods=["GET"])
def api_events() -> Any:
    deployment_ids = _parse_deployment_filter()
    events = _load_active_events(deployment_ids if deployment_ids else None)
    resp = jsonify({"events": events})
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/reports", methods=["GET"])
def api_reports_summary() -> Any:
    months = _list_report_month_keys()
    current_month = datetime.now(REPORTS_TIMEZONE).strftime("%Y-%m")
    if current_month not in months:
        start_ts, end_ts = _month_bounds(current_month)
        selector = _build_selector()
        if _find_system_ids_with_data(start_ts, end_ts, selector):
            months = [current_month] + months
    events = _list_report_event_ids()
    resp = jsonify({"months": months, "events": events})
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/reports/monthly", methods=["GET"])
def api_reports_monthly() -> Any:
    month_key = _parse_month_key(request.args.get("month", ""))
    if not month_key:
        return jsonify({"error": "month required (YYYY-MM)"}), 400
    deployment_ids = _parse_deployment_filter()
    _generate_monthly_reports(month_key, deployment_ids if deployment_ids else None)
    reports = _list_monthly_reports(month_key)
    base_url = _report_base_url()
    for report in reports:
        node_key = report.get("node_key")
        if not node_key:
            continue
        report["report_url"] = f"{base_url}/api/reports/monthly/{month_key}/{node_key}"
        report["download_url"] = f"{base_url}/api/reports/monthly/{month_key}/{node_key}?download=1"
    resp = jsonify({"month": month_key, "reports": reports})
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/reports/event", methods=["GET"])
def api_reports_event() -> Any:
    event_id = request.args.get("event_id", "").strip()
    if not event_id:
        return jsonify({"error": "event_id required"}), 400
    reports = _list_event_reports(event_id)
    base_url = _report_base_url()
    safe_event = quote(event_id, safe="")
    for report in reports:
        node_key = report.get("node_key")
        if not node_key:
            continue
        report["report_url"] = f"{base_url}/api/reports/event/{safe_event}/{node_key}"
        report["download_url"] = f"{base_url}/api/reports/event/{safe_event}/{node_key}?download=1"
        report["report_html_url"] = (
            f"{base_url}/api/reports/event/{safe_event}/{node_key}/html"
        )
    resp = jsonify({"event_id": event_id, "reports": reports})
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/reports/monthly/<month_key>/<node_key>", methods=["GET"])
def api_reports_monthly_get(month_key: str, node_key: str) -> Any:
    month_key = _parse_month_key(month_key)
    if not month_key:
        return jsonify({"error": "invalid month"}), 400
    safe_node = _safe_report_slug(node_key)
    if safe_node != node_key:
        return jsonify({"error": "invalid node"}), 400
    report_dir = os.path.join(REPORTS_PATH, "monthly", month_key, safe_node)
    report_path = os.path.join(report_dir, "data.json")
    if not os.path.exists(report_path):
        return jsonify({"error": "report not found"}), 404
    download = request.args.get("download") == "1"
    filename = f"monthly_{month_key}_{safe_node}.json"
    return send_from_directory(
        report_dir,
        "data.json",
        as_attachment=download,
        download_name=filename,
        mimetype="application/json",
    )


@app.route("/api/reports/event/<event_id>/<node_key>", methods=["GET"])
def api_reports_event_get(event_id: str, node_key: str) -> Any:
    safe_event = _safe_report_slug(event_id)
    safe_node = _safe_report_slug(node_key)
    if safe_node != node_key or not safe_event:
        return jsonify({"error": "invalid request"}), 400
    report_root = os.path.join(REPORTS_PATH, "event", safe_event, safe_node)
    if not os.path.isdir(report_root):
        return jsonify({"error": "report not found"}), 404
    latest_dir = None
    for stamp in os.listdir(report_root):
        candidate = os.path.join(report_root, stamp)
        if not os.path.isdir(candidate):
            continue
        if not latest_dir or stamp > latest_dir[0]:
            latest_dir = (stamp, candidate)
    if not latest_dir:
        return jsonify({"error": "report not found"}), 404
    report_dir = latest_dir[1]
    report_path = os.path.join(report_dir, "data.json")
    if not os.path.exists(report_path):
        return jsonify({"error": "report not found"}), 404
    download = request.args.get("download") == "1"
    filename = f"event_{safe_event}_{safe_node}.json"
    return send_from_directory(
        report_dir,
        "data.json",
        as_attachment=download,
        download_name=filename,
        mimetype="application/json",
    )


@app.route("/api/reports/event/<event_id>/<node_key>/html", methods=["GET"])
def api_reports_event_html(event_id: str, node_key: str) -> Any:
    safe_event = _safe_report_slug(event_id)
    safe_node = _safe_report_slug(node_key)
    if safe_node != node_key or not safe_event:
        return jsonify({"error": "invalid request"}), 400
    report_root = os.path.join(REPORTS_PATH, "event", safe_event, safe_node)
    if not os.path.isdir(report_root):
        return "Report not found", 404
    latest_dir = None
    for stamp in os.listdir(report_root):
        candidate = os.path.join(report_root, stamp)
        if not os.path.isdir(candidate):
            continue
        if not latest_dir or stamp > latest_dir[0]:
            latest_dir = (stamp, candidate)
    if not latest_dir:
        return "Report not found", 404
    report_dir = latest_dir[1]
    html_path = os.path.join(report_dir, "report.html")
    if os.path.exists(html_path):
        return send_from_directory(report_dir, "report.html", mimetype="text/html")
    report_path = os.path.join(report_dir, "data.json")
    if not os.path.exists(report_path):
        return "Report not found", 404
    try:
        with open(report_path, "r") as handle:
            payload = json.load(handle)
        pretty = json.dumps(payload, indent=2)
    except Exception:
        return "Report not found", 404
    html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Event report</title>
    <style>
      body {{ font-family: Arial, sans-serif; padding: 24px; background: #f8fafc; }}
      pre {{ background: #fff; border: 1px solid #e2e8f0; padding: 16px; border-radius: 8px; }}
    </style>
  </head>
  <body>
    <h1>Event report</h1>
    <pre>{pretty}</pre>
  </body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/reports/upload", methods=["POST"])
def api_reports_upload() -> Any:
    if REPORTS_UPLOAD_TOKEN:
        token = request.headers.get("X-Report-Token", "").strip()
        if token != REPORTS_UPLOAD_TOKEN:
            return jsonify({"error": "invalid token"}), 401
    payload = request.get_json(silent=True) or {}
    report_type = (payload.get("report_type") or "event").strip().lower()
    if report_type != "event":
        return jsonify({"error": "unsupported report type"}), 400

    report = payload.get("report") or {}
    report_html = payload.get("report_html")
    event_id = (payload.get("event_id") or report.get("event_id") or "").strip()
    if not event_id:
        return jsonify({"error": "event_id required"}), 400
    node_id = payload.get("node_id") or report.get("node_id")
    system_id = payload.get("system_id") or report.get("system_id")
    deployment_id = payload.get("deployment_id") or report.get("deployment_id")
    generated_at = payload.get("generated_at") or report.get("generated_at") or int(time.time() * 1e9)

    safe_event = _safe_report_slug(event_id)
    node_key = _report_node_key(node_id, system_id)
    if not safe_event or not node_key:
        return jsonify({"error": "invalid report identifiers"}), 400
    timestamp_str = datetime.fromtimestamp(generated_at / 1e9).strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join(REPORTS_PATH, "event", safe_event, node_key, timestamp_str)

    report["report_type"] = "event"
    report["event_id"] = event_id
    if node_id:
        report["node_id"] = node_id
    if system_id:
        report["system_id"] = system_id
    if deployment_id:
        report["deployment_id"] = deployment_id
    report["generated_at"] = generated_at

    _write_json(os.path.join(report_dir, "data.json"), report)
    if report_html:
        html_path = os.path.join(report_dir, "report.html")
        try:
            with open(html_path, "w", encoding="utf-8") as handle:
                handle.write(report_html)
        except Exception:
            pass
    _write_json(
        os.path.join(report_dir, "meta.json"),
        {
            "report_type": "event",
            "event_id": event_id,
            "node_id": node_id,
            "system_id": system_id,
            "deployment_id": deployment_id,
            "generated_at": generated_at,
            "node_key": node_key,
        },
    )

    base_url = _report_base_url()
    safe_event_url = quote(event_id, safe="")
    report_url = f"{base_url}/api/reports/event/{safe_event_url}/{node_key}"
    return jsonify({"success": True, "report_url": report_url})


@app.route("/api/nodes/manual", methods=["POST"])
def api_set_manual() -> Any:
    payload = request.get_json(silent=True) or {}
    system_id = str(payload.get("system_id", "")).strip()
    if not system_id:
        return jsonify({"error": "system_id required"}), 400

    try:
        latitude = float(payload.get("latitude"))
        longitude = float(payload.get("longitude"))
    except Exception:
        return jsonify({"error": "latitude and longitude required"}), 400

    label = payload.get("label")
    updated_at = int(time.time())

    with _get_db() as conn:
        conn.execute(
            """
            INSERT INTO manual_locations (system_id, latitude, longitude, label, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(system_id) DO UPDATE SET
              latitude=excluded.latitude,
              longitude=excluded.longitude,
              label=excluded.label,
              updated_at=excluded.updated_at
            """,
            (system_id, latitude, longitude, label, updated_at),
        )
        conn.commit()

    return jsonify({"ok": True, "system_id": system_id, "updated_at": updated_at})


@app.route("/api/nodes/manual/<system_id>", methods=["DELETE"])
def api_clear_manual(system_id: str) -> Any:
    system_id = system_id.strip()
    if not system_id:
        return jsonify({"error": "system_id required"}), 400

    with _get_db() as conn:
        conn.execute("DELETE FROM manual_locations WHERE system_id = ?", (system_id,))
        conn.commit()

    return jsonify({"ok": True, "system_id": system_id})


if __name__ == "__main__":
    _init_db()
    app.run(host="0.0.0.0", port=8081)
