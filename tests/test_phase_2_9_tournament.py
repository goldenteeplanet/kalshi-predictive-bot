from decimal import Decimal

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.forecasting.ensemble_v2 import EnsembleV2Forecaster
from kalshi_predictor.tournament.diagnostics import generate_model_diagnostics
from kalshi_predictor.tournament.engine import run_model_tournament
from kalshi_predictor.tournament.ranking import classify_market_category, rank_tournament_rows
from kalshi_predictor.tournament.repository import insert_model_weight
from kalshi_predictor.tournament.weights import generate_model_weights
from kalshi_predictor.utils.time import utc_now


def test_tournament_includes_models_with_insufficient_data(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        result = run_model_tournament(session, days=30, persist=False)

    model_names = {row["model_name"] for row in result.rows}
    assert "market_implied_v1" in model_names
    assert "ensemble_v2" in model_names
    assert all(row["status"] == "INSUFFICIENT_DATA" for row in result.rows)


def test_category_classifier_handles_crypto_weather_general() -> None:
    assert classify_market_category("Will BTC close above 100000?") == "crypto"
    assert classify_market_category("Will rain fall in Kansas City?") == "weather"
    assert classify_market_category("Will this general market resolve yes?") == "general"


def test_ranking_prefers_lower_brier_score() -> None:
    rows = [
        _row("model_a", brier="0.20", log_loss="0.50"),
        _row("model_b", brier="0.10", log_loss="0.30"),
    ]

    rank_tournament_rows(rows)

    ranks = {row["model_name"]: row["calibration_rank"] for row in rows}
    assert ranks["model_b"] == 1
    assert ranks["model_a"] == 2


def test_ranking_penalizes_high_drawdown() -> None:
    rows = [
        _row("steady", brier="0.10", roi="0.20", pnl="10", drawdown="1"),
        _row("swingy", brier="0.10", roi="0.20", pnl="10", drawdown="100"),
    ]

    rank_tournament_rows(rows)

    ranks = {row["model_name"]: row["overall_rank"] for row in rows}
    assert ranks["steady"] == 1
    assert ranks["swingy"] == 2


def test_weights_normalize_to_one_per_category() -> None:
    rows = [
        _row("model_a", brier="0.10", roi="0.20", status="OK"),
        _row("model_b", brier="0.20", roi="0.10", status="OK"),
    ]

    weights = generate_model_weights(rows, lookback_days=30)
    general_weights = [weight for weight in weights if weight["category"] == "general"]
    total = sum((Decimal(weight["weight"]) for weight in general_weights), Decimal("0"))

    assert total == Decimal("1.0")


def test_weights_fall_back_to_market_implied_when_insufficient_data() -> None:
    rows = [_row("model_a", status="INSUFFICIENT_DATA")]

    weights = generate_model_weights(rows, lookback_days=30)
    general = [weight for weight in weights if weight["category"] == "general"]

    assert general[0]["model_name"] == "market_implied_v1"
    assert Decimal(general[0]["weight"]) == Decimal("1.0")


def test_ensemble_v2_uses_stored_weights(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, title="Will BTC exceed 100000?")
        _seed_forecast(session, snapshot, "market_implied_v1", "0.40")
        _seed_forecast(session, snapshot, "crypto_v2", "0.80")
        insert_model_weight(
            session,
            {
                "model_name": "market_implied_v1",
                "category": "crypto",
                "weight": "0.25",
                "method": "test",
                "lookback_days": 30,
            },
        )
        insert_model_weight(
            session,
            {
                "model_name": "crypto_v2",
                "category": "crypto",
                "weight": "0.75",
                "method": "test",
                "lookback_days": 30,
            },
        )

        forecast = EnsembleV2Forecaster().forecast(session, snapshot)

    assert forecast is not None
    assert forecast.yes_probability == Decimal("0.7000")
    assert forecast.feature_json["fallback_reason"] is None


def test_ensemble_v2_falls_back_to_simple_average_without_weights(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, title="Will a general market resolve yes?")
        _seed_forecast(session, snapshot, "market_implied_v1", "0.40")
        _seed_forecast(session, snapshot, "economic_v1", "0.80")

        forecast = EnsembleV2Forecaster().forecast(session, snapshot)

    assert forecast is not None
    assert forecast.yes_probability == Decimal("0.60")
    assert "simple average" in forecast.feature_json["fallback_reason"]


def test_diagnostics_identify_insufficient_data() -> None:
    diagnostics = generate_model_diagnostics([_row("model_a", evaluated=0)])

    assert any("Not enough settled forecasts" in item["notes"] for item in diagnostics)


def test_diagnostics_identify_negative_pnl() -> None:
    diagnostics = generate_model_diagnostics(
        [_row("model_a", pnl="-1", roi="-0.1", status="OK")]
    )

    assert any("Negative P&L" in item["notes"] for item in diagnostics)
    assert any("Negative simulated ROI" in item["notes"] for item in diagnostics)


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'phase29.db'}")
    return get_session_factory(engine)


def _row(
    model_name: str,
    *,
    brier: str | None = "0.20",
    log_loss: str | None = "0.50",
    roi: str | None = "0.10",
    pnl: str | None = "1",
    drawdown: str | None = "0",
    evaluated: int = 10,
    status: str = "OK",
) -> dict:
    return {
        "tournament_run_id": 1,
        "model_name": model_name,
        "category": "general",
        "forecast_count": evaluated,
        "evaluated_forecast_count": evaluated,
        "simulated_trade_count": evaluated,
        "settled_trade_count": evaluated,
        "brier_score": brier,
        "log_loss": log_loss,
        "win_rate": "0.5",
        "total_pnl": pnl,
        "roi_on_exposure": roi,
        "avg_edge": "0.05",
        "max_drawdown": drawdown,
        "calibration_rank": None,
        "pnl_rank": None,
        "overall_rank": None,
        "status": status,
        "notes": "",
        "raw_json": {},
    }


def _seed_snapshot(session, *, title: str):
    now = utc_now()
    return insert_market_snapshot(
        session,
        {
            "ticker": "TOURNAMENT-TEST",
            "status": "open",
            "title": title,
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.50",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "10"]],
                "no_dollars": [["0.50", "10"]],
            }
        },
        now,
    )


def _seed_forecast(session, snapshot, model_name: str, probability: str) -> None:
    insert_forecast(
        session,
        ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at,
            model_name=model_name,
            yes_probability=Decimal(probability),
            market_mid_probability=Decimal("0.45"),
            best_yes_bid=Decimal("0.40"),
            best_yes_ask=Decimal("0.50"),
            feature_json={"test": True},
            notes="test forecast",
        ),
    )
