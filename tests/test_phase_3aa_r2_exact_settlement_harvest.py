from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market, upsert_settlement
from kalshi_predictor.data.schema import PaperOrder, PaperPnl, Settlement
from kalshi_predictor.paper.models import ORDER_FILLED
from kalshi_predictor.paper.settlement_reconciliation import (
    build_paper_settlement_reconciliation,
)
from kalshi_predictor.phase3aa_r2 import (
    run_exact_ticker_settlement_harvest,
    write_phase3aa_r2_exact_settlement_harvest_report,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3aa_r2_writes_only_exact_ticker_settlement(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KX3AAR2-EXACT"
    client = _FakeExactMarketClient(
        {
            ticker: {
                "ticker": ticker,
                "status": "settled",
                "result": "yes",
                "settlement_ts": "2026-06-24T12:00:00Z",
                "title": "Exact settlement source market",
            }
        }
    )
    with session_factory() as session:
        session.add(_paper_order(ticker))
        _active_due_market(session, ticker)

        payload = run_exact_ticker_settlement_harvest(session, client=client)

        settlement = session.get(Settlement, ticker)

    assert client.requested == [ticker]
    assert payload["summary"]["due_or_overdue_rows_reviewed"] == 1
    assert payload["summary"]["exact_tickers_checked"] == 1
    assert payload["summary"]["exact_settlements_written"] == 1
    assert payload["summary"]["eligible_exact_settlements_after"] == 1
    assert payload["summary"]["pnl_realized"] is False
    assert payload["summary"]["live_orders_created"] == 0
    assert settlement is not None
    assert settlement.result == "yes"


def test_phase3aa_r2_keeps_open_exact_source_unsettled(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KX3AAR2-OPEN"
    client = _FakeExactMarketClient(
        {
            ticker: {
                "ticker": ticker,
                "status": "active",
                "title": "Exact open source market",
            }
        }
    )
    with session_factory() as session:
        session.add(_paper_order(ticker))
        _active_due_market(session, ticker)

        payload = run_exact_ticker_settlement_harvest(session, client=client)

        settlement = session.get(Settlement, ticker)

    assert payload["summary"]["exact_settlements_written"] == 0
    assert payload["summary"]["eligible_exact_settlements_after"] == 0
    assert payload["summary"]["source_not_settled"] == 1
    assert payload["rows"][0]["source_fetch_status"] == "SOURCE_NOT_SETTLED"
    assert settlement is None


def test_phase3aa_r2_marks_closed_without_outcome_as_blocked_diagnostic(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KX3AAR2-CLOSED"
    client = _FakeExactMarketClient(
        {
            ticker: {
                "ticker": ticker,
                "status": "closed",
                "result": "",
                "expiration_value": "",
                "custom_strike": {
                    "associated_events": [{"ticker": "KXEVENT"}],
                    "associated_markets": [{"ticker": "KXCHILD"}],
                    "associated_market_sides": [{"ticker": "KXCHILD", "side": "yes"}],
                },
                "title": "Closed exact source market",
            }
        }
    )
    with session_factory() as session:
        session.add(_paper_order(ticker))
        _active_due_market(session, ticker)

        payload = run_exact_ticker_settlement_harvest(session, client=client)

        settlement = session.get(Settlement, ticker)

    assert payload["summary"]["exact_settlements_written"] == 0
    assert payload["summary"]["source_closed_without_outcome"] == 1
    assert payload["summary"]["source_settled_without_usable_outcome"] == 0
    assert payload["rows"][0]["source_fetch_status"] == "SOURCE_CLOSED_WITHOUT_OUTCOME"
    assert payload["rows"][0]["source_associated_markets_count"] == 1
    assert settlement is None


def test_phase3aa_r2_accepts_settlement_value_alias(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KX3AAR2-SETTLEMENTVALUE"
    client = _FakeExactMarketClient(
        {
            ticker: {
                "ticker": ticker,
                "status": "settled",
                "settlement_value": "0",
                "settlement_ts": "2026-06-24T12:00:00Z",
            }
        }
    )
    with session_factory() as session:
        session.add(_paper_order(ticker))
        _active_due_market(session, ticker)

        payload = run_exact_ticker_settlement_harvest(session, client=client)

        settlement = session.get(Settlement, ticker)

    assert payload["summary"]["exact_settlements_written"] == 1
    assert settlement is not None
    assert settlement.yes_settlement_value == "0"


def test_phase3aa_r2_blocks_ticker_identity_mismatch(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KX3AAR2-IDENTITY"
    client = _FakeExactMarketClient(
        {
            ticker: {
                "ticker": "KX3AAR2-SIBLING",
                "status": "settled",
                "result": "yes",
                "settlement_ts": "2026-06-24T12:00:00Z",
            }
        }
    )
    with session_factory() as session:
        session.add(_paper_order(ticker))
        _active_due_market(session, ticker)

        payload = run_exact_ticker_settlement_harvest(session, client=client)

        exact = session.get(Settlement, ticker)
        sibling = session.get(Settlement, "KX3AAR2-SIBLING")

    assert payload["summary"]["identity_mismatches"] == 1
    assert payload["summary"]["exact_settlements_written"] == 0
    assert payload["rows"][0]["source_fetch_status"] == "TICKER_IDENTITY_MISMATCH"
    assert exact is None
    assert sibling is None


def test_phase3aa_r2_skips_local_derived_composite_exact_fetch(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KXMVESPORTSMULTIGAMEEXTENDED-S2026LOCAL-ABC123"
    client = _FailingExactMarketClient()
    with session_factory() as session:
        session.add(_paper_order(ticker))
        _active_due_market(session, ticker)

        payload = run_exact_ticker_settlement_harvest(session, client=client)

        settlement = session.get(Settlement, ticker)

    assert client.requested == []
    assert payload["summary"]["exact_tickers_checked"] == 1
    assert payload["summary"]["fetch_errors"] == 0
    assert payload["summary"]["retryable_fetch_errors"] == 0
    assert payload["summary"]["local_derived_not_exchange_market"] == 1
    assert payload["summary"]["exact_settlements_written"] == 0
    assert payload["rows"][0]["source_fetch_status"] == "LOCAL_DERIVED_TICKER_NOT_EXCHANGE_MARKET"
    assert payload["rows"][0]["retryable"] is False
    assert payload["rows"][0]["exact_settlement_written"] is False
    assert "local derived composite tickers" in payload["recommended_next_action"]
    assert settlement is None


def test_settlement_doctor_marks_realized_exact_settlement_not_eligible(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KX3AAR2-REALIZED"
    with session_factory() as session:
        session.add(_paper_order(ticker))
        _active_due_market(session, ticker)
        upsert_settlement(
            session,
            {
                "ticker": ticker,
                "result": "yes",
                "settlement_ts": "2026-06-24T12:00:00Z",
            },
        )
        session.add(
            PaperPnl(
                ticker=ticker,
                calculated_at=utc_now(),
                yes_contracts=1,
                no_contracts=0,
                avg_yes_price="0.50",
                avg_no_price=None,
                settlement_result="yes",
                realized_pnl="0.50",
                unrealized_pnl="0",
                total_pnl="0.50",
                notes="settled market realized paper P&L",
            )
        )
        session.flush()

        payload = build_paper_settlement_reconciliation(session)

    assert payload["summary"]["eligible_to_settle_now"] == 0
    assert payload["reason_counts"]["ALREADY_REALIZED"] == 1
    assert payload["rows"][0]["reason"] == "ALREADY_REALIZED"


def test_settlement_doctor_blocks_local_derived_composite_without_sibling_scan(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KXMVESPORTSMULTIGAMEEXTENDED-S2026LOCAL-ABC123"
    sibling_ticker = "KXMVESPORTSMULTIGAMEEXTENDED-S2026LOCAL-SIBLING"
    with session_factory() as session:
        session.add(_paper_order(ticker))
        _active_due_market(session, ticker)
        _active_due_market(session, sibling_ticker)
        upsert_settlement(
            session,
            {
                "ticker": sibling_ticker,
                "result": "yes",
                "settlement_ts": "2026-06-24T12:00:00Z",
            },
        )
        session.flush()

        payload = build_paper_settlement_reconciliation(session)

    row = payload["rows"][0]
    assert payload["summary"]["eligible_to_settle_now"] == 0
    assert payload["reason_counts"]["LOCAL_DERIVED_COMPOSITE_NO_EXACT_SETTLEMENT"] == 1
    assert row["reason"] == "LOCAL_DERIVED_COMPOSITE_NO_EXACT_SETTLEMENT"
    assert row["is_local_derived_composite"] is True
    assert row["possible_settlement_matches"] == []
    assert row["settlement_resolution_policy"] == "EXACT_TICKER_ONLY"
    assert "derived composite tickers" in payload["recommended_next_action"]


def test_phase3aa_r2_writer_outputs_reports(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KX3AAR2-WRITER"
    output_dir = Path(tmp_path) / "phase3aa_r2"
    client = _FakeExactMarketClient(
        {
            ticker: {
                "ticker": ticker,
                "status": "settled",
                "settlement_value_dollars": "1",
                "settlement_ts": "2026-06-24T12:00:00Z",
            }
        }
    )
    with session_factory() as session:
        session.add(_paper_order(ticker))
        _active_due_market(session, ticker)

        artifacts = write_phase3aa_r2_exact_settlement_harvest_report(
            session,
            output_dir=output_dir,
            client=client,
        )

    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.rows_path.exists()
    assert "Exact Settlement Harvest" in artifacts.markdown_path.read_text(encoding="utf-8")


def test_phase3aa_r2_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3aa-r2-exact-settlement-harvest", "--help"])

    assert result.exit_code == 0
    assert "Usage" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3aa_r2.db'}")
    return get_session_factory(engine)


def _paper_order(ticker: str) -> PaperOrder:
    return PaperOrder(
        ticker=ticker,
        forecast_id=None,
        created_at=utc_now(),
        model_name="ensemble_v2",
        side="BUY_YES",
        probability="0.55",
        market_price="0.50",
        limit_price="0.50",
        edge="0.05",
        quantity=1,
        status=ORDER_FILLED,
        reason="phase3aa-r2 test",
        raw_decision_json="{}",
    )


def _active_due_market(session, ticker: str) -> None:
    upsert_market(
        session,
        {
            "ticker": ticker,
            "status": "active",
            "title": "Due paper market",
            "close_time": utc_now() - timedelta(hours=2),
        },
    )


class _FakeExactMarketClient:
    def __init__(self, markets: dict[str, dict[str, Any]]) -> None:
        self.markets = markets
        self.requested: list[str] = []

    def get_market(self, ticker: str) -> dict[str, Any]:
        self.requested.append(ticker)
        return self.markets[ticker]


class _FailingExactMarketClient:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def get_market(self, ticker: str) -> dict[str, Any]:
        self.requested.append(ticker)
        raise AssertionError("local derived composite ticker should not call exact market API")
