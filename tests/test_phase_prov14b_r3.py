from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot, upsert_market
from kalshi_predictor.forecasting.registry import latest_snapshots_for_model
from kalshi_predictor.phase_prov14b_r3 import (
    diagnose_weather_snapshot_eligibility,
    write_prov14b_r3_preview,
)
from kalshi_predictor.weather.repository import insert_weather_market_link

NOW = datetime(2026, 7, 19, 20, 0, tzinfo=UTC)


def test_exact_diagnosis_separates_closed_stale_current_and_mismatch(tmp_path) -> None:
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'r3.db'}"))
    with factory() as session:
        _seed(
            session,
            "WX-CLOSED",
            close=NOW - timedelta(minutes=1),
            captured=NOW - timedelta(minutes=5),
        )
        _seed(
            session, "WX-STALE", close=NOW + timedelta(hours=2), captured=NOW - timedelta(hours=7)
        )
        _seed(
            session,
            "WX-CURRENT",
            close=NOW + timedelta(hours=2),
            captured=NOW - timedelta(minutes=2),
        )
        _seed(
            session,
            "WX-MISMATCH",
            close=NOW + timedelta(hours=2),
            captured=NOW - timedelta(minutes=2),
            target=NOW + timedelta(hours=3),
        )
        session.commit()
        report = diagnose_weather_snapshot_eligibility(session, as_of=NOW)
        selector_rows = latest_snapshots_for_model(
            session, model_name="weather_v2", as_of=NOW, limit=10
        )
    classes = {row["ticker"]: row["classification"] for row in report["rows"]}
    assert classes == {
        "WX-CLOSED": "MARKET_CLOSED",
        "WX-CURRENT": "EXACT_CURRENT_WINDOW_READY",
        "WX-MISMATCH": "SELECTOR_ELIGIBLE_TARGET_MISMATCH",
        "WX-STALE": "SELECTOR_ELIGIBLE_SNAPSHOT_STALE",
    }
    assert report["selector_eligible_count"] == 3
    assert report["exact_current_window_ready_count"] == 1
    assert {row.ticker for row in selector_rows} == {"WX-CURRENT", "WX-MISMATCH", "WX-STALE"}
    assert report["guardrails"]["database_writes"] == 0


def test_all_closed_rows_require_catalog_refresh_not_selector_relaxation(tmp_path) -> None:
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'closed.db'}"))
    with factory() as session:
        _seed(
            session,
            "WX-CLOSED",
            close=NOW - timedelta(minutes=1),
            captured=NOW - timedelta(minutes=5),
        )
        session.commit()
        report = diagnose_weather_snapshot_eligibility(session, as_of=NOW)
    assert report["diagnosis"]["code"] == "NO_ROWS_PASSED_CURRENT_MARKET_SELECTOR"
    assert report["repair_preview"]["action"] == (
        "REFRESH_CURRENT_WEATHER_CATALOG; no code change justified"
    )
    assert report["repair_preview"]["change_selector"] is False
    assert report["repair_preview"]["runtime_retry_authorized"] is False


def test_empty_link_catalog_fails_closed(tmp_path) -> None:
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'empty.db'}"))
    with factory() as session:
        report = diagnose_weather_snapshot_eligibility(session, as_of=NOW)
    assert report["candidate_count"] == 0
    assert report["diagnosis"]["code"] == "NO_LINKED_WEATHER_CANDIDATES"
    assert report["repair_preview"]["change_selector"] is False


def test_partial_snapshot_upsert_preserves_close_time_after_r4_repair(tmp_path) -> None:
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'partial.db'}"))
    with factory() as session:
        _seed(
            session,
            "WX-PARTIAL",
            close=NOW + timedelta(hours=2),
            captured=NOW - timedelta(minutes=2),
            include_close_in_snapshot=False,
        )
        session.commit()
        report = diagnose_weather_snapshot_eligibility(session, as_of=NOW)
    assert report["rows"][0]["classification"] == "EXACT_CURRENT_WINDOW_READY"
    assert report["diagnosis"]["reproduced_adapter_defect"] is None
    assert report["repair_preview"]["action"] == (
        "NO_SELECTOR_CHANGE; rerun only against the pinned exact-ready rows"
    )
    assert report["repair_preview"]["change_selector"] is False


def test_preview_is_deterministic_and_database_free(tmp_path) -> None:
    first = json.loads(write_prov14b_r3_preview(tmp_path / "a").read_text(encoding="utf-8"))
    second = json.loads(write_prov14b_r3_preview(tmp_path / "b").read_text(encoding="utf-8"))
    assert first == second
    assert first["database_access"] is False
    assert first["database_writes"] == 0
    assert first["cloud_runtime_modified"] is False
    assert first["guarded_cloud_retry_requires_new_approval"] is True
    assert {row["classification"] for row in first["scenarios"]} == {
        "MARKET_CLOSED",
        "SELECTOR_ELIGIBLE_SNAPSHOT_STALE",
        "EXACT_CURRENT_WINDOW_READY",
        "SELECTOR_ELIGIBLE_TARGET_MISMATCH",
        "MARKET_CLOSE_MISSING",
    }
    assert first["repair_preview"]["metadata_preservation_change_required"] is True


def _seed(
    session,
    ticker: str,
    *,
    close: datetime,
    captured: datetime,
    target: datetime | None = None,
    include_close_in_snapshot: bool = True,
) -> None:
    upsert_market(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "close_time": close.isoformat(),
            "title": "NYC exact weather eligibility fixture",
        },
    )
    insert_weather_market_link(
        session,
        ticker=ticker,
        location_key="new_york",
        weather_metric="temperature_high",
        target_operator="ABOVE",
        target_value="80",
        target_time=target or close,
        confidence="1",
        reason="exact deterministic fixture",
    )
    snapshot_payload = {"ticker": ticker, "status": "open", "yes_bid_dollars": "0.4"}
    if include_close_in_snapshot:
        snapshot_payload["close_time"] = close.isoformat()
    insert_market_snapshot(
        session,
        snapshot_payload,
        {"orderbook_fp": {}},
        captured,
    )
