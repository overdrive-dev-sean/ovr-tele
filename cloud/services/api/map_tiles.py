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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS map_tile_usage (
            month_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            node_id TEXT NOT NULL,
            deployment_id TEXT NOT NULL,
            total INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (month_key, provider, node_id, deployment_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_map_tile_usage_month ON map_tile_usage (month_key)"
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


def record_tile_usage(
    conn,
    month_key: str,
    provider: str,
    node_id: str,
    deployment_id: str,
    delta: int,
) -> None:
    now = int(time.time())
    row = conn.execute(
        """
        SELECT total FROM map_tile_usage
        WHERE month_key = ? AND provider = ? AND node_id = ? AND deployment_id = ?
        """,
        (month_key, provider, node_id, deployment_id),
    ).fetchone()
    if row:
        total = int(row["total"] or 0) + delta
        conn.execute(
            """
            UPDATE map_tile_usage
            SET total = ?, updated_at = ?
            WHERE month_key = ? AND provider = ? AND node_id = ? AND deployment_id = ?
            """,
            (total, now, month_key, provider, node_id, deployment_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO map_tile_usage (month_key, provider, node_id, deployment_id, total, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (month_key, provider, node_id, deployment_id, delta, now),
        )


def get_tile_usage_totals(conn, month_key: str, deployment_ids: Optional[list[str]] = None) -> Dict[str, int]:
    base = {provider: 0 for provider in PROVIDERS}
    if deployment_ids:
        placeholders = ",".join(["?"] * len(deployment_ids))
        rows = conn.execute(
            f"""
            SELECT provider, SUM(total) AS total
            FROM map_tile_usage
            WHERE month_key = ? AND deployment_id IN ({placeholders})
            GROUP BY provider
            """,
            (month_key, *deployment_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT provider, SUM(total) AS total
            FROM map_tile_usage
            WHERE month_key = ?
            GROUP BY provider
            """,
            (month_key,),
        ).fetchall()
    for row in rows:
        provider = row["provider"]
        if provider in base:
            base[provider] = int(row["total"] or 0)
    return base


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


def build_tile_policy(
    preferred_provider: str,
    fleet_counts: Dict[str, Optional[int]],
    thresholds: Dict[str, int],
    switch_pct: float,
    disable_pct: float,
    month_label: Optional[str] = None,
) -> Dict[str, object]:
    guardrail = build_guardrail_status(
        preferred_provider,
        fleet_counts,
        thresholds,
        switch_pct,
        month_label,
    )

    satellite_allowed = True
    if all(
        fleet_counts.get(provider) is not None
        and thresholds.get(provider)
        and fleet_counts.get(provider, 0) >= thresholds.get(provider, 0) * disable_pct
        for provider in PROVIDERS
    ):
        satellite_allowed = False
        if not guardrail["warning"]:
            if month_label:
                guardrail["warning"] = (
                    f"Satellite imagery disabled after both providers exceeded limits for {month_label}."
                )
            else:
                guardrail["warning"] = (
                    "Satellite imagery disabled after both providers exceeded limits."
                )

    return {
        "pct": guardrail["pct"],
        "blocked": guardrail["blocked"],
        "recommended_provider": guardrail["recommended_provider"],
        "warning": guardrail["warning"],
        "satellite_allowed": satellite_allowed,
    }
