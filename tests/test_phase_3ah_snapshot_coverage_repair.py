from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    insert_forecast,
    insert_market_snapshot,
    upsert_market,
)
from kalshi_predictor.data.schema import LearningRejectionLog, MarketSnapshot
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.paper.strategy import generate_paper_decisions
from kalshi_predictor.phase3ah import (
    REASON_MARKET_CLOSED,
    REASON_UNSUPPORTED_MULTILEG,
    run_snapshot_coverage_repair,
    write_snapshot_coverage_repair_report,
)
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.workstation.repository import market_monitor_rows


def test_snapshot_coverage_repair_detects_missing_ranking(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranking(session, ticker="PHASE3AH-MISSING", status="closed")

        result = run_snapshot_coverage_repair(session, client=_FakeSnapshotClient())

    assert result.ranked_markets_scanned == 1
    assert result.missing_data_rankings_found == 1


def test_snapshot_coverage_repair_stores_fresh_snapshot_when_data_exists(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranking(session, ticker="PHASE3AH-REPAIR")
        client = _FakeSnapshotClient(
            markets={
                "PHASE3AH-REPAIR": _market_payload(
                    "PHASE3AH-REPAIR",
                    liquidity_dollars="2500",
                )
            },
            orderbooks={
                "PHASE3AH-REPAIR": {
                    "orderbook_fp": {
                        "yes_dollars": [["0.42", "25"]],
                        "no_dollars": [["0.52", "25"]],
                    }
                }
            },
        )

        result = run_snapshot_coverage_repair(session, client=client)
        stored = session.scalar(
            select(MarketSnapshot).where(MarketSnapshot.ticker == "PHASE3AH-REPAIR")
        )

    assert result.snapshots_repaired == 1
    assert result.still_missing == 0
    assert stored is not None
    assert stored.spread == "0.06"


def test_snapshot_coverage_repair_classifies_closed_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(session, _market_payload("PHASE3AH-CLOSED", status="closed"))
        _seed_ranking(session, ticker="PHASE3AH-CLOSED", status="closed")

        result = run_snapshot_coverage_repair(session, client=_FakeSnapshotClient())

    assert result.still_missing == 1
    assert result.reason_counts[REASON_MARKET_CLOSED] == 1


def test_snapshot_coverage_repair_classifies_unsupported_multileg(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    title = "yes Team A,yes Team B,yes Team C"
    with session_factory() as session:
        _seed_ranking(
            session,
            ticker="KXMVESPORTSMULTIGAMEEXTENDED-PHASE3AH",
            title=title,
        )
        client = _FakeSnapshotClient(
            markets={
                "KXMVESPORTSMULTIGAMEEXTENDED-PHASE3AH": _market_payload(
                    "KXMVESPORTSMULTIGAMEEXTENDED-PHASE3AH",
                    title=title,
                )
            },
            orderbooks={
                "KXMVESPORTSMULTIGAMEEXTENDED-PHASE3AH": {"orderbook_fp": {}},
            },
        )

        result = run_snapshot_coverage_repair(session, client=client)

    assert result.still_missing == 1
    assert result.reason_counts[REASON_UNSUPPORTED_MULTILEG] == 1


def test_snapshot_coverage_repair_report_renders(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output = Path(tmp_path) / "snapshot_coverage_repair.md"
    with session_factory() as session:
        _seed_ranking(session, ticker="PHASE3AH-REPORT", status="closed")

        artifacts = write_snapshot_coverage_repair_report(
            session,
            client=_FakeSnapshotClient(),
            output=output,
        )

    assert artifacts.markdown_path.exists()
    assert artifacts.json_path.exists()
    assert "Phase 3AH Market Snapshot Coverage Repair" in output.read_text(encoding="utf-8")
    assert "market_closed" in output.read_text(encoding="utf-8")


def test_learning_mode_rejects_missing_market_snapshot_rankings(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with session_factory() as session:
        insert_market_snapshot(
            session,
            _market_payload("PHASE3AH-LEARNING", liquidity_dollars="1000"),
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "10"]],
                    "no_dollars": [["0.54", "10"]],
                }
            },
            now,
        )
        insert_forecast(
            session,
            ForecastOutput(
                ticker="PHASE3AH-LEARNING",
                forecasted_at=now,
                model_name="ensemble_v2",
                yes_probability=Decimal("0.65"),
                market_mid_probability=None,
                best_yes_bid=Decimal("0.40"),
                best_yes_ask=Decimal("0.46"),
                feature_json={"source": "phase3ah_test"},
            ),
        )
        _seed_ranking(
            session,
            ticker="PHASE3AH-LEARNING",
            forecast_model="ensemble_v2",
            best_price="0",
            spread="0",
            liquidity="1000",
        )

        result = generate_paper_decisions(
            session,
            settings=Settings(
                learning_mode=True,
                paper_min_edge=Decimal("0.01"),
                learning_min_edge=Decimal("0.01"),
                learning_min_opportunity_score=Decimal("35"),
            ),
        )
        rejection = session.scalar(select(LearningRejectionLog))

    assert result.decisions_generated == 0
    assert rejection is not None
    assert rejection.reason == "missing_market_snapshot"


def test_market_monitor_groups_unrepaired_missing_multileg_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranking(
            session,
            ticker="KXMVESPORTSMULTIGAMEEXTENDED-PHASE3AH-GROUP",
            title="yes Team A,yes Team B,yes Team C",
            opportunity_score="99",
        )

        rows = market_monitor_rows(session, limit=10)

    grouped = next(row for row in rows if row["ticker"] == "GROUPED-MISSING-SPORTS-MULTILEG")
    assert grouped["data_quality"] == "Missing market data"
    assert grouped["snapshot_repair_status"] == "Grouped unresolved"
    assert grouped["recommended_action"] == "Collect fresh snapshots before ranking"


class _FakeSnapshotClient:
    def __init__(
        self,
        *,
        markets: dict[str, dict[str, Any]] | None = None,
        orderbooks: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.markets = markets or {}
        self.orderbooks = orderbooks or {}

    def get_market(self, ticker: str) -> dict[str, Any]:
        if ticker not in self.markets:
            return _market_payload(ticker)
        return self.markets[ticker]

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return self.orderbooks.get(ticker, {"orderbook_fp": {}})


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ah.db'}")
    return get_session_factory(engine)


def _market_payload(
    ticker: str,
    *,
    title: str | None = None,
    status: str = "open",
    liquidity_dollars: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    return {
        "ticker": ticker,
        "status": status,
        "title": title or f"Will {ticker} resolve yes?",
        "series_ticker": "KXTEST",
        "event_ticker": "KXTEST-EVENT",
        "close_time": (now + timedelta(hours=2)).isoformat(),
        "liquidity_dollars": liquidity_dollars,
        "volume_fp": "0",
        "open_interest_fp": "0",
    }


def _seed_ranking(
    session,
    *,
    ticker: str,
    title: str | None = None,
    status: str = "open",
    forecast_model: str = "ensemble_v2",
    best_price: str = "0",
    spread: str = "0",
    liquidity: str = "0",
    opportunity_score: str = "88",
) -> None:
    insert_market_ranking(
        session,
        {
            "ticker": ticker,
            "ranked_at": utc_now(),
            "title": title or f"Will {ticker} resolve yes?",
            "status": status,
            "series_ticker": "KXTEST",
            "event_ticker": "KXTEST-EVENT",
            "forecast_model": forecast_model,
            "forecast_probability": "0.66",
            "best_side": "BUY_YES",
            "best_price": best_price,
            "midpoint": "0",
            "estimated_edge": "0.18",
            "liquidity": liquidity,
            "liquidity_score": "0",
            "spread": spread,
            "spread_score": "0",
            "time_to_close_minutes": "120",
            "time_score": "70",
            "model_confidence_score": "65",
            "opportunity_score": opportunity_score,
            "reason": "Seeded Phase 3AH snapshot coverage repair test.",
        },
    )
