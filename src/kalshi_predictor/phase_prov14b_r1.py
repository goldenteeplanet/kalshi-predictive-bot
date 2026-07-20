from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import INACTIVE_MARKET_STATUSES
from kalshi_predictor.data.schema import Market, MarketSnapshot, WeatherMarketLink
from kalshi_predictor.utils.time import parse_datetime, utc_now


def diagnose_weather_snapshot_eligibility(
    session: Session,
    *,
    as_of: datetime | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Explain the exact, fail-closed weather eligibility result per linked ticker."""
    cutoff = as_of or utc_now()
    tickers = list(
        session.scalars(
            select(WeatherMarketLink.ticker)
            .distinct()
            .order_by(WeatherMarketLink.ticker)
            .limit(limit)
        )
    )
    rows: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    for ticker in tickers:
        link = session.scalar(
            select(WeatherMarketLink)
            .where(WeatherMarketLink.ticker == ticker)
            .order_by(desc(WeatherMarketLink.detected_at), desc(WeatherMarketLink.id))
            .limit(1)
        )
        market = session.get(Market, ticker)
        snapshot = session.scalar(
            select(MarketSnapshot)
            .where(MarketSnapshot.ticker == ticker)
            .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
            .limit(1)
        )
        reasons: list[str] = []
        market_status = (market.status or "").lower() if market else None
        snapshot_status = (snapshot.status or "").lower() if snapshot else None
        if market is None:
            reasons.append("MISSING_MARKET")
        if snapshot is None:
            reasons.append("MISSING_SNAPSHOT")
        if market is not None and market_status in INACTIVE_MARKET_STATUSES:
            reasons.append("INACTIVE_MARKET_STATUS")
        if snapshot is not None and snapshot_status in INACTIVE_MARKET_STATUSES:
            reasons.append("INACTIVE_SNAPSHOT_STATUS")
        if market is not None and market.close_time is None:
            reasons.append("MISSING_MARKET_CLOSE_TIME")
        elif market is not None and parse_datetime(market.close_time) <= cutoff:
            reasons.append("MARKET_NOT_FUTURE")
        if link is None or link.target_time is None:
            reasons.append("MISSING_LINK_TARGET_TIME")

        eligible = not reasons
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        rows.append({
            "ticker": ticker,
            "eligible": eligible,
            "exclusion_reasons": reasons,
            "market_status": market_status,
            "snapshot_status": snapshot_status,
            "market_close_time": (
                market.close_time.isoformat() if market and market.close_time else None
            ),
            "snapshot_id": snapshot.id if snapshot else None,
            "snapshot_captured_at": snapshot.captured_at.isoformat() if snapshot else None,
            "location_key": link.location_key if link else None,
            "link_target_time": link.target_time.isoformat() if link and link.target_time else None,
        })

    return {
        "as_of": cutoff.isoformat(),
        "linked_ticker_count": len(tickers),
        "eligible_ticker_count": sum(1 for row in rows if row["eligible"]),
        "excluded_ticker_count": sum(1 for row in rows if not row["eligible"]),
        "reason_counts": dict(sorted(reason_counts.items())),
        "rows": rows,
    }


def build_prov14b_r1_repair_preview() -> dict[str, Any]:
    repairs = [
        {
            "defect": (
                "weather feature creation occurred before current-market eligibility was known"
            ),
            "exact_repair": (
                "pin exact eligible weather snapshots before creating any weather feature"
            ),
        },
        {
            "defect": (
                "the latest stored New York forecast was not scoped to the pinned market target"
            ),
            "exact_repair": "join each pinned link to forecast location_key and exact target_time",
        },
        {
            "defect": (
                "zero eligible weather snapshots was reported only after a feature row was inserted"
            ),
            "exact_repair": (
                "fail closed before the transaction writes when the exact eligible set is empty"
            ),
        },
    ]
    canonical = json.dumps(repairs, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PROV-14B-R1",
        "mode": "LOCAL_NO_WRITE_REPAIR_PREVIEW",
        "database_access": False,
        "database_writes": 0,
        "cloud_runtime_modified": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "fuzzy_matching_used": False,
        "repairs": repairs,
        "diagnostic_exclusion_reasons": [
            "MISSING_MARKET",
            "MISSING_SNAPSHOT",
            "INACTIVE_MARKET_STATUS",
            "INACTIVE_SNAPSHOT_STATUS",
            "MISSING_MARKET_CLOSE_TIME",
            "MARKET_NOT_FUTURE",
            "MISSING_LINK_TARGET_TIME",
        ],
        "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        "guarded_cloud_retry_requires_new_approval": True,
    }


def write_prov14b_r1_repair_preview(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "prov14b_r1_weather_eligibility_repair_preview.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(build_prov14b_r1_repair_preview(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path
