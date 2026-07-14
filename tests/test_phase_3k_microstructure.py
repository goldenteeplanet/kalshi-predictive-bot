from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import (
    MarketSnapshot,
    MicrostructureEvent,
    MicrostructureFeature,
    MicrostructureSignal,
    OrderbookDepthSnapshot,
    SignalEvent,
)
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.forecasting.microstructure_v1 import MicrostructureV1Forecaster
from kalshi_predictor.microstructure.dislocation import detect_dislocation_events
from kalshi_predictor.microstructure.imbalance import calculate_imbalance
from kalshi_predictor.microstructure.late_moves import detect_late_move_events
from kalshi_predictor.microstructure.liquidity_tracker import detect_liquidity_events
from kalshi_predictor.microstructure.orderbook_features import (
    build_microstructure_features,
    parse_orderbook_depth,
)
from kalshi_predictor.microstructure.reports import generate_microstructure_report
from kalshi_predictor.microstructure.sampling import sample_microstructure_watchlist
from kalshi_predictor.microstructure.smart_money import detect_smart_money_events
from kalshi_predictor.microstructure.spread_tracker import detect_spread_events
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.scheduler import scheduler_plan
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_orderbook_depth_parser_handles_missing_data() -> None:
    depth = parse_orderbook_depth(None)

    assert depth["yes_levels"] == []
    assert depth["no_levels"] == []
    assert depth["yes_bid_depth"] is None
    assert depth["no_bid_depth"] is None
    assert depth["imbalance"] is None


def test_imbalance_calculation() -> None:
    imbalance = calculate_imbalance(Decimal("30"), Decimal("10"))

    assert imbalance == Decimal("0.5")
    assert calculate_imbalance(None, None) is None


def test_spread_tightening_and_widening_detection() -> None:
    tightened = detect_spread_events(
        {
            "ticker": "MICRO",
            "current_spread": Decimal("0.04"),
            "avg_spread": Decimal("0.08"),
            "max_spread": Decimal("0.12"),
            "spread_change": Decimal("-0.05"),
        },
        settings=Settings(),
    )
    widened = detect_spread_events(
        {
            "ticker": "MICRO",
            "current_spread": Decimal("0.16"),
            "avg_spread": Decimal("0.08"),
            "max_spread": Decimal("0.16"),
            "spread_change": Decimal("0.06"),
        },
        settings=Settings(),
    )

    assert any(event["event_type"] == "SPREAD_TIGHTENING" for event in tightened)
    assert any(event["event_type"] == "SPREAD_WIDENING" for event in widened)


def test_liquidity_improvement_and_drying_detection() -> None:
    improving = detect_liquidity_events(
        {
            "ticker": "MICRO",
            "current_liquidity": Decimal("200"),
            "avg_liquidity": Decimal("150"),
            "liquidity_change_pct": Decimal("0.40"),
        },
        settings=Settings(),
    )
    drying = detect_liquidity_events(
        {
            "ticker": "MICRO",
            "current_liquidity": Decimal("70"),
            "avg_liquidity": Decimal("120"),
            "liquidity_change_pct": Decimal("-0.35"),
        },
        settings=Settings(),
    )

    assert any(event["event_type"] == "LIQUIDITY_IMPROVING" for event in improving)
    assert any(event["event_type"] == "LIQUIDITY_DRYING_UP" for event in drying)


def test_price_dislocation_and_cross_model_disagreement_detection(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    feature = _micro_feature(ticker="MICRO-DISLOC")
    with session_factory() as session:
        _seed_market_snapshot(session, ticker="MICRO-DISLOC")
        _seed_forecast(session, ticker="MICRO-DISLOC", model_name="ensemble_v2", probability="0.70")
        _seed_forecast(
            session,
            ticker="MICRO-DISLOC",
            model_name="market_implied_v1",
            probability="0.50",
        )
        events = detect_dislocation_events(session, feature, settings=Settings())

    event_types = {event["event_type"] for event in events}
    assert "PRICE_DISLOCATION_YES" in event_types
    assert "MODEL_MARKET_DIVERGENCE" in event_types
    assert "CROSS_MODEL_DISAGREEMENT" in event_types


def test_late_move_detection() -> None:
    events = detect_late_move_events(
        {
            **_micro_feature(),
            "price_velocity": Decimal("0.10"),
            "price_acceleration": Decimal("0.06"),
            "liquidity_change_pct": Decimal("0.55"),
        },
        minutes_to_close=Decimal("30"),
        settings=Settings(),
    )

    event_types = {event["event_type"] for event in events}
    assert "LATE_YES_MOVE" in event_types
    assert "LATE_VOLATILITY_SPIKE" in event_types
    assert "LATE_LIQUIDITY_SURGE" in event_types


def test_smart_money_heuristic_cautious_output() -> None:
    events = detect_smart_money_events(
        {
            **_micro_feature(),
            "price_velocity": Decimal("0.10"),
            "spread_change": Decimal("-0.04"),
            "liquidity_change_pct": Decimal("0.50"),
            "orderbook_imbalance": Decimal("0.80"),
            "late_move_score": Decimal("0.80"),
            "dislocation_score": Decimal("0.60"),
            "current_liquidity": Decimal("200"),
        },
        settings=Settings(),
    )

    assert any(event["event_type"] == "POSSIBLE_INFORMED_FLOW" for event in events)
    assert any("not proof" in event["description"] for event in events)


def test_microstructure_v1_skips_without_enough_snapshots_and_forecasts_with_features(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    forecaster = MicrostructureV1Forecaster(settings=Settings())
    with session_factory() as session:
        empty_snapshot = _seed_market_snapshot(session, ticker="MICRO-EMPTY")
        assert forecaster.forecast(session, empty_snapshot) is None

        snapshot = _seed_microstructure_snapshots(session)
        _seed_forecast(
            session,
            ticker=snapshot.ticker,
            model_name="ensemble_v2",
            probability="0.70",
        )
        summary = build_microstructure_features(session, settings=Settings())
        forecast = forecaster.forecast(session, snapshot)

    assert summary.features_inserted == 1
    assert forecast is not None
    assert forecast.model_name == "microstructure_v1"
    assert forecast.yes_probability != forecast.market_mid_probability


def test_feature_build_signal_generation_report_and_ui(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    report_path = Path(tmp_path) / "microstructure_report.md"
    with session_factory() as session:
        snapshot = _seed_microstructure_snapshots(session)
        _seed_forecast(
            session,
            ticker=snapshot.ticker,
            model_name="ensemble_v2",
            probability="0.70",
        )
        summary = build_microstructure_features(session, settings=Settings())
        path = generate_microstructure_report(session, output_path=report_path)
        session.commit()
        features = session.scalar(select(func.count(MicrostructureFeature.id)))
        events = session.scalar(select(func.count(MicrostructureEvent.id)))
        signals = session.scalar(select(func.count(MicrostructureSignal.id)))
        signal_events = session.scalar(select(func.count(SignalEvent.id)))
        depth_rows = session.scalar(select(func.count(OrderbookDepthSnapshot.id)))

    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))
    dashboard = client.get("/microstructure")
    detail = client.get(f"/microstructure/{snapshot.ticker}")

    assert summary.features_inserted == 1
    assert summary.events_inserted >= 1
    assert summary.signals_inserted >= 1
    assert features == 1
    assert events and events >= 1
    assert signals and signals >= 1
    assert signal_events and signal_events >= 1
    assert depth_rows == 1
    assert path.exists()
    assert "Microstructure Report" in path.read_text(encoding="utf-8")
    assert dashboard.status_code == 200
    assert "Market Microstructure" in dashboard.text
    assert detail.status_code == 200
    assert snapshot.ticker in detail.text


def test_microstructure_sampler_creates_repeated_snapshots_and_features(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    client = _FakeMicrostructureClient()
    with session_factory() as session:
        insert_market_ranking(
            session,
            {
                "ticker": "MICRO-SAMPLE",
                "ranked_at": utc_now(),
                "title": "Will MICRO-SAMPLE resolve yes?",
                "status": "open",
                "series_ticker": "KXMICRO",
                "event_ticker": "KXMICRO-EVENT",
                "volume": "100",
                "open_interest": "100",
                "liquidity": "100",
                "spread": "0.05",
                "midpoint": "0.50",
                "time_to_close_minutes": "120",
                "forecast_model": "ensemble_v2",
                "forecast_probability": "0.60",
                "best_side": "BUY_YES",
                "best_price": "0.45",
                "estimated_edge": "0.15",
                "liquidity_score": "80",
                "spread_score": "80",
                "time_score": "80",
                "model_confidence_score": "70",
                "opportunity_score": "80",
                "reason": "Seeded sampler test ranking.",
            },
        )
        _seed_forecast(
            session,
            ticker="MICRO-SAMPLE",
            model_name="ensemble_v2",
            probability="0.60",
        )

        result = sample_microstructure_watchlist(
            session,
            limit=1,
            cycles=3,
            interval_seconds=0,
            lookback_minutes=60,
            client=client,
            settings=Settings(),
        )
        session.commit()
        snapshot_count = session.scalar(
            select(func.count(MarketSnapshot.id)).where(
                MarketSnapshot.ticker == "MICRO-SAMPLE"
            )
        )
        feature_count = session.scalar(
            select(func.count(MicrostructureFeature.id)).where(
                MicrostructureFeature.ticker == "MICRO-SAMPLE"
            )
        )

    assert result.snapshots_inserted == 3
    assert result.feature_summary.features_inserted == 1
    assert snapshot_count == 3
    assert feature_count == 1


def test_scheduler_profile_and_cli_smoke() -> None:
    plan = scheduler_plan("microstructure-watch")
    runner = CliRunner()

    assert any("build-microstructure-features" in step.command for step in plan)
    for command in (
        "build-microstructure-features",
        "microstructure-sample-watchlist",
        "microstructure-report",
        "microstructure-opportunities",
        "microstructure-backtest",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3k.db'}")
    return get_session_factory(engine)


class _FakeMicrostructureClient:
    def __init__(self) -> None:
        self.calls = 0

    def get_market(self, ticker: str) -> dict:
        self.calls += 1
        bid = Decimal("0.40") + (Decimal(self.calls) / Decimal("100"))
        return {
            "ticker": ticker,
            "status": "open",
            "title": f"Will {ticker} resolve yes?",
            "series_ticker": "KXMICRO",
            "event_ticker": "KXMICRO-EVENT",
            "close_time": (utc_now() + timedelta(minutes=30)).isoformat(),
            "yes_bid_dollars": str(bid),
            "yes_ask_dollars": str(bid + Decimal("0.05")),
            "no_bid_dollars": str(Decimal("0.95") - bid),
            "no_ask_dollars": str(Decimal("1.00") - bid),
            "liquidity_dollars": "100",
            "volume_fp": "100",
            "open_interest_fp": "100",
        }

    def get_orderbook(self, ticker: str) -> dict:
        bid = Decimal("0.40") + (Decimal(self.calls) / Decimal("100"))
        return {
            "orderbook_fp": {
                "yes_dollars": [[str(bid), "20"]],
                "no_dollars": [["0.50", "10"]],
            }
        }


def _seed_microstructure_snapshots(session):
    now = utc_now()
    ticker = "MICRO-YES"
    rows = [
        (now - timedelta(minutes=45), "0.30", "0.55", "100", "20", "20"),
        (now - timedelta(minutes=20), "0.42", "0.51", "150", "40", "15"),
        (now - timedelta(minutes=2), "0.48", "0.49", "220", "90", "10"),
    ]
    snapshot = None
    for captured_at, yes_bid, no_bid, liquidity, yes_depth, no_depth in rows:
        snapshot = _seed_market_snapshot(
            session,
            ticker=ticker,
            captured_at=captured_at,
            yes_bid=yes_bid,
            no_bid=no_bid,
            liquidity=liquidity,
            yes_depth=yes_depth,
            no_depth=no_depth,
        )
    session.flush()
    assert snapshot is not None
    return snapshot


def _seed_market_snapshot(
    session,
    *,
    ticker: str,
    captured_at=None,
    yes_bid: str = "0.48",
    no_bid: str = "0.48",
    liquidity: str = "100",
    yes_depth: str = "20",
    no_depth: str = "20",
):
    observed_at = captured_at or utc_now()
    return insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": f"Will {ticker} resolve yes?",
            "series_ticker": "KXMICRO",
            "event_ticker": "KXMICRO-EVENT",
            "close_time": (utc_now() + timedelta(minutes=30)).isoformat(),
            "yes_bid_dollars": yes_bid,
            "yes_ask_dollars": str(Decimal("1") - Decimal(no_bid)),
            "liquidity_dollars": liquidity,
            "volume_fp": liquidity,
            "open_interest_fp": "100",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [[yes_bid, yes_depth]],
                "no_dollars": [[no_bid, no_depth]],
            }
        },
        observed_at,
    )


def _seed_forecast(session, *, ticker: str, model_name: str, probability: str) -> None:
    insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
            forecasted_at=utc_now(),
            model_name=model_name,
            yes_probability=Decimal(probability),
            market_mid_probability=Decimal("0.50"),
            best_yes_bid=Decimal("0.48"),
            best_yes_ask=Decimal("0.52"),
            feature_json={"test": "phase3k"},
            notes="Phase 3K test forecast.",
        ),
    )
    session.flush()


def _micro_feature(*, ticker: str = "MICRO") -> dict:
    return {
        "ticker": ticker,
        "current_yes_bid": Decimal("0.48"),
        "current_yes_ask": Decimal("0.52"),
        "current_no_bid": Decimal("0.48"),
        "current_no_ask": Decimal("0.52"),
        "current_spread": Decimal("0.04"),
        "avg_spread": Decimal("0.08"),
        "max_spread": Decimal("0.12"),
        "spread_change": Decimal("-0.04"),
        "liquidity_change_pct": Decimal("0.50"),
        "orderbook_imbalance": Decimal("0.80"),
        "yes_bid_depth": Decimal("90"),
        "no_bid_depth": Decimal("10"),
        "price_velocity": Decimal("0.10"),
        "price_acceleration": Decimal("0.02"),
        "late_move_score": Decimal("0.60"),
        "dislocation_score": Decimal("0.50"),
        "smart_money_score": Decimal("0.80"),
        "microstructure_confidence": Decimal("90"),
    }
