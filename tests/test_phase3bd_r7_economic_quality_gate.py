from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import MarketRanking
from kalshi_predictor.economic.opportunity_quality_gate import (
    MISSING_ACTUAL_CONSENSUS_EVIDENCE,
    MISSING_CONSENSUS_EVIDENCE,
    build_phase3bd_r7_payload,
    write_phase3bd_r7_economic_opportunity_quality_gate_report,
)
from kalshi_predictor.economic.repository import (
    insert_economic_event,
    insert_economic_market_link,
)
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.paper.models import BUY_YES


def test_phase3bd_r7_reports_no_rankings(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        payload = build_phase3bd_r7_payload(session, now=_now())

    assert payload["summary"]["status"] == "NO_ECONOMIC_RANKINGS"
    assert payload["live_or_demo_execution"] is False
    assert payload["order_submission_cancel_replace"] is False


def test_phase3bd_r7_blocks_calendar_only_evidence(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_economic_market(
            session,
            event_forecast=None,
            event_actual=None,
            event_previous=None,
        )
        payload = build_phase3bd_r7_payload(session, now=_now())

    row = payload["rows"][0]
    assert payload["summary"]["status"] == "WAITING_FOR_ACTUAL_CONSENSUS_EVIDENCE"
    assert row["economic_evidence_state"] == "CALENDAR_ONLY"
    assert MISSING_CONSENSUS_EVIDENCE in row["blockers"]
    assert row["preflight_ready"] is False


def test_phase3bd_r7_requires_actual_consensus_by_default(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_economic_market(session, event_forecast="3.2", event_actual=None)
        payload = build_phase3bd_r7_payload(session, now=_now())

    row = payload["rows"][0]
    assert row["economic_evidence_state"] == "CONSENSUS_ONLY"
    assert MISSING_ACTUAL_CONSENSUS_EVIDENCE in row["blockers"]
    assert payload["summary"]["preflight_ready_rows"] == 0


def test_phase3bd_r7_marks_clean_verified_row_preflight_ready(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_economic_market(session, event_forecast="3.2", event_actual="3.5")
        payload = build_phase3bd_r7_payload(session, now=_now())

    row = payload["rows"][0]
    assert payload["summary"]["status"] == "PREFLIGHT_READY"
    assert payload["summary"]["preflight_ready_rows"] == 1
    assert payload["summary"]["risk_missing_rows"] == 1
    assert row["economic_evidence_state"] == "ACTUAL_AND_CONSENSUS"
    assert row["blockers"] == []
    assert row["preflight_ready"] is True
    assert payload["summary"]["risk_preflight_enabled"] is False
    assert payload["summary"]["phase3m_phase3n_preflight_recorded"] == 0


def test_phase3bd_r7_writes_json_markdown_and_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_economic_market(session, event_forecast="3.2", event_actual="3.5")
        artifacts = write_phase3bd_r7_economic_opportunity_quality_gate_report(
            session,
            output_dir=tmp_path / "reports",
            freshness_minutes=1440,
            now=_now(),
        )

    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.rows_path.exists()
    assert artifacts.preflight_rows_path.exists()
    assert "PREFLIGHT_READY" in artifacts.json_path.read_text(encoding="utf-8")


def _seed_ranked_economic_market(
    session,
    *,
    event_forecast: str | None,
    event_actual: str | None,
    event_previous: str | None = "3.1",
) -> None:
    now = _now()
    ticker = "KXCPI-QUALITY"
    insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "event_ticker": "KXCPI-QUALITY-EVENT",
            "series_ticker": "KXCPI",
            "status": "open",
            "title": "Core CPI above consensus?",
            "yes_bid_dollars": "0.48",
            "yes_ask_dollars": "0.50",
            "volume_fp": "5000",
            "open_interest_fp": "1000",
        },
        {"orderbook_fp": {"yes_dollars": [["0.50", "100"]]}},
        captured_at=now - timedelta(minutes=5),
    )
    insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
            forecasted_at=now - timedelta(minutes=4),
            model_name="economic_v1",
            yes_probability=Decimal("0.70"),
            market_mid_probability=Decimal("0.49"),
            best_yes_bid=Decimal("0.48"),
            best_yes_ask=Decimal("0.50"),
            feature_json={"event_key": "cpi"},
        ),
    )
    session.add(
        MarketRanking(
            ticker=ticker,
            ranked_at=now - timedelta(minutes=3),
            title="Core CPI above consensus?",
            status="open",
            series_ticker="KXCPI",
            event_ticker="KXCPI-QUALITY-EVENT",
            volume="5000",
            open_interest="1000",
            liquidity="1000",
            spread="0.01",
            midpoint="0.49",
            time_to_close_minutes="120",
            forecast_model="economic_v1",
            forecast_probability="0.70",
            best_side=BUY_YES,
            best_price="0.50",
            estimated_edge="0.20",
            liquidity_score="80",
            spread_score="90",
            time_score="70",
            model_confidence_score="80",
            opportunity_score="82",
            reason="test economic ranking",
            raw_json="{}",
        )
    )
    insert_economic_event(
        session,
        event_key="cpi",
        source="verified_consensus_fixture",
        event_time=now - timedelta(minutes=10),
        category="cpi",
        title="Core CPI release",
        actual_value=event_actual,
        forecast_value=event_forecast,
        previous_value=event_previous,
        raw_json={"source_url": "https://example.test/verified-cpi"},
    )
    insert_economic_market_link(
        session,
        ticker=ticker,
        event_key="cpi",
        category="cpi",
        confidence=Decimal("0.95"),
        reason="test link",
        raw_json={"source": "test"},
    )
    session.flush()


def _session_factory(tmp_path: Path):
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bd_r7.db'}")
    return get_session_factory(engine)


def _now() -> datetime:
    return datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
