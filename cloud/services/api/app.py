import json
import logging
import os
import re
import sqlite3
import threading
import time
import hashlib
import uuid
import html as html_lib
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
    build_tile_policy,
    is_valid_provider,
    record_tile_usage,
    get_tile_usage_totals,
)

app = Flask(__name__)
logger = logging.getLogger(__name__)

VM_URL = os.getenv("FLEET_VM_URL", "http://victoria-metrics:8428").rstrip("/")
VM_WRITE_URL = os.getenv(
    "FLEET_VM_WRITE_URL", "http://victoria-metrics:8428/write"
).strip()
METRIC_SUFFIX = os.getenv("FLEET_METRIC_SUFFIX", ":10s_avg")
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
MAP_TILE_LIMIT_MAPBOX = int(
    os.getenv("MAP_TILE_LIMIT_MAPBOX", str(MAPBOX_FREE_TILES_PER_MONTH))
)
MAP_TILE_LIMIT_ESRI = int(
    os.getenv("MAP_TILE_LIMIT_ESRI", str(ESRI_FREE_TILES_PER_MONTH))
)
MAP_TILE_SWITCH_THRESHOLD = float(
    os.getenv("MAP_TILE_SWITCH_THRESHOLD", str(GUARDRAIL_LIMIT_PCT))
)
MAP_TILE_DISABLE_THRESHOLD = float(os.getenv("MAP_TILE_DISABLE_THRESHOLD", "1.0"))
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
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  ended_at INTEGER
);
CREATE TABLE IF NOT EXISTS events_registry (
  event_id TEXT PRIMARY KEY,
  event_name TEXT NOT NULL,
  event_name_norm TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  started_at INTEGER NOT NULL,
  ended_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_registry_name_norm
  ON events_registry(event_name_norm);
CREATE TABLE IF NOT EXISTS event_nodes (
  event_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  joined_at INTEGER NOT NULL,
  ended_at INTEGER,
  PRIMARY KEY (event_id, node_id)
);
CREATE TABLE IF NOT EXISTS event_aliases (
  event_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  temp_event_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (event_id, node_id, temp_event_id)
);
CREATE INDEX IF NOT EXISTS idx_event_aliases_temp ON event_aliases (temp_event_id);
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
# Cloud event storage
# ============================================================================

EVENT_STATUSES = {"active", "ended"}


def _event_timestamp() -> int:
    return int(time.time())


def _generate_event_id() -> str:
    return str(uuid.uuid4())


def _event_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "name": row["name"],
        "status": row["status"],
        "created_at": row["created_at"],
        "ended_at": row["ended_at"],
    }


def _list_events(status: str = "active") -> List[Dict[str, Any]]:
    if status not in {"active", "ended", "all"}:
        status = "active"
    query = "SELECT event_id, name, status, created_at, ended_at FROM events"
    params: List[Any] = []
    if status != "all":
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    with _get_db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_event_row(row) for row in rows]


def _get_event(event_id: str) -> Optional[Dict[str, Any]]:
    with _get_db() as conn:
        row = conn.execute(
            "SELECT event_id, name, status, created_at, ended_at FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if not row:
            return None
        event = _event_row(row)
        nodes = conn.execute(
            "SELECT node_id, joined_at, ended_at FROM event_nodes WHERE event_id = ? ORDER BY joined_at DESC",
            (event_id,),
        ).fetchall()
    event["nodes"] = [
        {"node_id": node["node_id"], "joined_at": node["joined_at"], "ended_at": node["ended_at"]}
        for node in nodes
    ]
    return event


def _create_event(name: str) -> Dict[str, Any]:
    event_id = _generate_event_id()
    created_at = _event_timestamp()
    with _get_db() as conn:
        conn.execute(
            """
            INSERT INTO events (event_id, name, status, created_at, ended_at)
            VALUES (?, ?, ?, ?, NULL)
            """,
            (event_id, name, "active", created_at),
        )
        conn.commit()
    return {
        "event_id": event_id,
        "name": name,
        "status": "active",
        "created_at": created_at,
        "ended_at": None,
    }


def _end_event(event_id: str) -> Optional[Dict[str, Any]]:
    ended_at = _event_timestamp()
    with _get_db() as conn:
        row = conn.execute(
            "SELECT event_id, name, status, created_at, ended_at FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE events SET status = ?, ended_at = ? WHERE event_id = ?",
            ("ended", ended_at, event_id),
        )
        conn.commit()
    event = _event_row(row)
    event["status"] = "ended"
    event["ended_at"] = ended_at
    return event


def _add_event_node(event_id: str, node_id: str) -> bool:
    joined_at = _event_timestamp()
    with _get_db() as conn:
        existing = conn.execute(
            "SELECT node_id FROM event_nodes WHERE event_id = ? AND node_id = ?",
            (event_id, node_id),
        ).fetchone()
        if existing:
            return False
        conn.execute(
            """
            INSERT INTO event_nodes (event_id, node_id, joined_at, ended_at)
            VALUES (?, ?, ?, NULL)
            """,
            (event_id, node_id, joined_at),
        )
        conn.commit()
    return True


def _end_event_node(event_id: str, node_id: str) -> bool:
    ended_at = _event_timestamp()
    with _get_db() as conn:
        row = conn.execute(
            "SELECT node_id FROM event_nodes WHERE event_id = ? AND node_id = ?",
            (event_id, node_id),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE event_nodes SET ended_at = ? WHERE event_id = ? AND node_id = ?",
            (ended_at, event_id, node_id),
        )
        conn.commit()
    return True


def _add_event_alias(event_id: str, node_id: str, temp_event_id: str) -> None:
    created_at = _event_timestamp()
    with _get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO event_aliases (event_id, node_id, temp_event_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (event_id, node_id, temp_event_id, created_at),
        )
        conn.commit()


def _load_event_alias_map() -> Dict[tuple[str, str], str]:
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT event_id, node_id, temp_event_id FROM event_aliases"
        ).fetchall()
    return {
        (row["temp_event_id"], row["node_id"]): row["event_id"]
        for row in rows
    }


def _alias_temp_event_ids() -> set[str]:
    with _get_db() as conn:
        rows = conn.execute("SELECT temp_event_id FROM event_aliases").fetchall()
    return {row["temp_event_id"] for row in rows if row["temp_event_id"]}

# ============================================================================
# Event Registry
# ============================================================================

_EVENT_NAME_SPACE_RE = re.compile(r"\s+")


def _clean_event_name(value: Any) -> str:
    if value is None:
        return ""
    return _EVENT_NAME_SPACE_RE.sub(" ", str(value).strip())


def _normalize_event_name(value: Any) -> str:
    return _clean_event_name(value).lower()


def _parse_event_timestamp(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        ts = int(float(value))
    except Exception:
        return None
    if ts <= 0:
        return None
    if ts > 1_000_000_000_000_000:
        return int(ts / 1_000_000_000)
    if ts > 1_000_000_000_000:
        return int(ts / 1_000)
    return ts


def _event_registry_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "event_name": row["event_name"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
    }


def _event_registry_status(entry: Dict[str, Any]) -> str:
    return "ended" if entry.get("ended_at") else "active"


def _resolve_event_registry(value: str) -> Optional[Dict[str, Any]]:
    cleaned = _clean_event_name(value)
    if not cleaned:
        return None
    event = _get_event_registry(cleaned)
    if event:
        return event
    name_norm = _normalize_event_name(cleaned)
    if not name_norm:
        return None
    return _get_event_registry_by_norm(name_norm)


def _resolve_event_registry_id(value: str) -> Optional[str]:
    event = _resolve_event_registry(value)
    return event["event_id"] if event else None


def _get_event_registry(event_id: str) -> Optional[Dict[str, Any]]:
    with _get_db() as conn:
        row = conn.execute(
            """
            SELECT event_id, event_name, created_at, started_at, ended_at
            FROM events_registry WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
    if not row:
        return None
    return _event_registry_row(row)


def _get_event_registry_by_norm(name_norm: str) -> Optional[Dict[str, Any]]:
    if not name_norm:
        return None
    with _get_db() as conn:
        row = conn.execute(
            """
            SELECT event_id, event_name, created_at, started_at, ended_at
            FROM events_registry WHERE event_name_norm = ?
            """,
            (name_norm,),
        ).fetchone()
    if not row:
        return None
    return _event_registry_row(row)


def _list_event_registry(
    status: str = "all",
    event_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if event_id:
        clauses.append("event_id = ?")
        params.append(event_id)
    if status == "active":
        clauses.append("ended_at IS NULL")
    elif status == "ended":
        clauses.append("ended_at IS NOT NULL")
    query = """
        SELECT event_id, event_name, created_at, started_at, ended_at
        FROM events_registry
    """
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY started_at DESC, created_at DESC"
    with _get_db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_event_registry_row(row) for row in rows]


def _event_registry_lookup(event_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not event_ids:
        return {}
    placeholders = ",".join(["?"] * len(event_ids))
    query = f"""
        SELECT event_id, event_name, created_at, started_at, ended_at
        FROM events_registry WHERE event_id IN ({placeholders})
    """
    with _get_db() as conn:
        rows = conn.execute(query, event_ids).fetchall()
    return {row["event_id"]: _event_registry_row(row) for row in rows}


def _ensure_legacy_event(event_id: str, name: str, created_at: int) -> None:
    if not event_id:
        return
    with _get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO events (event_id, name, status, created_at, ended_at)
            VALUES (?, ?, ?, ?, NULL)
            """,
            (event_id, name, "active", created_at),
        )
        conn.commit()


def _mark_legacy_event_ended(event_id: str, ended_at: int) -> None:
    if not event_id:
        return
    with _get_db() as conn:
        conn.execute(
            "UPDATE events SET status = ?, ended_at = ? WHERE event_id = ?",
            ("ended", ended_at, event_id),
        )
        conn.commit()


def _create_event_registry_entry(
    event_name: str, started_at: Optional[int]
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    cleaned = _clean_event_name(event_name)
    if not cleaned:
        return None, None
    name_norm = _normalize_event_name(cleaned)
    existing = _get_event_registry_by_norm(name_norm)
    if existing:
        return None, existing
    created_at = _event_timestamp()
    started_at = started_at or created_at
    event_id = _generate_event_id()
    try:
        with _get_db() as conn:
            conn.execute(
                """
                INSERT INTO events_registry
                (event_id, event_name, event_name_norm, created_at, started_at, ended_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (event_id, cleaned, name_norm, created_at, started_at),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        existing = _get_event_registry_by_norm(name_norm)
        return None, existing

    _ensure_legacy_event(event_id, cleaned, created_at)
    return {
        "event_id": event_id,
        "event_name": cleaned,
        "created_at": created_at,
        "started_at": started_at,
        "ended_at": None,
    }, None


def _end_event_registry(event_id: str, ended_at: int) -> bool:
    if not event_id:
        return False
    with _get_db() as conn:
        row = conn.execute(
            "SELECT event_id FROM events_registry WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE events_registry SET ended_at = ? WHERE event_id = ?",
            (ended_at, event_id),
        )
        conn.commit()
    return True

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


def _tile_thresholds() -> Dict[str, float]:
    return {
        "mapbox": MAP_TILE_LIMIT_MAPBOX,
        "esri": MAP_TILE_LIMIT_ESRI,
        "guardrailPct": MAP_TILE_SWITCH_THRESHOLD,
        "disablePct": MAP_TILE_DISABLE_THRESHOLD,
    }

def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _escape_regex(value: str) -> str:
    return re.escape(value)

def _escape_tag_value(value: Optional[str]) -> str:
    if not value:
        return ""
    value = str(value)
    value = value.replace("\\", "\\\\")
    value = value.replace(",", "\\,")
    value = value.replace("=", "\\=")
    value = value.replace(" ", "\\ ")
    return value


def _escape_measurement(value: str) -> str:
    if not value:
        return "metric"
    value = str(value)
    value = value.replace("\\", "\\\\")
    value = value.replace(",", "\\,")
    value = value.replace(" ", "\\ ")
    return value


def _build_event_line(
    event_id: str,
    system_id: str,
    node_id: Optional[str],
    deployment_id: Optional[str],
    location: Optional[str],
    active: int,
    ts_ns: int,
) -> Optional[str]:
    if not event_id or not system_id:
        return None
    tags = [
        f"event_id={_escape_tag_value(event_id)}",
        f"system_id={_escape_tag_value(system_id)}",
        f"location={_escape_tag_value(location or '-')}",
    ]
    cleaned_node_id = _clean_label_value(node_id, PLACEHOLDER_NODE_IDS)
    cleaned_deployment_id = _clean_label_value(deployment_id, PLACEHOLDER_DEPLOYMENT_IDS)
    if cleaned_node_id:
        tags.append(f"node_id={_escape_tag_value(cleaned_node_id)}")
    if cleaned_deployment_id:
        tags.append(f"deployment_id={_escape_tag_value(cleaned_deployment_id)}")
    return (
        f"{_escape_measurement('ovr_event')},"
        + ",".join(tags)
        + f" active={int(active)}i {ts_ns}"
    )


def _vm_write_lines(lines: List[str]) -> tuple[bool, str]:
    if not lines:
        return False, "no lines to write"
    if not VM_WRITE_URL:
        return False, "vm write url not configured"
    url = VM_WRITE_URL.rstrip("/")
    payload = "\n".join(lines)
    try:
        resp = requests.post(
            url,
            data=payload.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            timeout=VM_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return False, f"vm write error: {exc}"
    if resp.status_code >= 300:
        return False, f"vm write failed: {resp.status_code}"
    return True, ""

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
    "soc": "victron_battery_soc_value",
    "pout": "victron_vebus_ac_out_p_value",
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


def _write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _sha256_bytes(data: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(data)
    return digest.hexdigest()


def _report_event_root(event_id: str) -> str:
    return os.path.join(REPORTS_PATH, _safe_report_slug(event_id))


def _report_node_root(event_id: str, node_key: str) -> str:
    return os.path.join(_report_event_root(event_id), _safe_report_slug(node_key))


def _report_aggregate_root(event_id: str) -> str:
    return os.path.join(_report_event_root(event_id), "aggregate")


def _report_paths(event_id: str, node_key: str) -> Dict[str, str]:
    base = _report_node_root(event_id, node_key)
    return {
        "json": os.path.join(base, "report.json"),
        "html": os.path.join(base, "report.html"),
        "meta": os.path.join(base, "meta.json"),
    }


def _aggregate_paths(event_id: str) -> Dict[str, str]:
    base = _report_aggregate_root(event_id)
    return {
        "json": os.path.join(base, "aggregate.json"),
        "html": os.path.join(base, "aggregate.html"),
    }


def _store_report_bundle(
    event_id: str,
    node_key: str,
    report: Dict[str, Any],
    report_html: Optional[str],
    metadata: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    safe_event = _safe_report_slug(event_id)
    safe_node = _safe_report_slug(node_key)
    if not safe_event or not safe_node:
        return None
    paths = _report_paths(event_id, node_key)
    report_bytes = json.dumps(report, indent=2).encode("utf-8")
    json_hash = _sha256_bytes(report_bytes)
    _write_text(paths["json"], report_bytes.decode("utf-8"))
    html_hash = None
    if report_html:
        html_bytes = report_html.encode("utf-8")
        html_hash = _sha256_bytes(html_bytes)
        _write_text(paths["html"], report_html)

    meta = {
        "event_id": event_id,
        "event_id_original": metadata.get("event_id_original") or metadata.get("temp_event_id"),
        "node_id": metadata.get("node_id"),
        "system_id": metadata.get("system_id"),
        "deployment_id": metadata.get("deployment_id"),
        "generated_at": metadata.get("generated_at"),
        "content_type_json": "application/json",
        "content_type_html": "text/html" if report_html else None,
        "sha256_json": json_hash,
        "sha256_html": html_hash,
        "version": metadata.get("version", "v1"),
        "node_key": node_key,
    }
    _write_json(paths["meta"], meta)
    return meta


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
    reports_by_node: Dict[str, Dict[str, Any]] = {}

    event_root = _report_event_root(event_id)
    if os.path.isdir(event_root):
        for node_key in os.listdir(event_root):
            if node_key == "aggregate":
                continue
            node_root = os.path.join(event_root, node_key)
            if not os.path.isdir(node_root):
                continue
            meta = _read_json(os.path.join(node_root, "meta.json"))
            if not meta:
                continue
            meta["node_key"] = node_key
            meta["event_id"] = meta.get("event_id", event_id)
            meta["storage"] = "v2"
            meta["report_dir"] = node_root
            reports_by_node[node_key] = meta

    safe_event = _safe_report_slug(event_id)
    legacy_root = os.path.join(REPORTS_PATH, "event", safe_event)
    if os.path.isdir(legacy_root):
        for node_key in os.listdir(legacy_root):
            node_root = os.path.join(legacy_root, node_key)
            if not os.path.isdir(node_root):
                continue
            latest = None
            latest_dir = None
            for stamp in os.listdir(node_root):
                report_dir = os.path.join(node_root, stamp)
                meta = _read_json(os.path.join(report_dir, "meta.json"))
                if not meta:
                    continue
                meta["node_key"] = node_key
                meta["event_id"] = meta.get("event_id", event_id)
                if not latest or meta.get("generated_at", 0) > latest.get("generated_at", 0):
                    latest = meta
                    latest_dir = report_dir
            if not latest:
                continue
            latest["storage"] = "legacy"
            latest["report_dir"] = latest_dir
            existing = reports_by_node.get(node_key)
            if not existing or latest.get("generated_at", 0) > existing.get("generated_at", 0):
                reports_by_node[node_key] = latest

    reports = list(reports_by_node.values())
    reports.sort(key=lambda item: item.get("generated_at", 0), reverse=True)
    return reports


def _list_report_month_keys() -> List[str]:
    root = os.path.join(REPORTS_PATH, "monthly")
    if not os.path.isdir(root):
        return []
    months = [name for name in os.listdir(root) if _parse_month_key(name)]
    return sorted(set(months), reverse=True)


def _list_report_event_ids() -> List[Dict[str, Any]]:
    events: Dict[str, Dict[str, Any]] = {}

    if os.path.isdir(REPORTS_PATH):
        for entry in os.listdir(REPORTS_PATH):
            if entry in {"monthly", "event"}:
                continue
            event_root = os.path.join(REPORTS_PATH, entry)
            if not os.path.isdir(event_root):
                continue
            latest_event: Optional[Dict[str, Any]] = None
            node_count = 0
            for node_key in os.listdir(event_root):
                if node_key == "aggregate":
                    continue
                node_root = os.path.join(event_root, node_key)
                if not os.path.isdir(node_root):
                    continue
                meta = _read_json(os.path.join(node_root, "meta.json"))
                if not meta:
                    continue
                node_count += 1
                if not latest_event or meta.get("generated_at", 0) > latest_event.get("generated_at", 0):
                    latest_event = meta
            if latest_event:
                event_id = latest_event.get("event_id") or entry
                existing = events.get(event_id)
                candidate = {
                    "event_id": event_id,
                    "generated_at": latest_event.get("generated_at", 0),
                    "node_count": node_count,
                }
                if not existing or candidate["generated_at"] > existing.get("generated_at", 0):
                    events[event_id] = candidate

    legacy_root = os.path.join(REPORTS_PATH, "event")
    if os.path.isdir(legacy_root):
        for event_slug in os.listdir(legacy_root):
            event_root = os.path.join(legacy_root, event_slug)
            if not os.path.isdir(event_root):
                continue
            latest_event: Optional[Dict[str, Any]] = None
            node_count = 0
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
                node_count += 1
            if latest_event:
                event_id = latest_event.get("event_id", event_slug)
                existing = events.get(event_id)
                candidate = {
                    "event_id": event_id,
                    "generated_at": latest_event.get("generated_at", 0),
                    "node_count": node_count,
                }
                if not existing or candidate["generated_at"] > existing.get("generated_at", 0):
                    events[event_id] = candidate

    event_list = list(events.values())
    event_list.sort(key=lambda item: item.get("generated_at", 0), reverse=True)
    return event_list


def _list_report_events() -> List[Dict[str, Any]]:
    events_by_id: Dict[str, Dict[str, Any]] = {}
    for event in _list_event_registry("all"):
        events_by_id[event["event_id"]] = {
            "event_id": event["event_id"],
            "event_name": event["event_name"],
            "name": event["event_name"],
            "status": _event_registry_status(event),
            "created_at": event["created_at"],
            "started_at": event["started_at"],
            "ended_at": event["ended_at"],
            "has_reports": False,
            "report_nodes": 0,
            "latest_report_at": None,
        }

    for event in _list_events("all"):
        if event["event_id"] in events_by_id:
            continue
        events_by_id[event["event_id"]] = {
            **event,
            "event_name": event.get("name"),
            "has_reports": False,
            "report_nodes": 0,
            "latest_report_at": None,
        }

    for entry in _list_report_event_ids():
        event_id = entry.get("event_id")
        if not event_id:
            continue
        record = events_by_id.setdefault(
            event_id,
            {
                "event_id": event_id,
                "name": None,
                "status": "unknown",
                "created_at": None,
                "ended_at": None,
                "has_reports": False,
                "report_nodes": 0,
                "latest_report_at": None,
            },
        )
        record["has_reports"] = True
        record["report_nodes"] = entry.get("node_count", record.get("report_nodes", 0))
        record["latest_report_at"] = entry.get("generated_at", record.get("latest_report_at"))

    def _sort_key(item: Dict[str, Any]) -> int:
        return int(item.get("latest_report_at") or item.get("created_at") or 0)

    events = list(events_by_id.values())
    temp_ids = _alias_temp_event_ids()
    if temp_ids:
        events = [event for event in events if event.get("event_id") not in temp_ids]
    events.sort(key=_sort_key, reverse=True)
    return events


def _resolve_report_paths(event_id: str, node_key: str) -> Optional[Dict[str, Any]]:
    node_key = _safe_report_slug(node_key)
    if not node_key:
        return None
    paths = _report_paths(event_id, node_key)
    if os.path.exists(paths["json"]):
        return {
            "json": paths["json"],
            "html": paths["html"],
            "meta": paths["meta"],
            "storage": "v2",
        }

    for meta in _list_event_reports(event_id):
        if meta.get("node_key") != node_key:
            continue
        report_dir = meta.get("report_dir")
        if not report_dir:
            continue
        return {
            "json": os.path.join(report_dir, "data.json"),
            "html": os.path.join(report_dir, "report.html"),
            "meta": os.path.join(report_dir, "meta.json"),
            "storage": meta.get("storage", "legacy"),
        }
    return None


def _aggregate_node_payload(event_id: str, meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    node_key = meta.get("node_key")
    if not node_key:
        return None
    report_dir = meta.get("report_dir")
    if meta.get("storage") == "legacy" and report_dir:
        json_path = os.path.join(report_dir, "data.json")
        html_path = os.path.join(report_dir, "report.html")
    else:
        paths = _report_paths(event_id, node_key)
        json_path = paths["json"]
        html_path = paths["html"]

    report_json = _read_json(json_path) if os.path.exists(json_path) else None
    html_exists = os.path.exists(html_path)
    safe_event = quote(event_id, safe="")
    safe_node = quote(node_key, safe="")
    return {
        "node_id": meta.get("node_id") or node_key,
        "system_id": meta.get("system_id"),
        "generated_at": meta.get("generated_at"),
        "event_id_original": meta.get("event_id_original"),
        "report_json": report_json,
        "report_json_url": f"/api/reports/{safe_event}/{safe_node}/json",
        "report_html_url": f"/api/reports/{safe_event}/{safe_node}/html" if html_exists else None,
    }


def _render_aggregate_html(event_id: str, event: Optional[Dict[str, Any]], nodes: List[Dict[str, Any]]) -> str:
    title = event.get("name") if event else None
    title = title or event_id
    title_html = html_lib.escape(title)
    event_id_html = html_lib.escape(event_id)

    header = f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Event report: {title_html}</title>
    <style>
      body {{
        font-family: "Segoe UI", Arial, sans-serif;
        margin: 0;
        padding: 24px;
        background: #f8fafc;
        color: #0f172a;
      }}
      h1 {{
        margin: 0 0 6px;
        font-size: 26px;
      }}
      .meta {{
        color: #64748b;
        margin-bottom: 20px;
      }}
      .node-card {{
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 16px;
        margin-bottom: 16px;
        box-shadow: 0 4px 12px rgba(15, 23, 42, 0.06);
      }}
      .node-card h2 {{
        font-size: 18px;
        margin: 0 0 8px;
      }}
      .node-card a {{
        color: #2563eb;
        font-weight: 600;
        text-decoration: none;
      }}
      iframe {{
        width: 100%;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        margin-top: 12px;
        height: 500px;
      }}
    </style>
  </head>
  <body>
    <h1>{title_html}</h1>
    <div class="meta">Event ID: {event_id_html}</div>
"""

    body_parts = [header]
    if not nodes:
        body_parts.append("<p>No node reports uploaded yet.</p>")
    else:
        for node in nodes:
            label = node.get("node_id") or node.get("system_id") or "Unknown node"
            label_html = html_lib.escape(str(label))
            generated = node.get("generated_at")
            ts_label = f"{generated}" if generated else "unknown time"
            link = node.get("report_html_url") or node.get("report_json_url") or "#"
            link_html = html_lib.escape(link)
            body_parts.append(
                f"""
    <div class="node-card">
      <h2>{label_html}</h2>
      <div class="meta">Generated at: {ts_label}</div>
      <a href="{link_html}" target="_blank" rel="noreferrer">Open node report</a>
"""
            )
            if node.get("report_html_url"):
                iframe_src = html_lib.escape(node["report_html_url"])
                body_parts.append(f'      <iframe src="{iframe_src}" loading="lazy"></iframe>\n')
            body_parts.append("    </div>")

    body_parts.append("</body></html>")
    return "\n".join(body_parts)


def _write_aggregate_report(event_id: str) -> Optional[Dict[str, Any]]:
    reports = _list_event_reports(event_id)
    if not reports:
        return None
    event = _get_event(event_id)
    nodes: List[Dict[str, Any]] = []
    for meta in reports:
        payload = _aggregate_node_payload(event_id, meta)
        if payload:
            nodes.append(payload)

    payload = {
        "event_id": event_id,
        "name": event.get("name") if event else None,
        "status": event.get("status") if event else None,
        "created_at": event.get("created_at") if event else None,
        "ended_at": event.get("ended_at") if event else None,
        "generated_at": int(time.time() * 1e9),
        "nodes": nodes,
    }
    paths = _aggregate_paths(event_id)
    _write_json(paths["json"], payload)
    _write_text(paths["html"], _render_aggregate_html(event_id, event, nodes))
    return payload


def _merge_temp_reports(event_id: str, node_id: str, temp_event_id: str) -> Dict[str, Any]:
    merged = 0
    updated_nodes: List[str] = []
    temp_reports = _list_event_reports(temp_event_id)
    if not temp_reports:
        return {"merged": 0, "nodes": []}

    safe_node = _safe_report_slug(node_id) if node_id else ""
    for meta in temp_reports:
        meta_node_id = meta.get("node_id")
        node_key = meta.get("node_key") or ""
        if node_id:
            node_match = False
            if meta_node_id and meta_node_id == node_id:
                node_match = True
            if safe_node and node_key == safe_node:
                node_match = True
            if not node_match:
                continue

        report_dir = meta.get("report_dir")
        if meta.get("storage") == "legacy" and report_dir:
            json_path = os.path.join(report_dir, "data.json")
            html_path = os.path.join(report_dir, "report.html")
        else:
            paths = _report_paths(temp_event_id, node_key)
            json_path = paths["json"]
            html_path = paths["html"]

        report = _read_json(json_path)
        if not report:
            continue
        report_html = None
        if os.path.exists(html_path):
            try:
                with open(html_path, "r", encoding="utf-8") as handle:
                    report_html = handle.read()
            except Exception:
                report_html = None

        report = dict(report)
        report["event_id_original"] = report.get("event_id", temp_event_id)
        report["event_id"] = event_id

        meta_payload = {
            "node_id": meta.get("node_id"),
            "system_id": meta.get("system_id"),
            "deployment_id": meta.get("deployment_id"),
            "generated_at": meta.get("generated_at"),
            "event_id_original": temp_event_id,
        }
        stored = _store_report_bundle(event_id, node_key, report, report_html, meta_payload)
        if not stored:
            continue
        merged += 1
        updated_nodes.append(node_key)

    if merged:
        _write_aggregate_report(event_id)
    return {"merged": merged, "nodes": updated_nodes}


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
            continue
        value = _value_to_float(result[0].get("value"))
        if value is None:
            continue
        return int(value), True
    if vm_ok:
        return 0, True
    return None, False


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


def _base_node_grouped(node_id: str) -> Dict[str, Any]:
    """Base structure for a node with grouped systems."""
    return {
        "node_id": node_id,
        "deployment_id": None,
        "latitude": None,
        "longitude": None,
        "event_id": None,
        "event_updated_at": None,
        "gps_updated_at": None,
        "gps_age_sec": None,
        "gps_source": "none",
        "location": None,
        "node_url": None,
        "manual": None,
        "alerts": [],
        "alerts_count": 0,
        "systems": [],
        # Aggregated metrics from primary GX (for backward compat)
        "soc": None,
        "pout": None,
        # Aggregated ACUVIM metrics (for backward compat)
        "acuvim_vavg": None,
        "acuvim_iavg": None,
        "acuvim_p": None,
        "acuvim_updated_at": None,
    }


def _base_gx_system(portal_id: str) -> Dict[str, Any]:
    """Base structure for a GX system."""
    return {
        "system_id": portal_id,
        "type": "gx",
        "gx_host": None,
        "soc": None,
        "pout": None,
        "latitude": None,
        "longitude": None,
        "gps_updated_at": None,
        "alerts": [],
        "alerts_count": 0,
    }


def _base_acuvim_system(device: str) -> Dict[str, Any]:
    """Base structure for an ACUVIM system."""
    return {
        "system_id": device,
        "type": "acuvim",
        "location": None,
        "ip": None,
        "vavg": None,
        "iavg": None,
        "p": None,
        "updated_at": None,
    }


def _get_gx_system_id(metric: Dict[str, Any]) -> Optional[str]:
    """Extract unique system ID for a GX device from system_id label."""
    system_id = metric.get("system_id")
    if system_id and system_id not in ("-", ""):
        return system_id
    # Fallback to portal_id for legacy data
    portal_id = metric.get("portal_id")
    if portal_id and portal_id not in ("-", ""):
        return portal_id
    return None


def _get_acuvim_system_id(metric: Dict[str, Any]) -> Optional[str]:
    """Extract unique system ID for an ACUVIM device from system_id label."""
    system_id = metric.get("system_id")
    if system_id and system_id not in ("-", ""):
        return system_id
    # Fallback to device for legacy data
    device = metric.get("device")
    if device and device not in ("-", ""):
        return device
    return None


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
        f"last_over_time({_metric_name('victron_gps_position_latitude_value')}{selector}[{lookback}])"
    )
    lon_series = _vm_query_vector(
        f"last_over_time({_metric_name('victron_gps_position_longitude_value')}{selector}[{lookback}])"
    )
    soc_series = _vm_query_vector(
        f"last_over_time({_metric_name('victron_battery_soc_value')}{selector}[{lookback}])"
    )
    pout_series = _vm_query_vector(
        f"last_over_time({_metric_name('victron_vebus_ac_out_p_value')}{selector}[{lookback}])"
    )

    alarm_selector = _build_selector(
        ["__name__=~\"victron_.*_alarms_.*_value\""],
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

    alias_map = _load_event_alias_map()
    if alias_map:
        for node in nodes.values():
            event_id = node.get("event_id")
            node_id = _clean_label_value(node.get("node_id"), PLACEHOLDER_NODE_IDS)
            if not event_id or not node_id:
                continue
            alias_key = (event_id, node_id)
            resolved = alias_map.get(alias_key)
            if resolved and resolved != event_id:
                node["event_id_original"] = event_id
                node["event_id"] = resolved

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


def _nodes_by_node_id(nodes: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        node_id = _clean_label_value(node.get("node_id"), PLACEHOLDER_NODE_IDS)
        if not node_id:
            continue
        current = result.get(node_id)
        if not current:
            result[node_id] = node
            continue
        if current.get("is_logger") and not node.get("is_logger"):
            result[node_id] = node
    return result


def _load_nodes_grouped(
    deployment_ids: Optional[List[str]] = None,
    event_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Load nodes grouped by node_id with systems nested within."""
    selector = _build_selector(deployment_ids=deployment_ids)
    lookback = f"{LOOKBACK_SECONDS}s"

    # Query all node_ids
    node_ids = _vm_label_values("node_id", selector if selector else None)

    nodes: Dict[str, Dict[str, Any]] = {}
    for node_id in node_ids:
        normalized = _clean_label_value(node_id, PLACEHOLDER_NODE_IDS)
        if not normalized or normalized in nodes:
            continue
        nodes[normalized] = _base_node_grouped(normalized)

    # Query GX metrics
    gx_lat_series = _vm_query_vector(
        f"last_over_time({_metric_name('victron_gps_position_latitude_value')}{selector}[{lookback}])"
    )
    gx_lon_series = _vm_query_vector(
        f"last_over_time({_metric_name('victron_gps_position_longitude_value')}{selector}[{lookback}])"
    )
    gx_soc_series = _vm_query_vector(
        f"last_over_time({_metric_name('victron_battery_soc_value')}{selector}[{lookback}])"
    )
    gx_pout_series = _vm_query_vector(
        f"last_over_time({_metric_name('victron_vebus_ac_out_p_value')}{selector}[{lookback}])"
    )

    alarm_selector = _build_selector(
        ["__name__=~\"victron_.*_alarms_.*_value\""],
        deployment_ids=deployment_ids,
    )
    gx_alarms_series = _vm_query_vector(
        f"last_over_time({alarm_selector}[{lookback}])"
    )

    # Query ACUVIM metrics
    acuvim_vavg_series = _vm_query_vector(
        f"last_over_time({_metric_name('acuvim_Vln')}{selector}[{lookback}])"
    )
    acuvim_iavg_series = _vm_query_vector(
        f"last_over_time({_metric_name('acuvim_I')}{selector}[{lookback}])"
    )
    acuvim_p_series = _vm_query_vector(
        f"last_over_time({_metric_name('acuvim_P')}{selector}[{lookback}])"
    )

    # Query events
    event_labels = _build_event_labels(event_ids)
    event_selector = _build_selector(event_labels, deployment_ids=deployment_ids)
    event_series: List[Dict[str, Any]] = []
    for metric in _event_metric_names():
        event_series.extend(
            _vm_query_vector(
                f"last_over_time({metric}{event_selector}[{lookback}])"
            )
        )

    # Build systems lookup: node_id -> {system_id -> system}
    gx_systems: Dict[str, Dict[str, Dict[str, Any]]] = {}
    acuvim_systems: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def _apply_gx_data(series_list: List[Dict[str, Any]], field: str) -> None:
        for series in series_list:
            metric = series.get("metric", {})
            node_id = _clean_label_value(metric.get("node_id"), PLACEHOLDER_NODE_IDS)
            if not node_id or node_id not in nodes:
                continue
            system_id = _get_gx_system_id(metric)
            if not system_id:
                continue

            # Get or create system entry
            if node_id not in gx_systems:
                gx_systems[node_id] = {}
            if system_id not in gx_systems[node_id]:
                gx_systems[node_id][system_id] = _base_gx_system(system_id)

            system = gx_systems[node_id][system_id]
            system["gx_host"] = metric.get("gx_host")

            value = _value_to_float(series.get("value"))
            if value is not None:
                system[field] = value
                if field in ("latitude", "longitude"):
                    ts = _value_ts(series.get("value"))
                    if ts:
                        system["gps_updated_at"] = max(system.get("gps_updated_at") or 0, ts)

            # Also set deployment_id on node
            deployment_id = _clean_label_value(metric.get("deployment_id"), PLACEHOLDER_DEPLOYMENT_IDS)
            if deployment_id:
                nodes[node_id]["deployment_id"] = deployment_id

    def _apply_gx_alarms(series_list: List[Dict[str, Any]]) -> None:
        for series in series_list:
            metric = series.get("metric", {})
            node_id = _clean_label_value(metric.get("node_id"), PLACEHOLDER_NODE_IDS)
            if not node_id or node_id not in nodes:
                continue
            system_id = _get_gx_system_id(metric)
            if not system_id:
                continue

            value = _value_to_float(series.get("value"))
            if value is None or value < 0.5:
                continue

            if node_id not in gx_systems:
                gx_systems[node_id] = {}
            if system_id not in gx_systems[node_id]:
                gx_systems[node_id][system_id] = _base_gx_system(system_id)

            system = gx_systems[node_id][system_id]
            name = metric.get("name") or metric.get("__name__", "alarm")
            if name.endswith(":avg"):
                name = name[:-4]
            system["alerts"].append(name)

    def _apply_acuvim_data(series_list: List[Dict[str, Any]], field: str) -> None:
        for series in series_list:
            metric = series.get("metric", {})
            node_id = _clean_label_value(metric.get("node_id"), PLACEHOLDER_NODE_IDS)
            if not node_id or node_id not in nodes:
                continue
            system_id = _get_acuvim_system_id(metric)
            if not system_id:
                continue

            if node_id not in acuvim_systems:
                acuvim_systems[node_id] = {}
            if system_id not in acuvim_systems[node_id]:
                acuvim_systems[node_id][system_id] = _base_acuvim_system(system_id)

            system = acuvim_systems[node_id][system_id]
            system["ip"] = metric.get("ip")
            location = metric.get("location")
            if location and location != "-":
                system["location"] = location

            value = _value_to_float(series.get("value"))
            if value is not None:
                system[field] = value
                ts = _value_ts(series.get("value"))
                if ts:
                    system["updated_at"] = max(system.get("updated_at") or 0, ts)

            deployment_id = _clean_label_value(metric.get("deployment_id"), PLACEHOLDER_DEPLOYMENT_IDS)
            if deployment_id:
                nodes[node_id]["deployment_id"] = deployment_id

    # Apply GX data
    _apply_gx_data(gx_lat_series, "latitude")
    _apply_gx_data(gx_lon_series, "longitude")
    _apply_gx_data(gx_soc_series, "soc")
    _apply_gx_data(gx_pout_series, "pout")
    _apply_gx_alarms(gx_alarms_series)

    # Apply ACUVIM data
    _apply_acuvim_data(acuvim_vavg_series, "vavg")
    _apply_acuvim_data(acuvim_iavg_series, "iavg")
    _apply_acuvim_data(acuvim_p_series, "p")

    # Apply events to nodes
    for series in event_series:
        metric = series.get("metric", {})
        node_id = _clean_label_value(metric.get("node_id"), PLACEHOLDER_NODE_IDS)
        if not node_id or node_id not in nodes:
            continue
        event_id = _normalize_label_value(metric.get("event_id"))
        value = _value_to_float(series.get("value"))
        if value is None or value < 0.5:
            continue
        ts = _value_ts(series.get("value")) or 0
        node = nodes[node_id]
        prev_ts = node.get("event_updated_at") or 0
        if ts >= prev_ts:
            node["event_id"] = event_id
            node["event_updated_at"] = ts
            location = _normalize_label_value(metric.get("location"))
            if location and location != "-":
                node["location"] = location

    # Finalize nodes: attach systems, aggregate data, build URLs
    now = time.time()
    manual_locations = _load_manual_locations()

    for node_id, node in nodes.items():
        # Attach GX systems
        if node_id in gx_systems:
            for system in gx_systems[node_id].values():
                system["alerts"] = sorted(set(system["alerts"]))
                system["alerts_count"] = len(system["alerts"])
                node["systems"].append(system)
                # Aggregate to node level (first GX with data wins)
                if node["latitude"] is None and system.get("latitude") is not None:
                    node["latitude"] = system["latitude"]
                    node["longitude"] = system["longitude"]
                    node["gps_updated_at"] = system.get("gps_updated_at")
                if node["soc"] is None and system.get("soc") is not None:
                    node["soc"] = system["soc"]
                if node["pout"] is None and system.get("pout") is not None:
                    node["pout"] = system["pout"]
                # Aggregate alerts
                node["alerts"].extend(system["alerts"])

        # Attach ACUVIM systems
        if node_id in acuvim_systems:
            for system in acuvim_systems[node_id].values():
                node["systems"].append(system)
                # Aggregate to node level
                if node["acuvim_vavg"] is None and system.get("vavg") is not None:
                    node["acuvim_vavg"] = system["vavg"]
                    node["acuvim_iavg"] = system.get("iavg")
                    node["acuvim_p"] = system.get("p")
                    node["acuvim_updated_at"] = system.get("updated_at")

        # Finalize alerts
        node["alerts"] = sorted(set(node["alerts"]))
        node["alerts_count"] = len(node["alerts"])

        # GPS age
        gps_ts = node.get("gps_updated_at")
        if gps_ts:
            node["gps_age_sec"] = max(0, int(now - gps_ts))
            node["gps_source"] = "gps"

        # Manual location fallback
        manual = manual_locations.get(node_id)
        node["manual"] = manual
        gps_valid = node.get("latitude") is not None and node.get("longitude") is not None
        gps_stale = node.get("gps_age_sec") is not None and node["gps_age_sec"] > GPS_STALE_SECONDS
        if (not gps_valid or gps_stale) and manual:
            node["latitude"] = manual["latitude"]
            node["longitude"] = manual["longitude"]
            node["gps_source"] = "manual"
            node["gps_age_sec"] = max(0, int(now - manual["updated_at"]))

        # Build node URL using node_id directly
        node["node_url"] = _build_node_url_simple(node_id, node.get("deployment_id"))

    # Filter by event if specified
    if event_ids:
        nodes = {
            nid: n for nid, n in nodes.items()
            if n.get("event_id") in event_ids
        }

    # Filter out nodes with no data
    result = []
    for node in nodes.values():
        if (
            node.get("systems")
            or node.get("latitude") is not None
            or node.get("soc") is not None
            or node.get("event_id")
        ):
            result.append(node)

    return sorted(result, key=lambda n: n.get("node_id") or "")


def _build_node_url_simple(node_id: str, deployment_id: Optional[str]) -> Optional[str]:
    """Build node URL using node_id directly."""
    node_id = _clean_label_value(node_id, PLACEHOLDER_NODE_IDS)
    if not node_id:
        return None

    if NODE_URL_TEMPLATE:
        context = {
            "node_id": node_id,
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

    if not node_domain:
        return None

    return f"https://{node_id}.{node_domain}"


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
    thresholds = _tile_thresholds()
    deployment_id = _preference_deployment_id(deployment_ids if deployment_ids else None)
    with _get_db() as conn:
        preferred_provider = get_preferred_provider(conn, deployment_id)
        fleet_counts = get_tile_usage_totals(
            conn,
            month_key,
            deployment_ids if deployment_ids else None,
        )

    if not preferred_provider or preferred_provider not in MAP_TILE_PROVIDERS:
        preferred_provider = MAP_DEFAULT_PROVIDER if MAP_DEFAULT_PROVIDER in MAP_TILE_PROVIDERS else "esri"

    policy = build_tile_policy(
        preferred_provider,
        fleet_counts,
        {"mapbox": MAP_TILE_LIMIT_MAPBOX, "esri": MAP_TILE_LIMIT_ESRI},
        MAP_TILE_SWITCH_THRESHOLD,
        MAP_TILE_DISABLE_THRESHOLD,
        _month_label(month_key),
    )

    resp = jsonify({
        "month_key": month_key,
        "thresholds": thresholds,
        "local": None,
        "fleet": fleet_counts,
        "pct": policy["pct"],
        "blocked": policy["blocked"],
        "preferredProvider": preferred_provider,
        "recommendedProvider": policy["recommended_provider"],
        "satelliteAllowed": policy["satellite_allowed"],
        "warning": policy["warning"],
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

    with _get_db() as conn:
        fleet_counts = get_tile_usage_totals(conn, utc_month_key(), deployment_ids)

    preferred_provider = provider
    policy = build_tile_policy(
        preferred_provider,
        fleet_counts,
        {"mapbox": MAP_TILE_LIMIT_MAPBOX, "esri": MAP_TILE_LIMIT_ESRI},
        MAP_TILE_SWITCH_THRESHOLD,
        MAP_TILE_DISABLE_THRESHOLD,
    )

    if policy["blocked"].get(provider):
        return (
            jsonify(
                {
                    "error": "provider blocked by guardrail",
                    "preferredProvider": preferred_provider,
                    "recommendedProvider": policy["recommended_provider"],
                    "warning": policy["warning"],
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
        "warning": policy["warning"],
    }
    return jsonify(response)


@app.route("/api/tiles/usage", methods=["POST"])
def api_tiles_usage() -> Any:
    payload = request.get_json(silent=True) or {}
    provider = str(payload.get("provider", "")).strip().lower()
    if not is_valid_provider(provider):
        return jsonify({"error": "provider must be mapbox or esri"}), 400

    node_id = str(payload.get("node_id", "")).strip()
    if not node_id:
        return jsonify({"error": "node_id required"}), 400

    delta_value = payload.get("delta", payload.get("count", payload.get("increment_count", 0)))
    try:
        delta = int(delta_value)
    except Exception:
        return jsonify({"error": "delta must be an integer"}), 400
    if delta <= 0:
        return jsonify({"error": "delta must be positive"}), 400

    month_key = _parse_month_key(str(payload.get("month", payload.get("month_key", ""))).strip())
    if not month_key:
        month_key = utc_month_key()

    deployment_id = str(payload.get("deployment_id", "")).strip() or DEPLOYMENT_ID or "global"

    with _get_db() as conn:
        record_tile_usage(conn, month_key, provider, node_id, deployment_id, delta)
        conn.commit()

    return jsonify(
        {
            "ok": True,
            "provider": provider,
            "delta": delta,
            "month_key": month_key,
            "node_id": node_id,
            "deployment_id": deployment_id,
        }
    )


@app.route("/api/tiles/state", methods=["GET"])
def api_tiles_state() -> Any:
    month_key = _parse_month_key(request.args.get("month", "").strip())
    if not month_key:
        month_key = utc_month_key()
    deployment_ids = _parse_deployment_filter()
    thresholds = _tile_thresholds()
    deployment_id = _preference_deployment_id(deployment_ids if deployment_ids else None)

    with _get_db() as conn:
        preferred_provider = get_preferred_provider(conn, deployment_id)
        fleet_counts = get_tile_usage_totals(
            conn,
            month_key,
            deployment_ids if deployment_ids else None,
        )

    if not preferred_provider or preferred_provider not in MAP_TILE_PROVIDERS:
        preferred_provider = MAP_DEFAULT_PROVIDER if MAP_DEFAULT_PROVIDER in MAP_TILE_PROVIDERS else "esri"

    policy = build_tile_policy(
        preferred_provider,
        fleet_counts,
        {"mapbox": MAP_TILE_LIMIT_MAPBOX, "esri": MAP_TILE_LIMIT_ESRI},
        MAP_TILE_SWITCH_THRESHOLD,
        MAP_TILE_DISABLE_THRESHOLD,
        _month_label(month_key),
    )

    resp = jsonify(
        {
            "month_key": month_key,
            "thresholds": thresholds,
            "fleet": fleet_counts,
            "pct": policy["pct"],
            "blocked": policy["blocked"],
            "preferredProvider": preferred_provider,
            "recommendedProvider": policy["recommended_provider"],
            "satelliteAllowed": policy["satellite_allowed"],
            "warning": policy["warning"],
        }
    )
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/tiles/policy", methods=["GET"])
def api_tiles_policy() -> Any:
    month_key = _parse_month_key(request.args.get("month", "").strip())
    if not month_key:
        month_key = utc_month_key()
    deployment_ids = _parse_deployment_filter()
    deployment_id = _preference_deployment_id(deployment_ids if deployment_ids else None)

    with _get_db() as conn:
        preferred_provider = get_preferred_provider(conn, deployment_id)
        fleet_counts = get_tile_usage_totals(
            conn,
            month_key,
            deployment_ids if deployment_ids else None,
        )

    if not preferred_provider or preferred_provider not in MAP_TILE_PROVIDERS:
        preferred_provider = MAP_DEFAULT_PROVIDER if MAP_DEFAULT_PROVIDER in MAP_TILE_PROVIDERS else "esri"

    policy = build_tile_policy(
        preferred_provider,
        fleet_counts,
        {"mapbox": MAP_TILE_LIMIT_MAPBOX, "esri": MAP_TILE_LIMIT_ESRI},
        MAP_TILE_SWITCH_THRESHOLD,
        MAP_TILE_DISABLE_THRESHOLD,
        _month_label(month_key),
    )

    resp = jsonify(
        {
            "month_key": month_key,
            "preferredProvider": preferred_provider,
            "recommendedProvider": policy["recommended_provider"],
            "satelliteAllowed": policy["satellite_allowed"],
            "blocked": policy["blocked"],
            "pct": policy["pct"],
        }
    )
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


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
    # Use grouped format (node_id as primary, systems nested)
    nodes = _load_nodes_grouped(
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


@app.route("/api/nodes/legacy", methods=["GET"])
def api_nodes_legacy() -> Any:
    """Legacy endpoint using old system_id-based format."""
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


@app.route("/api/events", methods=["GET", "POST"])
def api_events() -> Any:
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        raw_name = payload.get("event_name") or payload.get("name")
        name = _clean_event_name(raw_name)
        if not name:
            return jsonify({"error": "event_name required"}), 400
        started_at_raw = payload.get("started_at")
        started_at = None
        if started_at_raw is not None:
            started_at = _parse_event_timestamp(started_at_raw)
            if started_at is None:
                return jsonify({"error": "started_at invalid"}), 400
        event, existing = _create_event_registry_entry(name, started_at)
        if existing:
            year = datetime.now().year
            suggested = f"{name} {year}"
            return jsonify(
                {
                    "error": "event_name_exists",
                    "event_id": existing["event_id"],
                    "already_existed": True,
                    "existing": {
                        "event_id": existing["event_id"],
                        "event_name": existing["event_name"],
                        "created_at": existing["created_at"],
                        "started_at": existing.get("started_at"),
                    },
                    "suggested": {"event_name": suggested},
                }
            ), 409
        if not event:
            return jsonify({"error": "event_name required"}), 400
        response = {
            "event_id": event["event_id"],
            "event_name": event["event_name"],
            "name": event["event_name"],
            "created_at": event["created_at"],
            "started_at": event["started_at"],
            "ended_at": event["ended_at"],
            "status": "active",
        }
        return jsonify(response), 201

    deployment_ids = _parse_deployment_filter()
    events = _load_active_events(deployment_ids if deployment_ids else None)
    registry = _event_registry_lookup([event["event_id"] for event in events if event.get("event_id")])
    for event in events:
        event_id = event.get("event_id")
        info = registry.get(event_id) if event_id else None
        if info:
            event["event_name"] = info["event_name"]
            event["name"] = info["event_name"]
            event["created_at"] = info["created_at"]
            event["started_at"] = info["started_at"]
            event["ended_at"] = info["ended_at"]
        else:
            event["event_name"] = event_id
            event["name"] = event_id
        event["status"] = "active"
    resp = jsonify({"events": events})
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/events/create", methods=["POST"])
def api_events_create() -> Any:
    return api_events()


@app.route("/api/events/registry", methods=["GET"])
def api_events_registry() -> Any:
    status = str(request.args.get("status", "all")).strip().lower()
    event_id = str(request.args.get("event_id", "")).strip()
    if status not in {"active", "ended", "all"}:
        status = "all"
    events = _list_event_registry(status=status, event_id=event_id or None)
    payload = []
    for event in events:
        payload.append(
            {
                "event_id": event["event_id"],
                "event_name": event["event_name"],
                "created_at": event["created_at"],
                "started_at": event["started_at"],
                "ended_at": event["ended_at"],
                "status": _event_registry_status(event),
            }
        )
    resp = jsonify({"events": payload})
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/events/<event_id>", methods=["GET"])
def api_event_detail(event_id: str) -> Any:
    resolved = _resolve_event_registry(event_id)
    resolved_id = resolved["event_id"] if resolved else event_id
    event = _get_event(resolved_id)
    if not event:
        return jsonify({"error": "event not found"}), 404
    if resolved:
        event["event_name"] = resolved["event_name"]
        event["name"] = resolved["event_name"]
        event["started_at"] = resolved["started_at"]
        event["ended_at"] = resolved["ended_at"]
    resp = jsonify(event)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/events/<event_id>/end", methods=["POST"])
def api_event_end(event_id: str) -> Any:
    resolved = _resolve_event_registry(event_id)
    resolved_id = resolved["event_id"] if resolved else event_id
    event = _get_event_registry(resolved_id)
    if not event:
        legacy_event = _end_event(resolved_id)
        if legacy_event:
            return jsonify(legacy_event)
        return jsonify({"error": "event not found"}), 404
    ended_at = _event_timestamp()
    nodes = _load_nodes(event_ids=[resolved_id])
    ts_ns = int(ended_at * 1_000_000_000)
    lines: List[str] = []
    seen: set[tuple[str, Optional[str], Optional[str]]] = set()
    for node in nodes:
        system_id = node.get("host_system_id") or node.get("system_id")
        node_id = _clean_label_value(node.get("node_id"), PLACEHOLDER_NODE_IDS)
        deployment_id = _clean_label_value(node.get("deployment_id"), PLACEHOLDER_DEPLOYMENT_IDS)
        if not system_id:
            continue
        key = (system_id, node_id, deployment_id)
        if key in seen:
            continue
        seen.add(key)
        line = _build_event_line(
            resolved_id,
            system_id,
            node_id,
            deployment_id,
            node.get("location"),
            0,
            ts_ns,
        )
        if line:
            lines.append(line)
    if lines:
        ok, error = _vm_write_lines(lines)
        if not ok:
            return jsonify({"error": "vm_write_failed", "detail": error}), 502
    _end_event_registry(resolved_id, ended_at)
    _mark_legacy_event_ended(resolved_id, ended_at)
    event["ended_at"] = ended_at
    payload = {
        "event_id": event["event_id"],
        "event_name": event["event_name"],
        "name": event["event_name"],
        "created_at": event["created_at"],
        "started_at": event["started_at"],
        "ended_at": event["ended_at"],
        "status": "ended",
    }
    return jsonify(payload)


@app.route("/api/events/<event_id>/add_nodes", methods=["POST"])
def api_event_add_nodes_bulk(event_id: str) -> Any:
    payload = request.get_json(silent=True) or {}
    node_ids = payload.get("node_ids") or []
    if isinstance(node_ids, str):
        node_ids = [node_ids]
    if not isinstance(node_ids, list):
        return jsonify({"error": "node_ids must be a list"}), 400
    node_ids = [str(value).strip() for value in node_ids if str(value).strip()]
    if not node_ids:
        return jsonify({"error": "node_ids required"}), 400

    resolved = _resolve_event_registry(event_id)
    resolved_id = resolved["event_id"] if resolved else event_id
    event = _get_event_registry(resolved_id)
    if not event:
        return jsonify({"error": "event not found"}), 404

    nodes = _load_nodes()
    nodes_by_id = _nodes_by_node_id(nodes)
    started_at = event.get("started_at") or _event_timestamp()
    ts_ns = int(started_at * 1_000_000_000)
    location = _clean_event_name(payload.get("location"))

    lines: List[str] = []
    added: List[Dict[str, Any]] = []
    missing: List[str] = []
    for node_id in node_ids:
        node_entry = nodes_by_id.get(node_id)
        if not node_entry:
            missing.append(node_id)
            continue
        system_id = node_entry.get("host_system_id") or node_entry.get("system_id")
        deployment_id = node_entry.get("deployment_id")
        line = _build_event_line(
            resolved_id,
            system_id,
            node_id,
            deployment_id,
            location or node_entry.get("location"),
            1,
            ts_ns,
        )
        if not line:
            missing.append(node_id)
            continue
        lines.append(line)
        added.append(
            {
                "node_id": node_id,
                "system_id": system_id,
                "deployment_id": _clean_label_value(
                    deployment_id, PLACEHOLDER_DEPLOYMENT_IDS
                ),
            }
        )

    if not lines:
        return jsonify({"error": "no_nodes_found", "missing": missing}), 404

    ok, error = _vm_write_lines(lines)
    if not ok:
        return jsonify({"error": "vm_write_failed", "detail": error}), 502

    for entry in added:
        _add_event_node(resolved_id, entry["node_id"])

    return jsonify(
        {
            "event_id": resolved_id,
            "added": added,
            "missing": missing,
            "started_at": started_at,
        }
    )


@app.route("/api/events/<event_id>/nodes", methods=["POST"])
def api_event_add_node(event_id: str) -> Any:
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id", "")).strip()
    if not node_id:
        return jsonify({"error": "node_id required"}), 400
    resolved_id = _resolve_event_registry_id(event_id) or event_id
    event = _get_event(resolved_id)
    if not event:
        return jsonify({"error": "event not found"}), 404
    created = _add_event_node(resolved_id, node_id)
    return jsonify({"event_id": resolved_id, "node_id": node_id, "created": created})


@app.route("/api/events/<event_id>/nodes/<node_id>/end", methods=["POST"])
def api_event_end_node(event_id: str, node_id: str) -> Any:
    resolved_id = _resolve_event_registry_id(event_id) or event_id
    event = _get_event(resolved_id)
    if not event:
        return jsonify({"error": "event not found"}), 404
    updated = _end_event_node(resolved_id, node_id)
    if not updated:
        return jsonify({"error": "node not found"}), 404
    return jsonify({"event_id": resolved_id, "node_id": node_id, "ended": True})


@app.route("/api/events/<event_id>/aliases", methods=["POST"])
def api_event_aliases(event_id: str) -> Any:
    payload = request.get_json(silent=True) or {}
    node_id = str(payload.get("node_id", "")).strip()
    temp_event_id = str(payload.get("temp_event_id", "")).strip()
    if not node_id or not temp_event_id:
        return jsonify({"error": "node_id and temp_event_id required"}), 400
    resolved_id = _resolve_event_registry_id(event_id) or event_id
    event = _get_event(resolved_id)
    if not event:
        return jsonify({"error": "event not found"}), 404
    _add_event_alias(resolved_id, node_id, temp_event_id)
    merged = _merge_temp_reports(resolved_id, node_id, temp_event_id)
    return jsonify(
        {
            "event_id": resolved_id,
            "node_id": node_id,
            "temp_event_id": temp_event_id,
            "merged_reports": merged.get("merged", 0),
        }
    )


@app.route("/api/reports", methods=["GET"])
def api_reports_summary() -> Any:
    months = _list_report_month_keys()
    events = _list_report_events()
    resp = jsonify({"months": months, "events": events})
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/reports/monthly", methods=["GET"])
def api_reports_monthly() -> Any:
    month_key = _parse_month_key(request.args.get("month", ""))
    if not month_key:
        return jsonify({"error": "month required (YYYY-MM)"}), 400
    reports = _list_monthly_reports(month_key)
    base_url = _report_base_url()
    for report in reports:
        node_key = report.get("node_key")
        if not node_key:
            continue
        report["report_url"] = f"{base_url}/api/reports/monthly/{month_key}/{node_key}"
        report["download_url"] = f"{base_url}/api/reports/monthly/{month_key}/{node_key}?download=1"
    payload: Dict[str, Any] = {"month": month_key, "reports": reports}
    if not reports:
        payload["warning"] = (
            "No monthly reports found. Check FLEET_METRIC_SUFFIX and stream aggregation."
        )
        payload["metric_suffix"] = METRIC_SUFFIX
        payload["metric_names"] = [_metric_name(name) for name in _MONTHLY_METRICS.values()]
    resp = jsonify(payload)
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


@app.route("/api/reports/events", methods=["GET"])
def api_reports_events() -> Any:
    events = _list_report_events()
    resp = jsonify({"events": events})
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/reports/<event_id>", methods=["GET"])
def api_reports_event_nodes(event_id: str) -> Any:
    event_id = event_id.strip()
    if not event_id:
        return jsonify({"error": "event_id required"}), 400
    reports = _list_event_reports(event_id)
    base_url = _report_base_url()
    safe_event = quote(event_id, safe="")
    items = []
    for report in reports:
        node_key = report.get("node_key")
        if not node_key:
            continue
        items.append(
            {
                "node_key": node_key,
                "node_id": report.get("node_id"),
                "system_id": report.get("system_id"),
                "generated_at": report.get("generated_at"),
                "report_json_url": f"{base_url}/api/reports/{safe_event}/{node_key}/json",
                "report_html_url": f"{base_url}/api/reports/{safe_event}/{node_key}/html",
                "event_id_original": report.get("event_id_original"),
            }
        )

    aggregate_paths = _aggregate_paths(event_id)
    if reports and not (os.path.exists(aggregate_paths["json"]) or os.path.exists(aggregate_paths["html"])):
        _write_aggregate_report(event_id)
    aggregate = {
        "available": os.path.exists(aggregate_paths["json"]) or os.path.exists(aggregate_paths["html"]),
        "report_json_url": f"{base_url}/api/reports/{safe_event}/aggregate/json",
        "report_html_url": f"{base_url}/api/reports/{safe_event}/aggregate/html",
    }
    resp = jsonify({"event_id": event_id, "reports": items, "aggregate": aggregate})
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
    resolved = _resolve_report_paths(event_id, node_key)
    if not resolved or not os.path.exists(resolved["json"]):
        return jsonify({"error": "report not found"}), 404
    download = request.args.get("download") == "1"
    filename = f"event_{safe_event}_{safe_node}.json"
    return send_from_directory(
        os.path.dirname(resolved["json"]),
        os.path.basename(resolved["json"]),
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
    resolved = _resolve_report_paths(event_id, node_key)
    if not resolved:
        return "Report not found", 404
    if os.path.exists(resolved["html"]):
        return send_from_directory(
            os.path.dirname(resolved["html"]),
            os.path.basename(resolved["html"]),
            mimetype="text/html",
        )
    if not os.path.exists(resolved["json"]):
        return "Report not found", 404
    try:
        with open(resolved["json"], "r") as handle:
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


@app.route("/api/reports/<event_id>/<node_key>/json", methods=["GET"])
def api_reports_node_json(event_id: str, node_key: str) -> Any:
    resolved = _resolve_report_paths(event_id, node_key)
    if not resolved or not os.path.exists(resolved["json"]):
        return jsonify({"error": "report not found"}), 404
    safe_event = _safe_report_slug(event_id)
    safe_node = _safe_report_slug(node_key)
    download = request.args.get("download") == "1"
    filename = f"event_{safe_event}_{safe_node}.json"
    return send_from_directory(
        os.path.dirname(resolved["json"]),
        os.path.basename(resolved["json"]),
        as_attachment=download,
        download_name=filename,
        mimetype="application/json",
    )


@app.route("/api/reports/<event_id>/<node_key>/html", methods=["GET"])
def api_reports_node_html(event_id: str, node_key: str) -> Any:
    resolved = _resolve_report_paths(event_id, node_key)
    if not resolved:
        return "Report not found", 404
    if os.path.exists(resolved["html"]):
        return send_from_directory(
            os.path.dirname(resolved["html"]),
            os.path.basename(resolved["html"]),
            mimetype="text/html",
        )
    return "Report not found", 404


@app.route("/api/reports/<event_id>/aggregate/json", methods=["GET"])
def api_reports_aggregate_json(event_id: str) -> Any:
    event_id = event_id.strip()
    if not event_id:
        return jsonify({"error": "event_id required"}), 400
    paths = _aggregate_paths(event_id)
    if not os.path.exists(paths["json"]):
        if not _write_aggregate_report(event_id):
            return jsonify({"error": "aggregate not found"}), 404
    safe_event = _safe_report_slug(event_id)
    filename = f"event_{safe_event}_aggregate.json"
    return send_from_directory(
        os.path.dirname(paths["json"]),
        os.path.basename(paths["json"]),
        as_attachment=request.args.get("download") == "1",
        download_name=filename,
        mimetype="application/json",
    )


@app.route("/api/reports/<event_id>/aggregate/html", methods=["GET"])
def api_reports_aggregate_html(event_id: str) -> Any:
    event_id = event_id.strip()
    if not event_id:
        return "Report not found", 404
    paths = _aggregate_paths(event_id)
    if not os.path.exists(paths["html"]):
        if not _write_aggregate_report(event_id):
            return "Report not found", 404
    return send_from_directory(
        os.path.dirname(paths["html"]),
        os.path.basename(paths["html"]),
        mimetype="text/html",
    )


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
    if not isinstance(report, dict):
        return jsonify({"error": "report must be an object"}), 400
    report_html = payload.get("report_html")
    event_id = (payload.get("event_id") or report.get("event_id") or "").strip()
    temp_event_id = (payload.get("temp_event_id") or report.get("temp_event_id") or "").strip()
    event_id_original = (
        payload.get("event_id_original")
        or report.get("event_id_original")
        or temp_event_id
    )
    if not event_id:
        return jsonify({"error": "event_id required"}), 400
    node_id = payload.get("node_id") or report.get("node_id")
    system_id = payload.get("system_id") or report.get("system_id")
    deployment_id = payload.get("deployment_id") or report.get("deployment_id")
    generated_at = payload.get("generated_at") or report.get("generated_at") or int(time.time() * 1e9)

    node_key = _report_node_key(node_id, system_id)
    if not _safe_report_slug(event_id) or not node_key:
        return jsonify({"error": "invalid report identifiers"}), 400
    report_payload = dict(report)
    report_payload["report_type"] = "event"
    report_payload["event_id"] = event_id
    if event_id_original:
        report_payload["event_id_original"] = event_id_original
    if node_id:
        report_payload["node_id"] = node_id
    if system_id:
        report_payload["system_id"] = system_id
    if deployment_id:
        report_payload["deployment_id"] = deployment_id
    report_payload["generated_at"] = generated_at

    stored = _store_report_bundle(
        event_id,
        node_key,
        report_payload,
        report_html,
        {
            "node_id": node_id,
            "system_id": system_id,
            "deployment_id": deployment_id,
            "generated_at": generated_at,
            "event_id_original": event_id_original or None,
            "version": payload.get("version", "v2"),
        },
    )
    if not stored:
        return jsonify({"error": "unable to store report"}), 500

    _write_aggregate_report(event_id)

    base_url = _report_base_url()
    safe_event_url = quote(event_id, safe="")
    report_url = f"{base_url}/api/reports/{safe_event_url}/{node_key}/json"
    report_html_url = f"{base_url}/api/reports/{safe_event_url}/{node_key}/html"
    return jsonify(
        {
            "success": True,
            "report_url": report_url,
            "report_html_url": report_html_url,
        }
    )


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
