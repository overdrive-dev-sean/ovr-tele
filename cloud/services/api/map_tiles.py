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
        CREATE TABLE IF NOT EXISTS map_provider_settings (
            deployment_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )


def set_preferred_provider(conn, deployment_id: str, provider: str) -> None:
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO map_provider_settings (deployment_id, provider, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(deployment_id) DO UPDATE SET
            provider = excluded.provider,
            updated_at = excluded.updated_at
        """,
        (deployment_id, provider, now),
    )


def get_preferred_provider(conn, deployment_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT provider FROM map_provider_settings WHERE deployment_id = ?",
        (deployment_id,),
    ).fetchone()
    if not row:
        return None
    value = row["provider"]
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
