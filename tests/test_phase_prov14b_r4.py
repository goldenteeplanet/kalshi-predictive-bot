from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot, upsert_market
from kalshi_predictor.data.schema import Market
from kalshi_predictor.forecasting.registry import latest_snapshots_for_model
from kalshi_predictor.phase_prov14b_r4 import write_prov14b_r4_preview
from kalshi_predictor.weather.repository import insert_weather_market_link

NOW = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)


def test_partial_snapshot_preserves_existing_exact_close_time(tmp_path) -> None:
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'preserve.db'}"))
    close = NOW + timedelta(hours=2)
    with factory() as session:
        _seed_market_and_link(session, "WX-PRESERVE", close)
        insert_market_snapshot(
            session,
            {"ticker": "WX-PRESERVE", "status": "open", "yes_bid_dollars": "0.4"},
            {"orderbook_fp": {}},
            NOW,
        )
        session.flush()
        assert session.get(Market, "WX-PRESERVE").close_time == close.replace(tzinfo=None)
        rows = latest_snapshots_for_model(
            session, model_name="weather_v2", as_of=NOW, limit=10
        )
    assert rows is not None
    assert [row.ticker for row in rows] == ["WX-PRESERVE"]


def test_explicit_close_time_replacement_is_not_preserved(tmp_path) -> None:
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'replace.db'}"))
    original = NOW + timedelta(hours=2)
    replacement = NOW + timedelta(hours=3)
    with factory() as session:
        upsert_market(
            session,
            {"ticker": "WX-REPLACE", "status": "open", "close_time": original.isoformat()},
        )
        insert_market_snapshot(
            session,
            {
                "ticker": "WX-REPLACE",
                "status": "open",
                "close_time": replacement.isoformat(),
            },
            {},
            NOW,
        )
        session.flush()
        assert session.get(Market, "WX-REPLACE").close_time == replacement.replace(tzinfo=None)


def test_explicit_null_close_time_retains_upsert_semantics(tmp_path) -> None:
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'explicit-null.db'}"))
    with factory() as session:
        upsert_market(
            session,
            {
                "ticker": "WX-EXPLICIT-NULL",
                "status": "open",
                "close_time": (NOW + timedelta(hours=2)).isoformat(),
            },
        )
        insert_market_snapshot(
            session,
            {"ticker": "WX-EXPLICIT-NULL", "status": "open", "close_time": None},
            {},
            NOW,
        )
        session.flush()
        assert session.get(Market, "WX-EXPLICIT-NULL").close_time is None


def test_new_partial_market_remains_fail_closed(tmp_path) -> None:
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'new-partial.db'}"))
    with factory() as session:
        insert_market_snapshot(
            session,
            {"ticker": "WX-NEW-PARTIAL", "status": "open"},
            {},
            NOW,
        )
        session.flush()
        assert session.get(Market, "WX-NEW-PARTIAL").close_time is None


def test_r4_preview_is_deterministic_and_guarded(tmp_path) -> None:
    first = json.loads(write_prov14b_r4_preview(tmp_path / "a").read_text(encoding="utf-8"))
    second = json.loads(write_prov14b_r4_preview(tmp_path / "b").read_text(encoding="utf-8"))
    assert first == second
    assert first["status"] == "PASSED_LOCAL_PREVIEW"
    assert first["repair"]["selector_predicates_changed"] is False
    assert first["guardrails"]["database_access"] is False
    assert first["guardrails"]["guarded_cloud_retry_requires_new_approval"] is True


def _seed_market_and_link(session, ticker: str, close: datetime) -> None:
    upsert_market(
        session,
        {"ticker": ticker, "status": "open", "close_time": close.isoformat()},
    )
    insert_weather_market_link(
        session,
        ticker=ticker,
        location_key="new_york",
        weather_metric="temperature_high",
        target_operator="ABOVE",
        target_value="80",
        target_time=close,
        confidence="1",
        reason="exact R4 fixture",
    )
