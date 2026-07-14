from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kalshi_predictor.autopilot.runner import run_autopilot_once
from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import MarketLeg
from kalshi_predictor.explain.model_explainer import explain_model
from kalshi_predictor.explain.opportunity_explainer import explain_opportunity
from kalshi_predictor.explain.risk_explainer import explain_risks
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.decision_clarity import build_market_structure
from kalshi_predictor.utils.time import utc_now


def test_opportunity_explainer_handles_missing_data() -> None:
    explanation = explain_opportunity(None, settings=Settings())

    assert explanation["recommendation"] == "No trade recommended"
    assert "No local ranking exists" in explanation["why_interesting"]


def test_risk_explainer_flags_wide_spread() -> None:
    ranking = SimpleNamespace(
        spread="0.12",
        estimated_edge="0.08",
        liquidity_score="80",
        opportunity_score="75",
    )
    snapshot = SimpleNamespace(captured_at=utc_now())

    explanation = explain_risks(ranking, snapshot)

    assert explanation["top_risk"].startswith("The spread is wide")
    assert {"label": "High Spread", "kind": "caution"} in explanation["badges"]


def test_model_explainer_explains_ensemble_v2() -> None:
    explanation = explain_model(
        "ensemble_v2",
        forecast_probability="0.63",
        feature_json={
            "component_forecasts": {
                "market_implied_v1": {},
                "crypto_v2": {},
                "weather_v2": {},
            }
        },
    )

    assert "ensemble_v2 is combining" in explanation
    assert "market_implied_v1" in explanation
    assert "63.0%" in explanation


def test_ui_dashboard_renders_human_labels(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_opportunity(session_factory)
    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))

    response = client.get("/")

    assert response.status_code == 200
    assert "Decision Cockpit" in response.text
    assert "Best Opportunities Right Now" in response.text
    assert "Why This Looks Interesting" in response.text
    assert "Bot would buy YES" in response.text


def test_raw_json_hidden_by_default_behind_advanced_section(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_opportunity(session_factory)
    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))

    response = client.get("/opportunities/UI-TEST")

    assert response.status_code == 200
    assert "Advanced / Raw Data" in response.text
    assert '<details class="raw-data">' in response.text
    assert "Show raw feature JSON and market rules" in response.text


def test_phase_3ai_detail_separates_positive_signal_from_no_liquidity_block(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_no_liquidity_opportunity(session_factory)
    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))

    response = client.get("/opportunities/UI-NOLIQ")

    assert response.status_code == 200
    assert "Decision Summary" in response.text
    assert "Decision Waterfall" in response.text
    assert "Final Decision" in response.text
    assert "NO TRADE" in response.text
    assert "BUY YES" in response.text
    assert "No Liquidity" in response.text
    assert "INTERESTING_BUT_NOT_EXECUTABLE" in response.text
    assert "Why This Looks Interesting" in response.text
    assert "Why This Is Not Tradable" in response.text
    assert "What Would Make This Tradable?" in response.text
    assert "liquidity above the configured threshold" in response.text
    assert "Trace details" in response.text
    assert "demo-execute" not in response.text


def test_phase_3ai_market_structure_keeps_multileg_headline_clean() -> None:
    legs = [
        SimpleNamespace(
            leg_index=1,
            side="YES",
            category="sports",
            market_type="PLAYER_PROP",
            entity_name="Player A",
            operator="above",
            threshold_value="1.5",
            unit="goals",
            confidence="0.90",
            raw_text="Yes Player A above 1.5 goals",
            reason="parsed player prop",
        ),
        SimpleNamespace(
            leg_index=2,
            side="NO",
            category="sports",
            market_type="PLAYER_PROP",
            entity_name="Player B",
            operator="below",
            threshold_value="0.5",
            unit="goals",
            confidence="0.90",
            raw_text="No Player B below 0.5 goals",
            reason="parsed player prop",
        ),
    ]

    structure = build_market_structure(
        title="Yes Player A above 1.5 goals, No Player B below 0.5 goals",
        ticker="UI-MULTI",
        series_ticker="KXSPORTSMULTIGAME",
        market_type="multi_leg",
        market_legs=legs,
    )

    assert structure["parser_status"] == "UNSUPPORTED_MULTI_LEG"
    assert structure["clean_title"] == (
        "Sports multi-leg: Player A above 1.5 goals; Player B below 0.5 goals"
    )
    assert structure["parser_label"] == "Unsupported multi-leg market (2 options)"
    assert "Yes Player A" not in structure["clean_title"]
    assert structure["option_list"][0]["label"] == "Yes Player A above 1.5 goals"
    assert structure["option_list"][0]["human_label"] == "Player A above 1.5 goals"


def test_phase_3ai_multileg_detail_shows_lookup_and_component_tickers(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_multileg_opportunity(session_factory)
    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))

    response = client.get("/opportunities/UI-MULTI")

    assert response.status_code == 200
    assert "Sports multi-leg: Mexico; Bosnia and Herzegovina wins by more than 1.5 goals" in (
        response.text
    )
    assert "Unsupported multi-leg General market" not in response.text
    assert "Unsupported multi-leg market (2 options)" in response.text
    assert "Open combo event API" in response.text
    assert "Check exact API status" in response.text
    assert "Copy exact ticker" in response.text
    assert "Copy component tickers" in response.text
    assert "Copy exact ticker; open Kalshi search" not in response.text
    assert "Direct Kalshi web-search links are disabled" in response.text
    assert 'href="https://kalshi.com/search"' not in response.text
    assert "https://kalshi.com/search?query=UI-MULTI" not in response.text
    assert 'data-copy-text="UI-MULTI"' in response.text
    assert "KXWCGAME-26JUN24CZEMEX-MEX" in response.text
    assert "KXWCSPREAD-26JUN24BIHQAT-BIH2" in response.text


def test_autopilot_blocked_reason_displays_clearly(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        run_autopilot_once(session, settings=Settings(autopilot_enabled=False))
        session.commit()
    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))

    response = client.get("/autopilot")

    assert response.status_code == 200
    assert "Autopilot is OFF" in response.text
    assert "AUTOPILOT_ENABLED=false" in response.text
    assert "Top guardrail blocking trades" in response.text


def test_explain_opportunity_cli_smoke(tmp_path) -> None:
    db_path = Path(tmp_path) / "cli-explain.db"
    get_settings.cache_clear()
    result = CliRunner().invoke(
        app,
        ["explain-opportunity", "--ticker", "MISSING", "--model-name", "ensemble_v2"],
        env={"KALSHI_DB_URL": f"sqlite:///{db_path}"},
    )
    get_settings.cache_clear()

    assert result.exit_code == 0
    assert "Recommendation: No trade recommended" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3c.db'}")
    return get_session_factory(engine)


def _seed_opportunity(session_factory) -> None:
    with session_factory() as session:
        captured_at = utc_now()
        snapshot = insert_market_snapshot(
            session,
            {
                "ticker": "UI-TEST",
                "status": "open",
                "title": "Will this UI market resolve yes?",
                "rules_primary": "This is a local UI test market.",
                "close_time": (captured_at + timedelta(hours=4)).isoformat(),
                "volume_fp": "1000",
                "open_interest_fp": "500",
                "liquidity_dollars": "10000",
            },
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "10"]],
                    "no_dollars": [["0.50", "10"]],
                }
            },
            captured_at,
        )
        insert_forecast(
            session,
            ForecastOutput(
                ticker="UI-TEST",
                forecasted_at=captured_at,
                model_name="ensemble_v2",
                yes_probability=Decimal("0.63"),
                market_mid_probability=None,
                best_yes_bid=Decimal("0.40"),
                best_yes_ask=Decimal(snapshot.best_yes_ask),
                feature_json={
                    "component_forecasts": {
                        "market_implied_v1": {},
                        "crypto_v2": {},
                        "weather_v2": {},
                    }
                },
            ),
        )
        insert_market_ranking(
            session,
            {
                "ticker": "UI-TEST",
                "ranked_at": captured_at,
                "title": "Will this UI market resolve yes?",
                "status": "open",
                "forecast_model": "ensemble_v2",
                "forecast_probability": "0.63",
                "best_side": "BUY_YES",
                "best_price": "0.50",
                "estimated_edge": "0.13",
                "liquidity_score": "80",
                "spread_score": "90",
                "time_score": "70",
                "model_confidence_score": "65",
                "opportunity_score": "78",
                "spread": "0.10",
                "time_to_close_minutes": "240",
                "reason": "Seeded UI test opportunity.",
            },
        )
        session.commit()


def _seed_no_liquidity_opportunity(session_factory) -> None:
    with session_factory() as session:
        captured_at = utc_now()
        snapshot = insert_market_snapshot(
            session,
            {
                "ticker": "UI-NOLIQ",
                "status": "open",
                "title": "Will rain fall in the no-liquidity UI market?",
                "rules_primary": "This is a local UI liquidity test market.",
                "close_time": (captured_at + timedelta(hours=6)).isoformat(),
                "volume_fp": "0",
                "open_interest_fp": "0",
                "liquidity_dollars": "0",
            },
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "0"]],
                    "no_dollars": [["0.50", "0"]],
                }
            },
            captured_at,
        )
        insert_forecast(
            session,
            ForecastOutput(
                ticker="UI-NOLIQ",
                forecasted_at=captured_at,
                model_name="ensemble_v2",
                yes_probability=Decimal("0.65"),
                market_mid_probability=None,
                best_yes_bid=Decimal("0.40"),
                best_yes_ask=Decimal(snapshot.best_yes_ask),
                feature_json={"component_forecasts": {"market_implied_v1": {}}},
            ),
        )
        insert_market_ranking(
            session,
            {
                "ticker": "UI-NOLIQ",
                "ranked_at": captured_at,
                "title": "Will rain fall in the no-liquidity UI market?",
                "status": "open",
                "forecast_model": "ensemble_v2",
                "forecast_probability": "0.65",
                "best_side": "BUY_YES",
                "best_price": "0.50",
                "estimated_edge": "0.15",
                "liquidity": "0",
                "liquidity_score": "0",
                "spread_score": "90",
                "time_score": "70",
                "model_confidence_score": "70",
                "opportunity_score": "82",
                "spread": "0.05",
                "time_to_close_minutes": "360",
                "reason": "Positive model edge but no executable liquidity.",
            },
        )
        session.commit()


def _seed_multileg_opportunity(session_factory) -> None:
    with session_factory() as session:
        captured_at = utc_now()
        title = (
            "yes Mexico,"
            "yes Bosnia and Herzegovina wins by more than 1.5 goals"
        )
        snapshot = insert_market_snapshot(
            session,
            {
                "ticker": "UI-MULTI",
                "event_ticker": "KXMVESPORTSMULTIGAMEEXTENDED-S20268558FB03D19",
                "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
                "status": "open",
                "title": title,
                "rules_primary": "This is a local multi-leg UI test market.",
                "close_time": (captured_at + timedelta(days=4)).isoformat(),
                "market_type": "binary",
                "volume_fp": "100",
                "open_interest_fp": "50",
                "liquidity_dollars": "0",
                "yes_bid_dollars": "0.01",
                "yes_ask_dollars": "0.02",
                "mve_selected_legs": [
                    {
                        "event_ticker": "KXWCGAME-26JUN24CZEMEX",
                        "market_ticker": "KXWCGAME-26JUN24CZEMEX-MEX",
                        "side": "yes",
                    },
                    {
                        "event_ticker": "KXWCSPREAD-26JUN24BIHQAT",
                        "market_ticker": "KXWCSPREAD-26JUN24BIHQAT-BIH2",
                        "side": "yes",
                    },
                ],
            },
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.01", "0"]],
                    "no_dollars": [["0.98", "0"]],
                }
            },
            captured_at,
        )
        insert_forecast(
            session,
            ForecastOutput(
                ticker="UI-MULTI",
                forecasted_at=captured_at,
                model_name="ensemble_v2",
                yes_probability=Decimal("0.50"),
                market_mid_probability=None,
                best_yes_bid=Decimal("0.01"),
                best_yes_ask=Decimal(snapshot.best_yes_ask),
                feature_json={"component_forecasts": {"market_implied_v1": {}}},
            ),
        )
        for index, raw_text in enumerate(title.split(","), start=1):
            session.add(
                MarketLeg(
                    ticker="UI-MULTI",
                    leg_index=index,
                    parsed_at=captured_at,
                    side="YES",
                    category="general",
                    market_type="THRESHOLD",
                    entity_name=raw_text.replace("yes ", "", 1),
                    operator="UNKNOWN",
                    threshold_value="1.5" if index == 2 else None,
                    unit=None,
                    confidence="0.80",
                    raw_text=raw_text,
                    reason="test parser output",
                    raw_json="{}",
                )
            )
        insert_market_ranking(
            session,
            {
                "ticker": "UI-MULTI",
                "ranked_at": captured_at,
                "title": title,
                "status": "open",
                "forecast_model": "ensemble_v2",
                "forecast_probability": "0.50",
                "best_side": "BUY_YES",
                "best_price": "0.02",
                "estimated_edge": "0.48",
                "liquidity": "0",
                "liquidity_score": "0",
                "spread_score": "0",
                "time_score": "70",
                "model_confidence_score": "57.2",
                "opportunity_score": "57.2",
                "spread": "1.00",
                "time_to_close_minutes": "5760",
                "reason": "Positive model edge but unsupported multi-leg market.",
            },
        )
        session.commit()
