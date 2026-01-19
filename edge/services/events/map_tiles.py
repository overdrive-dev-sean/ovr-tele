from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, Optional

PROVIDERS = ("mapbox", "esri")


def utc_month_key(value: Optional[datetime] = None) -> str:
    if value is None:
        current = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        current = value.astimezone(timezone.utc)
    else:
        raise TypeError("utc_month_key expects datetime or None")
    return current.strftime("%Y-%m")


def is_valid_provider(provider: str) -> bool:
    return provider in PROVIDERS


def init_map_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS map_tile_counts (
            month_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            count INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (month_key, provider)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS map_provider_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )


def increment_tile_count(conn, month_key: str, provider: str, count: int) -> None:
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO map_tile_counts (month_key, provider, count, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(month_key, provider) DO UPDATE SET
            count = count + excluded.count,
            updated_at = excluded.updated_at
        """,
        (month_key, provider, count, now),
    )


def get_tile_counts(conn, month_key: str) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT provider, count FROM map_tile_counts WHERE month_key = ?",
        (month_key,),
    ).fetchall()
    counts = {provider: 0 for provider in PROVIDERS}
    for row in rows:
        provider = row["provider"]
        if provider in counts:
            counts[provider] = int(row["count"] or 0)
    return counts


def set_preferred_provider(conn, provider: str) -> None:
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO map_provider_settings (key, value, updated_at)
        VALUES ('preferred_provider', ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (provider, now),
    )


def get_preferred_provider(conn) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM map_provider_settings WHERE key = 'preferred_provider'"
    ).fetchone()
    if not row:
        return None
    value = row["value"]
    return value if value in PROVIDERS else None


def _pct(value: Optional[float], threshold: Optional[float]) -> Optional[float]:
    if value is None or threshold in (None, 0):
        return None
    return (float(value) / float(threshold)) * 100


def build_guardrail_status(
    preferred_provider: str,
    fleet_counts: Dict[str, Optional[int]],
    thresholds: Dict[str, int],
    guardrail_pct: float,
    month_label: Optional[str] = None,
) -> Dict[str, object]:
    pct: Dict[str, Optional[float]] = {}
    blocked: Dict[str, bool] = {}

    for provider in PROVIDERS:
        total = fleet_counts.get(provider)
        threshold = thresholds.get(provider)
        pct[provider] = _pct(total, threshold)
        if total is None or threshold is None:
            blocked[provider] = False
        else:
            blocked[provider] = total >= (float(threshold) * guardrail_pct)

    recommended = preferred_provider if preferred_provider in PROVIDERS else PROVIDERS[0]
    warning = None
    if blocked.get(recommended):
        other = PROVIDERS[0] if recommended == PROVIDERS[1] else PROVIDERS[1]
        if not blocked.get(other):
            recommended = other
        else:
            warning = (
                f"Both providers are at or above {guardrail_pct * 100:.0f}% of the free tier"
            )
            if month_label:
                warning += f" for {month_label}."
            else:
                warning += "."

    return {
        "pct": pct,
        "blocked": blocked,
        "recommended_provider": recommended,
        "warning": warning,
    }
