from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.crypto.repository import insert_crypto_features, insert_crypto_market_link
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    insert_forecast,
    insert_market_snapshot,
    upsert_settlement,
)
from kalshi_predictor.data.schema import MetaModelTrainingExample
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.forecasting.meta_ensemble_v1 import meta_ensemble_weights
from kalshi_predictor.forecasting.meta_model_v1 import MetaModelV1Forecaster
from kalshi_predictor.meta.diagnostics import meta_diagnostics
from kalshi_predictor.meta.evaluator import evaluate_meta_model
from kalshi_predictor.meta.explanations import explain_meta_selection
from kalshi_predictor.meta.feature_builder import (
    build_meta_features_for_ticker,
    model_disagreement_score,
)
from kalshi_predictor.meta.repository import insert_meta_model_decision
from kalshi_predictor.meta.selector import _select_model, score_candidate_models
from kalshi_predictor.meta.trainer import build_meta_training_examples
from kalshi_predictor.research.assistant import research_opportunity
from kalshi_predictor.scheduler import scheduler_plan
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_meta_feature_builder_handles_missing_specialized_features(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="META-MISSING")
        _seed_forecast(
            session,
            ticker=snapshot.ticker,
            model_name="ensemble_v2",
            probability="0.70",
        )
        _seed_forecast(
            session,
            ticker=snapshot.ticker,
            model_name="market_implied_v1",
            probability="0.50",
        )

        feature = build_meta_features_for_ticker(session, ticker=snapshot.ticker)

    assert feature is not None
    assert feature["ticker"] == "META-MISSING"
    assert feature["crypto_features_json"] == {}
    assert feature["news_features_json"] == {}
    assert Decimal(feature["model_disagreement_score"]) == Decimal("0.2")


def test_model_disagreement_score_calculation() -> None:
    score = model_disagreement_score({"a": "0.35", "b": "0.60", "c": "0.55"})

    assert score == Decimal("0.25")


def test_training_builder_skips_unsettled_markets(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="META-OPEN")
        _seed_forecast(
            session,
            ticker=snapshot.ticker,
            model_name="ensemble_v2",
            probability="0.60",
        )
        summary = build_meta_training_examples(session, days=90)

    assert summary.settled_markets_scanned == 0
    assert summary.examples_inserted == 0


def test_training_builder_marks_best_model_by_brier_loss(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="META-SETTLED")
        good = _seed_forecast(
            session,
            ticker=snapshot.ticker,
            model_name="ensemble_v2",
            probability="0.80",
        )
        bad = _seed_forecast(
            session,
            ticker=snapshot.ticker,
            model_name="market_implied_v1",
            probability="0.40",
        )
        upsert_settlement(session, {"ticker": snapshot.ticker, "result": "yes"})

        summary = build_meta_training_examples(session, days=90)
        rows = list(session.scalars(select(MetaModelTrainingExample)))

    assert summary.examples_inserted == 2
    assert {row.forecast_id for row in rows} == {good.id, bad.id}
    best = next(row for row in rows if row.forecast_id == good.id)
    assert best.was_best_model == 1
    assert Decimal(best.brier_loss) == Decimal("0.04")


def test_selector_boosts_category_matched_model() -> None:
    feature = _feature_payload(
        category="crypto",
        probabilities={"crypto_v2": "0.70", "weather_v2": "0.70"},
        crypto_features={"feature": {"momentum_score": "80"}},
        active_signals=[{"category": "Crypto", "signal_name": "Crypto Signal"}],
    )

    scores = score_candidate_models(feature)

    assert scores["crypto_v2"] > scores["weather_v2"]


def test_selector_penalizes_stale_features() -> None:
    fresh = _feature_payload(
        category="general",
        probabilities={"market_implied_v1": "0.55"},
        freshness="100",
    )
    stale = _feature_payload(
        category="general",
        probabilities={"market_implied_v1": "0.55"},
        freshness="0",
    )

    assert score_candidate_models(stale)["market_implied_v1"] < score_candidate_models(fresh)[
        "market_implied_v1"
    ]


def test_selector_falls_back_to_ensemble_v2() -> None:
    selected, fallback = _select_model({"ensemble_v2": "0.58"}, {})

    assert selected == "ensemble_v2"
    assert fallback == "ensemble_v2"


def test_selector_falls_back_to_market_implied_when_needed() -> None:
    selected, fallback = _select_model({"market_implied_v1": "0.51"}, {})

    assert selected == "market_implied_v1"
    assert fallback == "market_implied_v1"


def test_meta_model_v1_uses_selected_model_probability(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="BTC-META", title="Will Bitcoin close higher?")
        _seed_crypto_context(session, ticker=snapshot.ticker)
        _seed_forecast(session, ticker=snapshot.ticker, model_name="crypto_v2", probability="0.72")
        _seed_forecast(
            session,
            ticker=snapshot.ticker,
            model_name="ensemble_v2",
            probability="0.55",
        )
        _seed_forecast(
            session,
            ticker=snapshot.ticker,
            model_name="market_implied_v1",
            probability="0.50",
        )

        forecast = MetaModelV1Forecaster().forecast(session, snapshot)

    assert forecast is not None
    assert forecast.model_name == "meta_model_v1"
    assert forecast.yes_probability == Decimal("0.72")
    assert forecast.feature_json["selected_model"] == "crypto_v2"


def test_meta_ensemble_v1_normalizes_trust_weights() -> None:
    weights, _reason = meta_ensemble_weights(
        probabilities={"market_implied_v1": "0.50", "ensemble_v2": "0.70"},
        trust_scores={"market_implied_v1": "30", "ensemble_v2": "70"},
        disagreement=Decimal("0.20"),
    )

    assert sum(weights.values(), Decimal("0")).quantize(Decimal("0.0001")) == Decimal("1.0000")
    assert weights["ensemble_v2"] > weights["market_implied_v1"]


def test_meta_evaluator_handles_insufficient_data(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        result = evaluate_meta_model(session, days=90, persist=True)

    assert result.performance is not None
    assert result.performance["evaluated_count"] == 0
    assert "Insufficient data" in result.performance["notes"]


def test_diagnostics_identify_high_disagreement_and_fallback_overuse(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        for index in range(3):
            insert_meta_model_decision(
                session,
                {
                    "ticker": f"META-DIAG-{index}",
                    "selected_model_name": "ensemble_v2",
                    "selected_probability": Decimal("0.55"),
                    "selected_confidence": Decimal("40"),
                    "fallback_model_name": "ensemble_v2",
                    "decision_reason": "fallback",
                    "competing_models": {},
                    "trust_scores": {},
                    "raw_json": {"model_disagreement_score": "0.30"},
                },
            )
        diagnostics = meta_diagnostics(session)

    titles = {row["title"] for row in diagnostics}
    assert "High model disagreement" in titles
    assert "Fallback overuse" in titles


def test_research_explanation_includes_selected_model_reason(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_snapshot(session, ticker="META-RESEARCH")
        insert_meta_model_decision(
            session,
            {
                "ticker": "META-RESEARCH",
                "selected_model_name": "ensemble_v2",
                "selected_probability": Decimal("0.60"),
                "selected_confidence": Decimal("80"),
                "decision_reason": "ensemble_v2 has the strongest settled evidence.",
                "competing_models": {},
                "trust_scores": {"ensemble_v2": "80"},
            },
        )
        context = research_opportunity(session, ticker="META-RESEARCH")
        explanation = explain_meta_selection(session, "META-RESEARCH")

    assert context["meta_selection"]["selected_model"] == "ensemble_v2"
    assert "ensemble_v2" in explanation["summary"]


def test_ui_meta_page_renders(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_snapshot(session, ticker="META-UI")
        insert_meta_model_decision(
            session,
            {
                "ticker": "META-UI",
                "selected_model_name": "ensemble_v2",
                "selected_probability": Decimal("0.60"),
                "selected_confidence": Decimal("75"),
                "decision_reason": "UI smoke test decision.",
                "competing_models": {},
                "trust_scores": {"ensemble_v2": "75"},
            },
        )
        session.commit()

    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))
    response = client.get("/meta")
    detail = client.get("/meta/META-UI")

    assert response.status_code == 200
    assert "Meta Model" in response.text
    assert "META-UI" in response.text
    assert detail.status_code == 200
    assert "Trusted model" in detail.text


def test_scheduler_meta_watch_profile_exists() -> None:
    plan = scheduler_plan("meta-watch")

    assert any("build-meta-features" in step.command for step in plan)
    assert any("forecast --model meta_model_v1" in step.command for step in plan)
    assert any("meta-report" in step.command for step in plan)


def test_phase_3l_cli_smoke() -> None:
    runner = CliRunner()
    for command in (
        "build-meta-features",
        "build-meta-training",
        "meta-evaluate",
        "meta-report",
        "meta-opportunities",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3l.db'}")
    return get_session_factory(engine)


def _seed_snapshot(
    session,
    *,
    ticker: str,
    title: str | None = None,
):
    now = utc_now()
    return insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": title or f"Will {ticker} resolve yes?",
            "series_ticker": "KXMETA",
            "event_ticker": "KXMETA-EVENT",
            "close_time": (now + timedelta(hours=2)).isoformat(),
            "yes_bid_dollars": "0.45",
            "yes_ask_dollars": "0.55",
            "liquidity_dollars": "500",
            "volume_fp": "500",
            "open_interest_fp": "100",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.45", "20"]],
                "no_dollars": [["0.45", "20"]],
            }
        },
        now,
    )


def _seed_forecast(session, *, ticker: str, model_name: str, probability: str):
    forecast = insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
            forecasted_at=utc_now(),
            model_name=model_name,
            yes_probability=Decimal(probability),
            market_mid_probability=Decimal("0.50"),
            best_yes_bid=Decimal("0.45"),
            best_yes_ask=Decimal("0.55"),
            feature_json={"test": "phase3l"},
            notes="Phase 3L test forecast.",
        ),
    )
    session.flush()
    return forecast


def _seed_crypto_context(session, *, ticker: str) -> None:
    insert_crypto_market_link(
        session,
        ticker=ticker,
        symbol="BTC",
        confidence=Decimal("0.95"),
        reason="BTC test market.",
    )
    insert_crypto_features(
        session,
        symbol="BTC",
        source="test",
        generated_at=utc_now(),
        window_minutes=60,
        features={
            "price": Decimal("65000"),
            "return_24h": Decimal("0.05"),
            "momentum_score": Decimal("90"),
            "trend_direction": "UP",
        },
    )


def _feature_payload(
    *,
    category: str,
    probabilities: dict[str, str],
    freshness: str = "100",
    active_signals: list[dict] | None = None,
    crypto_features: dict | None = None,
) -> dict:
    return {
        "category": category,
        "model_probabilities": probabilities,
        "model_recent_performance": {},
        "active_signals": active_signals or [],
        "data_freshness_score": freshness,
        "spread_score": "90",
        "liquidity_score": "90",
        "model_disagreement_score": str(model_disagreement_score(probabilities)),
        "crypto_features": crypto_features or {},
        "weather_features": {},
        "sports_features": {},
        "economic_features": {},
        "news_features": {},
        "microstructure_features": {},
    }
