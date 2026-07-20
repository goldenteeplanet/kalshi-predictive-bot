"""Read-only diagnosis for exact current weather snapshot eligibility."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import INACTIVE_MARKET_STATUSES
from kalshi_predictor.data.schema import Market, MarketSnapshot, WeatherMarketLink

DEFAULT_SNAPSHOT_FRESHNESS = timedelta(hours=6)
DIAGNOSTIC_LIMIT = 100


def diagnose_weather_snapshot_eligibility(
    session: Session,
    *,
    as_of: datetime,
    freshness: timedelta = DEFAULT_SNAPSHOT_FRESHNESS,
    limit: int = DIAGNOSTIC_LIMIT,
) -> dict[str, Any]:
    """Explain every exact predicate without changing selector thresholds or data."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if not 1 <= limit <= DIAGNOSTIC_LIMIT:
        raise ValueError("limit must be between 1 and 100")
    tickers = list(
        session.scalars(
            select(WeatherMarketLink.ticker)
            .distinct()
            .order_by(WeatherMarketLink.ticker)
            .limit(limit)
        )
    )
    rows = [_diagnose_ticker(session, ticker, as_of, freshness) for ticker in tickers]
    reasons = Counter(row["classification"] for row in rows)
    eligible = [row for row in rows if row["selector_eligible"]]
    exact_ready = [row for row in rows if row["exact_current_window_ready"]]
    return {
        "phase": "PROV-14B-R3",
        "generated_at": as_of.astimezone(UTC).isoformat(),
        "mode": "READ_ONLY_NO_WRITE_PREVIEW",
        "candidate_count": len(rows),
        "selector_eligible_count": len(eligible),
        "exact_current_window_ready_count": len(exact_ready),
        "classification_counts": dict(sorted(reasons.items())),
        "rows": rows,
        "diagnosis": _diagnosis(rows, eligible, exact_ready),
        "repair_preview": _repair_preview(rows, eligible, exact_ready),
        "guardrails": {
            "database_writes": 0,
            "cloud_runtime_modified": False,
            "thresholds_changed": False,
            "fuzzy_matching_added": False,
            "execution_enabled": False,
            "guarded_cloud_retry_requires_new_approval": True,
        },
    }


def write_prov14b_r3_preview(output_dir: Path) -> Path:
    """Write deterministic synthetic predicate coverage without database access."""
    output_dir.mkdir(parents=True, exist_ok=True)
    scenarios = [
        _classify_candidate(
            ticker="WX-CLOSED",
            as_of=_fixture_time(),
            market_status="open",
            snapshot_status="open",
            close_time=_fixture_time() - timedelta(minutes=1),
            captured_at=_fixture_time() - timedelta(minutes=5),
            target_time=_fixture_time() - timedelta(minutes=1),
            freshness=DEFAULT_SNAPSHOT_FRESHNESS,
        ),
        _classify_candidate(
            ticker="WX-STALE",
            as_of=_fixture_time(),
            market_status="open",
            snapshot_status="open",
            close_time=_fixture_time() + timedelta(hours=2),
            captured_at=_fixture_time() - timedelta(hours=7),
            target_time=_fixture_time() + timedelta(hours=2),
            freshness=DEFAULT_SNAPSHOT_FRESHNESS,
        ),
        _classify_candidate(
            ticker="WX-CURRENT",
            as_of=_fixture_time(),
            market_status="open",
            snapshot_status="open",
            close_time=_fixture_time() + timedelta(hours=2),
            captured_at=_fixture_time() - timedelta(minutes=2),
            target_time=_fixture_time() + timedelta(hours=2),
            freshness=DEFAULT_SNAPSHOT_FRESHNESS,
        ),
        _classify_candidate(
            ticker="WX-TARGET-MISMATCH",
            as_of=_fixture_time(),
            market_status="open",
            snapshot_status="open",
            close_time=_fixture_time() + timedelta(hours=2),
            captured_at=_fixture_time() - timedelta(minutes=2),
            target_time=_fixture_time() + timedelta(hours=3),
            freshness=DEFAULT_SNAPSHOT_FRESHNESS,
        ),
        _classify_candidate(
            ticker="WX-PARTIAL-METADATA",
            as_of=_fixture_time(),
            market_status="open",
            snapshot_status="open",
            close_time=None,
            captured_at=_fixture_time() - timedelta(minutes=2),
            target_time=_fixture_time() + timedelta(hours=2),
            freshness=DEFAULT_SNAPSHOT_FRESHNESS,
        ),
    ]
    eligible = [row for row in scenarios if row["selector_eligible"]]
    exact_ready = [row for row in scenarios if row["exact_current_window_ready"]]
    payload = {
        "phase": "PROV-14B-R3",
        "mode": "LOCAL_SYNTHETIC_NO_WRITE_PREVIEW",
        "source_selector_predicates": [
            "linked ticker exists",
            "market status is not inactive",
            "snapshot status is not inactive",
            "market close_time is present",
            "market close_time is after as_of",
        ],
        "downstream_exact_predicates": [
            "snapshot freshness is within diagnostic bound",
            "weather link target_time exactly equals market close_time",
        ],
        "scenarios": scenarios,
        "diagnosis": _diagnosis(scenarios, eligible, exact_ready),
        "repair_preview": _repair_preview(scenarios, eligible, exact_ready),
        "database_access": False,
        "database_writes": 0,
        "cloud_runtime_modified": False,
        "guarded_cloud_retry_requires_new_approval": True,
    }
    path = output_dir / "prov14b_r3_weather_snapshot_eligibility_preview.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _diagnose_ticker(
    session: Session,
    ticker: str,
    as_of: datetime,
    freshness: timedelta,
) -> dict[str, Any]:
    market = session.get(Market, ticker)
    snapshot = session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )
    link = session.scalar(
        select(WeatherMarketLink)
        .where(WeatherMarketLink.ticker == ticker)
        .order_by(desc(WeatherMarketLink.detected_at), desc(WeatherMarketLink.id))
        .limit(1)
    )
    return _classify_candidate(
        ticker=ticker,
        as_of=as_of,
        market_status=market.status if market else None,
        snapshot_status=snapshot.status if snapshot else None,
        close_time=market.close_time if market else None,
        captured_at=snapshot.captured_at if snapshot else None,
        target_time=link.target_time if link else None,
        freshness=freshness,
        market_present=market is not None,
        snapshot_present=snapshot is not None,
    )


def _classify_candidate(
    *,
    ticker: str,
    as_of: datetime,
    market_status: str | None,
    snapshot_status: str | None,
    close_time: datetime | None,
    captured_at: datetime | None,
    target_time: datetime | None,
    freshness: timedelta,
    market_present: bool = True,
    snapshot_present: bool = True,
) -> dict[str, Any]:
    market_inactive = _inactive(market_status)
    snapshot_inactive = _inactive(snapshot_status)
    close_time = _aware(close_time)
    captured_at = _aware(captured_at)
    target_time = _aware(target_time)
    selector_eligible = bool(
        market_present
        and snapshot_present
        and not market_inactive
        and not snapshot_inactive
        and close_time is not None
        and close_time > as_of
    )
    fresh = captured_at is not None and timedelta(0) <= as_of - captured_at <= freshness
    target_aligned = (
        close_time is not None and target_time is not None and close_time == target_time
    )
    if not snapshot_present:
        classification = "SNAPSHOT_MISSING"
    elif not market_present:
        classification = "MARKET_MISSING"
    elif market_inactive:
        classification = "MARKET_INACTIVE"
    elif snapshot_inactive:
        classification = "SNAPSHOT_INACTIVE"
    elif close_time is None:
        classification = "MARKET_CLOSE_MISSING"
    elif close_time <= as_of:
        classification = "MARKET_CLOSED"
    elif not fresh:
        classification = "SELECTOR_ELIGIBLE_SNAPSHOT_STALE"
    elif not target_aligned:
        classification = "SELECTOR_ELIGIBLE_TARGET_MISMATCH"
    else:
        classification = "EXACT_CURRENT_WINDOW_READY"
    return {
        "ticker": ticker,
        "classification": classification,
        "selector_eligible": selector_eligible,
        "exact_current_window_ready": selector_eligible and fresh and target_aligned,
        "market_status": market_status,
        "snapshot_status": snapshot_status,
        "close_time": close_time.isoformat() if close_time else None,
        "captured_at": captured_at.isoformat() if captured_at else None,
        "target_time": target_time.isoformat() if target_time else None,
        "snapshot_fresh": fresh,
        "target_time_aligned": target_aligned,
    }


def _diagnosis(
    rows: list[dict[str, Any]],
    eligible: list[dict[str, Any]],
    exact_ready: list[dict[str, Any]],
) -> dict[str, Any]:
    if not rows:
        code = "NO_LINKED_WEATHER_CANDIDATES"
    elif not eligible:
        code = "NO_ROWS_PASSED_CURRENT_MARKET_SELECTOR"
    elif not exact_ready:
        code = "SELECTOR_ROWS_FAIL_DOWNSTREAM_EXACTNESS"
    else:
        code = "EXACT_CURRENT_ROWS_AVAILABLE"
    close_missing = any(row["classification"] == "MARKET_CLOSE_MISSING" for row in rows)
    return {
        "code": code,
        "observed_cloud_failure": "No exact current weather snapshots are eligible",
        "conclusion": (
            "Partial snapshot upserts can erase Market.close_time and deterministically force "
            "zero selector rows. A read-only runtime census is still required to prove that "
            "this reproduced adapter defect caused the cloud failure."
            if close_missing
            else "The guarded cycle failed before writes because the selector returned zero rows. "
            "A runtime census is required to attribute the exact rejecting predicate."
        ),
        "reproduced_adapter_defect": (
            "PARTIAL_SNAPSHOT_UPSERT_ERASES_MARKET_CLOSE_TIME" if close_missing else None
        ),
    }


def _repair_preview(
    rows: list[dict[str, Any]],
    eligible: list[dict[str, Any]],
    exact_ready: list[dict[str, Any]],
) -> dict[str, Any]:
    counts = Counter(row["classification"] for row in rows)
    if exact_ready:
        action = "NO_SELECTOR_CHANGE; rerun only against the pinned exact-ready rows"
    elif eligible:
        action = "REFRESH_EXACT_SNAPSHOTS_OR_REPAIR_TARGET_ALIGNMENT; do not relax predicates"
    elif counts and set(counts) == {"MARKET_CLOSE_MISSING"}:
        action = "PREVIEW_PRESERVE_EXISTING_CLOSE_TIME_ON_PARTIAL_MARKET_UPSERT"
    elif counts and set(counts) <= {"MARKET_CLOSED", "MARKET_INACTIVE", "SNAPSHOT_INACTIVE"}:
        action = "REFRESH_CURRENT_WEATHER_CATALOG; no code change justified"
    else:
        action = "CAPTURE_READ_ONLY_RUNTIME_CENSUS_BEFORE_ANY_SELECTOR_CHANGE"
    return {
        "action": action,
        "change_selector": False,
        "metadata_preservation_change_required": bool(counts.get("MARKET_CLOSE_MISSING")),
        "metadata_preservation_preview": (
            "Keep existing Market.close_time when a partial snapshot payload omits close_time; "
            "still overwrite it when the source explicitly supplies a value."
            if counts.get("MARKET_CLOSE_MISSING")
            else None
        ),
        "fallback_to_closed_or_stale_rows": False,
        "use_fuzzy_matching": False,
        "runtime_retry_authorized": False,
    }


def _inactive(status: str | None) -> bool:
    return str(status or "").lower() in INACTIVE_MARKET_STATUSES


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _fixture_time() -> datetime:
    return datetime(2026, 7, 19, 20, 0, tzinfo=UTC)
