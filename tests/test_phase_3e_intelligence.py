from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.opportunities.payout_scoring import calculate_payout_metrics
from kalshi_predictor.opportunities.reports import (
    best_payout_rows,
    generate_best_payouts_report,
)
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.paper.ledger import upsert_position
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.market_display import (
    classify_market_category,
    risk_meter,
    summarize_market_title,
    traffic_light_label,
)
from kalshi_predictor.utils.time import utc_now


def test_market_title_summarizer_shortens_long_sports_title() -> None:
    title = "yes Over 5.5 runs scored,yes Over 3.5 runs scored,yes Over 7.5 runs scored"

    assert summarize_market_title(title) == "MLB Multi-Game Runs Market"


def test_market_title_summarizer_does_not_match_eth_inside_netherlands() -> None:
    title = "Will Netherlands win in Extra Time?"

    assert summarize_market_title(title) == "Netherlands win in Extra Time?"


def test_category_classifier_detects_core_categories() -> None:
    assert classify_market_category("Will the Yankees win?", "KXSPORTS") == "Sports"
    assert classify_market_category("Will Bitcoin be above 100k?", None) == "Crypto"
    assert classify_market_category("Will ETH be above 4000?", None) == "Crypto"
    assert classify_market_category("Will it rain in New York?", None) == "Weather"


def test_category_classifier_does_not_match_eth_inside_netherlands() -> None:
    assert classify_market_category("Will Netherlands win in Extra Time?", None) != "Crypto"
    assert classify_market_category("Will Netherlands win in Extra Time?", "KXWCMOV") == "Sports"


def test_traffic_light_returns_strong_watchlist_and_avoid() -> None:
    strong = traffic_light_label(
        opportunity_score="85",
        edge="0.07",
        spread="0.04",
        liquidity="80",
        confidence="75",
        is_fresh=True,
    )
    watchlist = traffic_light_label(
        opportunity_score="65",
        edge="0.03",
        spread="0.07",
        liquidity="60",
        confidence="55",
        is_fresh=True,
    )
    avoid = traffic_light_label(
        opportunity_score="85",
        edge="0.07",
        spread="0.04",
        liquidity="80",
        confidence="75",
        is_fresh=False,
    )

    assert strong["label"] == "Strong Opportunity"
    assert watchlist["label"] == "Watchlist"
    assert avoid["label"] == "Avoid"


def test_risk_meter_flags_stale_data_and_wide_spread() -> None:
    stale = risk_meter(
        opportunity_score="88",
        edge="0.08",
        spread="0.04",
        liquidity="90",
        confidence="80",
        is_fresh=False,
    )
    wide = risk_meter(
        opportunity_score="88",
        edge="0.08",
        spread="0.20",
        liquidity="90",
        confidence="80",
        is_fresh=True,
    )

    assert any(factor["label"] == "Stale data" for factor in stale["factors"])
    assert any(factor["label"] == "Wide spread" for factor in wide["factors"])


def test_expected_value_calculation_for_buy_yes_and_buy_no() -> None:
    yes = calculate_payout_metrics(
        side=BUY_YES,
        yes_probability=Decimal("0.66"),
        cost=Decimal("0.48"),
        confidence_score="80",
    )
    no = calculate_payout_metrics(
        side=BUY_NO,
        yes_probability=Decimal("0.30"),
        cost=Decimal("0.45"),
        confidence_score="80",
    )

    assert yes.expected_value == Decimal("0.1800")
    assert no.expected_value == Decimal("0.2500")


def test_payout_to_risk_ratio_calculation() -> None:
    metrics = calculate_payout_metrics(
        side=BUY_YES,
        yes_probability=Decimal("0.66"),
        cost=Decimal("0.48"),
    )

    assert metrics.payout_to_risk_ratio is not None
    assert metrics.payout_to_risk_ratio.quantize(Decimal("0.01")) == Decimal("1.08")


def test_best_payouts_excludes_low_confidence_longshots(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_phase3e_market(session)
        _seed_low_confidence_longshot(session)
        rows = best_payout_rows(session, model_name="ensemble_v2", limit=10)
        report = generate_best_payouts_report(
            session,
            model_name="ensemble_v2",
            limit=10,
            output_path=Path(tmp_path) / "best_payouts.md",
        )

    assert [row["ticker"] for row in rows] == ["PHASE3E-GOOD"]
    assert "Best Payout" in report.read_text(encoding="utf-8")


def test_dashboard_renders_executive_summary_and_hides_raw_json(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_phase3e_market(session)
        session.commit()
    client = TestClient(
        create_app(
            session_factory=session_factory,
            settings=Settings(overnight_require_market_data=False),
        )
    )

    response = client.get("/")

    assert response.status_code == 200
    assert "Today's Summary" in response.text
    assert "Paper Portfolio" in response.text
    assert "View Market Details" in response.text
    assert "raw_json" not in response.text


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3e.db'}")
    return get_session_factory(engine)


def _seed_phase3e_market(session) -> None:
    now = utc_now()
    snapshot = insert_market_snapshot(
        session,
        {
            "ticker": "PHASE3E-GOOD",
            "status": "open",
            "title": "Will Bitcoin be above 100k by July 31?",
            "series_ticker": "KXCRYPTO",
            "event_ticker": "KXCRYPTO-EVENT",
            "close_time": (now + timedelta(hours=5)).isoformat(),
            "volume_fp": "1000",
            "open_interest_fp": "500",
            "liquidity_dollars": "12000",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.48", "20"]],
                "no_dollars": [["0.50", "20"]],
            }
        },
        now,
    )
    insert_forecast(
        session,
        ForecastOutput(
            ticker="PHASE3E-GOOD",
            forecasted_at=now,
            model_name="ensemble_v2",
            yes_probability=Decimal("0.66"),
            market_mid_probability=None,
            best_yes_bid=Decimal("0.46"),
            best_yes_ask=Decimal(snapshot.best_yes_ask),
            feature_json={"component_forecasts": {"crypto_v2": "0.68"}},
        ),
    )
    insert_market_ranking(
        session,
        {
            "ticker": "PHASE3E-GOOD",
            "ranked_at": now,
            "title": "Will Bitcoin be above 100k by July 31?",
            "status": "open",
            "series_ticker": "KXCRYPTO",
            "forecast_model": "ensemble_v2",
            "forecast_probability": "0.66",
            "best_side": "BUY_YES",
            "best_price": "0.48",
            "estimated_edge": "0.18",
            "liquidity_score": "85",
            "spread_score": "90",
            "time_score": "80",
            "model_confidence_score": "82",
            "opportunity_score": "88",
            "spread": "0.06",
            "liquidity": "12000",
            "time_to_close_minutes": "300",
            "reason": "Seeded Phase 3E payout opportunity.",
        },
    )
    upsert_position(
        session,
        ticker="PHASE3E-GOOD",
        yes_contracts=2,
        no_contracts=0,
        avg_yes_price=Decimal("0.44"),
        avg_no_price=None,
        realized_pnl=Decimal("0"),
    )
    session.flush()


def _seed_low_confidence_longshot(session) -> None:
    now = utc_now()
    insert_market_ranking(
        session,
        {
            "ticker": "PHASE3E-LONGSHOT",
            "ranked_at": now,
            "title": "Will a low confidence longshot resolve yes?",
            "status": "open",
            "series_ticker": "KXGENERAL",
            "forecast_model": "ensemble_v2",
            "forecast_probability": "0.30",
            "best_side": "BUY_YES",
            "best_price": "0.12",
            "estimated_edge": "0.18",
            "liquidity_score": "90",
            "spread_score": "95",
            "time_score": "80",
            "model_confidence_score": "25",
            "opportunity_score": "92",
            "spread": "0.03",
            "liquidity": "15000",
            "time_to_close_minutes": "300",
            "reason": "Low-confidence longshot should be filtered.",
        },
    )
    session.flush()
