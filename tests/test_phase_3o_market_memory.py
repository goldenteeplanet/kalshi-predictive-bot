import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    encode_json,
    insert_forecast,
    insert_market_snapshot,
    upsert_settlement,
)
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    ForecastMemory,
    MarketMemory,
    MemoryEventQuarantine,
    PositionSizingDecisionLog,
    TradeMemory,
)
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.memory.archive import archive_memory_to_jsonl
from kalshi_predictor.memory.backfill import backfill_memory_from_existing_tables
from kalshi_predictor.memory.capture import (
    capture_advanced_risk_decision,
    capture_forecast_attempt,
    capture_position_sizing_decision,
)
from kalshi_predictor.memory.datasets import build_forecast_learning_dataset
from kalshi_predictor.memory.repository import forecast_timeline, write_forecast_memory
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.paper.ledger import (
    create_paper_order,
    insert_paper_fill,
    mark_order_filled,
    update_position_for_fill,
)
from kalshi_predictor.paper.models import BUY_YES, PaperDecision
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_market_and_forecast_capture_link_decision_snapshot(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session)
        forecast = _seed_forecast(session, snapshot=snapshot)
        session.commit()

    with session_factory() as session:
        market_event = session.scalar(select(MarketMemory))
        forecast_event = session.scalar(
            select(ForecastMemory).where(ForecastMemory.event_type == "FORECAST_CREATED")
        )

    assert market_event is not None
    assert forecast_event is not None
    assert market_event.snapshot_type == "DECISION"
    assert forecast_event.forecast_id == f"forecast:{forecast.id}"
    assert forecast_event.market_memory_id == market_event.market_memory_id
    quality_flags = json.loads(forecast_event.data_quality_flags_json)
    assert "FORECAST_ARTIFACT_HASH_MISSING" in quality_flags


def test_failed_forecast_and_no_trade_opportunity_are_persisted(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="P3O-NOTRADE")
        capture_forecast_attempt(
            session,
            snapshot=snapshot,
            model_name="weather_v2",
            forecast=None,
            error="missing weather features",
        )
        insert_market_ranking(
            session,
            {
                "ticker": snapshot.ticker,
                "ranked_at": snapshot.captured_at + timedelta(minutes=1),
                "forecast_model": "weather_v2",
                "forecast_probability": "0.52",
                "best_side": None,
                "opportunity_score": "20",
                "reason": "no trade",
            },
        )
        session.commit()

    with session_factory() as session:
        rows = list(session.scalars(select(ForecastMemory)))

    assert {row.event_type for row in rows} >= {"FORECAST_FAILED", "OPPORTUNITY_REJECTED"}
    assert any(row.decision_status == "NO_TRADE" for row in rows)


def test_phase_3m_and_3n_events_capture_risk_blocked_no_trade(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="P3O-RISK")
        forecast = _seed_forecast(session, snapshot=snapshot)
        sizing = _seed_position_sizing_decision(
            session,
            ticker=snapshot.ticker,
            forecast_id=forecast.id,
        )
        risk = _seed_advanced_risk_decision(
            session,
            ticker=snapshot.ticker,
            forecast_id=forecast.id,
            sizing_id=sizing.id,
        )
        capture_position_sizing_decision(session, sizing)
        capture_advanced_risk_decision(session, risk)
        session.commit()

    with session_factory() as session:
        events = [row.event_type for row in forecast_timeline(session, f"forecast:{forecast.id}")]

    assert "PHASE_3M_SIZED" in events
    assert "PHASE_3N_EVALUATED" in events
    assert "NO_TRADE_FINALIZED" in events


def test_paper_trade_and_settlement_capture_trade_and_forecast_outcomes(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="P3O-TRADE")
        forecast = _seed_forecast(session, snapshot=snapshot, probability=Decimal("0.80"))
        order = create_paper_order(
            session,
            _paper_decision(snapshot.ticker, forecast.id),
            settings=Settings(learning_mode=False),
        )
        assert order is not None
        mark_order_filled(session, order, filled_at=snapshot.captured_at + timedelta(minutes=2))
        fill = insert_paper_fill(
            session,
            order=order,
            price=Decimal("0.40"),
            quantity=1,
            fee=Decimal("0"),
            filled_at=snapshot.captured_at + timedelta(minutes=2),
        )
        update_position_for_fill(session, fill)
        upsert_settlement(
            session,
            {
                "ticker": snapshot.ticker,
                "result": "yes",
                "settlement_ts": (snapshot.captured_at + timedelta(hours=1)).isoformat(),
            },
        )
        session.commit()

    with session_factory() as session:
        trade_events = list(
            session.scalars(
                select(TradeMemory).where(TradeMemory.trade_id == f"paper_order:{order.id}")
            )
        )
        forecast_outcome = session.scalar(
            select(ForecastMemory)
            .where(ForecastMemory.forecast_id == f"forecast:{forecast.id}")
            .where(ForecastMemory.event_type == "FORECAST_OUTCOME_FINALIZED")
        )

    assert {event.event_type for event in trade_events} >= {
        "TRADE_INTENT_CREATED",
        "ENTRY_FILLED",
        "POSITION_OPENED",
        "SETTLEMENT_FINAL",
        "TRADE_OUTCOME_FINALIZED",
    }
    fill_event = next(event for event in trade_events if event.event_type == "ENTRY_FILLED")
    assert fill_event.execution_mode == "PAPER"
    assert fill_event.paper_fill_model_version == "v1"
    assert forecast_outcome is not None
    assert forecast_outcome.forecast_outcome_status == "FINAL"


def test_repeated_settlement_capture_skips_existing_forecast_outcome(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="P3O-SETTLE-REPEAT")
        forecast = _seed_forecast(session, snapshot=snapshot, probability=Decimal("0.80"))
        forecast_id = forecast.id
        upsert_settlement(
            session,
            {
                "ticker": snapshot.ticker,
                "result": "yes",
                "settlement_ts": (snapshot.captured_at + timedelta(hours=1)).isoformat(),
            },
        )
        session.commit()

    with session_factory() as session:
        upsert_settlement(
            session,
            {
                "ticker": "P3O-SETTLE-REPEAT",
                "result": "yes",
                "settlement_ts": datetime(2026, 1, 2, 1, tzinfo=UTC).isoformat(),
            },
        )
        session.commit()

    with session_factory() as session:
        outcomes = list(
            session.scalars(
                select(ForecastMemory)
                .where(ForecastMemory.forecast_id == f"forecast:{forecast_id}")
                .where(ForecastMemory.event_type == "FORECAST_OUTCOME_FINALIZED")
            )
        )

    assert len(outcomes) == 1
    assert outcomes[0].forecast_outcome_status == "FINAL"


def test_learning_dataset_requires_cutoff_and_excludes_future_labels(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="P3O-DATASET")
        forecast = _seed_forecast(session, snapshot=snapshot, probability=Decimal("0.70"))
        upsert_settlement(
            session,
            {
                "ticker": snapshot.ticker,
                "result": "yes",
                "settlement_ts": (snapshot.captured_at + timedelta(hours=1)).isoformat(),
            },
        )
        session.commit()

    with session_factory() as session:
        early = build_forecast_learning_dataset(
            session,
            training_as_of=datetime(2026, 1, 1, tzinfo=UTC),
        )
        later = build_forecast_learning_dataset(
            session,
            training_as_of=utc_now() + timedelta(minutes=1),
        )

    assert early.rows == []
    assert any(row["forecast_id"] == f"forecast:{forecast.id}" for row in later.rows)
    assert later.manifest["training_as_of"]


def test_memory_writer_idempotency_and_conflict_quarantine(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    event_time = datetime(2026, 1, 2, tzinfo=UTC)
    values = {
        "forecast_id": "manual-forecast",
        "event_type": "FORECAST_CREATED",
        "event_time": event_time,
        "source_component": "test",
        "idempotency_key": "manual-forecast:created:v1",
        "instrument_id": "MANUAL",
        "strategy_id": "test",
        "timeframe": "test",
        "predicted_probability": "0.55",
    }
    with session_factory() as session:
        first = write_forecast_memory(session, values)
        duplicate = write_forecast_memory(session, values)
        conflict = write_forecast_memory(
            session,
            {**values, "predicted_probability": "0.60"},
        )
        session.commit()

    with session_factory() as session:
        quarantine_count = len(session.scalars(select(MemoryEventQuarantine)).all())

    assert first.status == "written"
    assert duplicate.status == "duplicate"
    assert conflict.status == "conflict"
    assert quarantine_count == 1


def test_archive_manifest_and_backfill_dry_run(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="P3O-ARCHIVE")
        _seed_forecast(session, snapshot=snapshot)
        backfill = backfill_memory_from_existing_tables(session, dry_run=True)
        manifest = archive_memory_to_jsonl(session, output_dir=tmp_path / "archive")
        session.commit()

    manifest_path = Path(manifest.output_uri) / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert backfill.market_snapshots == 1
    assert backfill.forecasts == 1
    assert manifest.status == "VERIFIED"
    assert payload["row_counts"]["market_memory"] >= 1
    assert (manifest_path.parent / "market_memory.jsonl").exists()


def test_memory_cli_smoke(tmp_path) -> None:
    db_url = f"sqlite:///{Path(tmp_path) / 'cli.db'}"
    runner = CliRunner()
    env = {"KALSHI_DB_URL": db_url}

    get_settings.cache_clear()
    try:
        status = runner.invoke(app, ["memory-status"], env=env)
        report = runner.invoke(
            app,
            ["memory-report", "--output", str(Path(tmp_path) / "memory.md")],
            env=env,
        )
        backfill = runner.invoke(app, ["memory-backfill", "--dry-run"], env=env)
        dataset = runner.invoke(
            app,
            [
                "memory-dataset",
                "--training-as-of",
                "2026-06-18T00:00:00Z",
                "--output",
                str(Path(tmp_path) / "dataset.json"),
            ],
            env=env,
        )
    finally:
        get_settings.cache_clear()

    assert status.exit_code == 0
    assert report.exit_code == 0
    assert backfill.exit_code == 0
    assert dataset.exit_code == 0
    assert "Phase 3O Market Memory" in status.stdout


def test_memory_ui_renders_dashboard_and_page(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="P3O-UI")
        _seed_forecast(session, snapshot=snapshot)
        session.commit()
    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))

    dashboard = client.get("/dashboard")
    memory_page = client.get("/memory")

    assert dashboard.status_code == 200
    assert memory_page.status_code == 200
    assert "Market Memory" in dashboard.text
    assert "Phase 3O point-in-time learning ledger status" in memory_page.text


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3o.db'}")
    return get_session_factory(engine)


def _seed_snapshot(
    session,
    *,
    ticker: str = "P3O",
    captured_at: datetime | None = None,
):
    now = captured_at or datetime(2026, 1, 2, tzinfo=UTC)
    return insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": f"Will {ticker} resolve yes?",
            "close_time": (now + timedelta(hours=4)).isoformat(),
            "volume_fp": "3000",
            "open_interest_fp": "2000",
            "liquidity_dollars": "50000",
            "yes_bid_dollars": "0.39",
            "yes_ask_dollars": "0.41",
            "no_bid_dollars": "0.58",
            "no_ask_dollars": "0.60",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.39", "100"], ["0.38", "80"]],
                "no_dollars": [["0.58", "100"], ["0.57", "80"]],
            }
        },
        now,
    )


def _seed_forecast(
    session,
    *,
    snapshot,
    probability: Decimal = Decimal("0.62"),
):
    return insert_forecast(
        session,
        ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at + timedelta(minutes=1),
            model_name="market_implied_v1",
            yes_probability=probability,
            market_mid_probability=Decimal("0.40"),
            best_yes_bid=Decimal("0.39"),
            best_yes_ask=Decimal("0.41"),
            feature_json={"source": "phase3o-test"},
        ),
    )


def _paper_decision(ticker: str, forecast_id: int | None) -> PaperDecision:
    return PaperDecision(
        ticker=ticker,
        forecast_id=forecast_id,
        model_name="market_implied_v1",
        side=BUY_YES,
        probability=Decimal("0.80"),
        market_price=Decimal("0.40"),
        limit_price=Decimal("0.40"),
        edge=Decimal("0.40"),
        quantity=1,
        reason="phase3o paper decision",
        raw_decision_json={"strategy": "paper_edge_v1"},
    )


def _seed_position_sizing_decision(
    session,
    *,
    ticker: str,
    forecast_id: int,
) -> PositionSizingDecisionLog:
    row = PositionSizingDecisionLog(
        decision_timestamp=datetime(2026, 1, 2, 0, 3, tzinfo=UTC),
        created_at=utc_now(),
        version="v1",
        mode="live",
        strategy_id="paper_edge_v1",
        instrument=ticker,
        ticker=ticker,
        model_name="market_implied_v1",
        trade_intent_id=f"forecast:{forecast_id}",
        order_correlation_id=f"forecast:{forecast_id}",
        paper_order_id=None,
        tier="high",
        composite_score="0.90",
        proposed_contracts=5,
        live_candidate_contracts=5,
        executed_contracts=1,
        factor_scores_json=encode_json({}),
        factor_weights_json=encode_json({}),
        adjusted_historical_accuracy="0.70",
        historical_sample_size=50,
        drawdown_utilization="0.10",
        caps_json=encode_json({}),
        limiting_factors_json=encode_json([]),
        reason_codes_json=encode_json(["HIGH_CONFIDENCE"]),
        fallback_used=0,
        raw_json=encode_json({}),
    )
    session.add(row)
    session.flush()
    return row


def _seed_advanced_risk_decision(
    session,
    *,
    ticker: str,
    forecast_id: int,
    sizing_id: int,
) -> AdvancedRiskDecisionLog:
    row = AdvancedRiskDecisionLog(
        decision_timestamp=datetime(2026, 1, 2, 0, 4, tzinfo=UTC),
        created_at=utc_now(),
        version="v1",
        mode="live",
        action="BLOCK",
        strategy_id="paper_edge_v1",
        model_id="market_implied_v1",
        category_id="general",
        instrument_id=ticker,
        correlation_group_id=ticker,
        ticker=ticker,
        trade_intent_id=f"forecast:{forecast_id}",
        order_correlation_id=f"forecast:{forecast_id}",
        position_sizing_decision_id=sizing_id,
        paper_order_id=None,
        reservation_id=None,
        phase_3m_tier="high",
        phase_3m_proposed_contracts=5,
        live_candidate_contracts=0,
        executed_contracts=0,
        risk_per_contract="0.40",
        planned_trade_risk="0",
        raw_caps_json=encode_json({}),
        bucketed_caps_json=encode_json({}),
        limiting_factors_json=encode_json(["SPREAD_TOO_WIDE"]),
        hard_blocks_json=encode_json(["SPREAD_TOO_WIDE"]),
        reason_codes_json=encode_json(["SPREAD_TOO_WIDE"]),
        fallback_used=0,
        raw_json=encode_json({}),
    )
    session.add(row)
    session.flush()
    return row
