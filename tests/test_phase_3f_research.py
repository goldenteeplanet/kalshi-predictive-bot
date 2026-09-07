from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import OpportunityResearchSnapshot
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.research.evidence import build_opportunity_evidence
from kalshi_predictor.research.narratives import generate_narrative
from kalshi_predictor.research.questions import answer_research_question
from kalshi_predictor.research.reports import generate_research_report
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_evidence_builder_handles_missing_ticker(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        evidence = build_opportunity_evidence(
            session,
            ticker="MISSING",
            model_name="ensemble_v2",
        )

    assert evidence["found"] is False
    assert "opportunity ranking" in evidence["missing_data"]


def test_evidence_builder_gathers_forecast_and_opportunity_data(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_research_market(session)
        evidence = build_opportunity_evidence(
            session,
            ticker="RESEARCH-GOOD",
            model_name="ensemble_v2",
        )

    assert evidence["found"] is True
    assert evidence["ticker"] == "RESEARCH-GOOD"
    assert evidence["model_probability"] == "0.66"
    assert evidence["opportunity_score"] == "88"
    assert evidence["market_price"] == "0.48"


def test_narrative_explains_edge_and_missing_crypto_without_hallucinating(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_research_market(session)
        evidence = build_opportunity_evidence(
            session,
            ticker="RESEARCH-GOOD",
            model_name="ensemble_v2",
        )
        narrative = generate_narrative(evidence)

    combined = " ".join(
        [
            narrative["why_ranked"],
            narrative["bot_thinks"],
            " ".join(narrative["supporting_signals"]),
        ]
    )
    assert "estimated edge" in narrative["why_ranked"]
    assert "BTC is up" not in combined
    assert "crypto features" in evidence["missing_data"]


def test_narrative_explains_low_liquidity_risk(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_research_market(session, ticker="RESEARCH-RISKY", liquidity_score="20")
        evidence = build_opportunity_evidence(
            session,
            ticker="RESEARCH-RISKY",
            model_name="ensemble_v2",
        )
        narrative = generate_narrative(evidence)

    assert any("Low liquidity" in risk for risk in narrative["risks"])


def test_question_layer_answers_rank_and_missing_data(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_research_market(session)
        ranked = answer_research_question(
            session,
            question="Why is this ranked #1?",
            model_name="ensemble_v2",
        )
        missing = answer_research_question(
            session,
            question="What data is missing?",
            ticker="RESEARCH-GOOD",
            model_name="ensemble_v2",
        )

    assert "ranked" in ranked["answer"]
    assert "crypto features" in missing["answer"]


def test_research_report_creates_markdown_and_stores_snapshots(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output = Path(tmp_path) / "research_report.md"
    with session_factory() as session:
        _seed_research_market(session)
        report_path = generate_research_report(
            session,
            model_name="ensemble_v2",
            limit=10,
            output_path=output,
        )
        session.commit()
        snapshot_count = session.scalar(select(func.count(OpportunityResearchSnapshot.id)))

    assert report_path.exists()
    assert "Research Assistant Report" in report_path.read_text(encoding="utf-8")
    assert snapshot_count == 1


def test_ui_research_pages_and_card_link_render(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_research_market(session)
        session.commit()
    client = TestClient(
        create_app(
            session_factory=session_factory,
            settings=Settings(overnight_require_market_data=False),
        )
    )

    dashboard = client.get("/dashboard")
    research = client.get("/research")
    why = client.get("/research/opportunity/RESEARCH-GOOD?model_name=ensemble_v2")

    assert dashboard.status_code == 200
    assert research.status_code == 200
    assert "Research Assistant" in research.text
    assert "/research/opportunity/RESEARCH-GOOD" in research.text
    assert why.status_code == 200
    assert "Analyst Writeup" in why.text


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3f.db'}")
    return get_session_factory(engine)


def _seed_research_market(
    session,
    *,
    ticker: str = "RESEARCH-GOOD",
    liquidity_score: str = "85",
) -> None:
    now = utc_now()
    snapshot = insert_market_snapshot(
        session,
        {
            "ticker": ticker,
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
                "yes_dollars": [["0.46", "20"]],
                "no_dollars": [["0.50", "20"]],
            }
        },
        now,
    )
    insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
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
            "ticker": ticker,
            "ranked_at": now,
            "title": "Will Bitcoin be above 100k by July 31?",
            "status": "open",
            "series_ticker": "KXCRYPTO",
            "forecast_model": "ensemble_v2",
            "forecast_probability": "0.66",
            "best_side": "BUY_YES",
            "best_price": "0.48",
            "estimated_edge": "0.18",
            "liquidity_score": liquidity_score,
            "spread_score": "90",
            "time_score": "80",
            "model_confidence_score": "82",
            "opportunity_score": "88",
            "spread": "0.06",
            "liquidity": "12000",
            "time_to_close_minutes": "300",
            "reason": "Seeded Phase 3F research opportunity.",
        },
    )
    session.flush()
