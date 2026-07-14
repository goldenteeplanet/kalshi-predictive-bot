from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market, upsert_settlement
from kalshi_predictor.data.schema import PaperOrder
from kalshi_predictor.paper.ledger import upsert_position
from kalshi_predictor.paper.models import BUY_YES, ORDER_FILLED
from kalshi_predictor.paper.pnl import calculate_and_store_pnl
from kalshi_predictor.paper.settlement_reconciliation import build_paper_settlement_reconciliation
from kalshi_predictor.phase3aa_r3 import (
    build_phase3aa_r3_residual_audit,
    write_phase3aa_r3_residual_audit_report,
)


def test_phase3aa_r3_classifies_scalar_ready_then_cleared_after_realizer(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KX3AAR3-SCALAR"
    with session_factory() as session:
        _seed_order_market_settlement(session, ticker, yes_settlement_value="0.76")
        upsert_position(
            session,
            ticker=ticker,
            yes_contracts=1,
            avg_yes_price=Decimal("0.40"),
        )

        before = build_phase3aa_r3_residual_audit(session)
        calculate_and_store_pnl(session)
        after_doctor = build_paper_settlement_reconciliation(session)
        after = build_phase3aa_r3_residual_audit(session)

    assert before["summary"]["residual_rows"] == 1
    assert before["rows"][0]["classification"] == "SCALAR_READY_FOR_REALIZER"
    assert before["summary"]["safe_to_run_phase3aa_realize"] is True
    assert after_doctor["summary"]["eligible_to_settle_now"] == 0
    assert after_doctor["rows"][0]["reason"] == "ALREADY_REALIZED"
    assert after["summary"]["residue_cleared"] is True


def test_phase3aa_r3_classifies_missing_position_residue(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KX3AAR3-NOPOS"
    with session_factory() as session:
        _seed_order_market_settlement(session, ticker, yes_settlement_value="1")

        payload = build_phase3aa_r3_residual_audit(session)

    assert payload["summary"]["residual_rows"] == 1
    assert payload["rows"][0]["classification"] == "MISSING_PAPER_POSITION"
    assert payload["summary"]["missing_position_rows"] == 1
    assert payload["summary"]["safe_to_run_phase3aa_realize"] is False


def test_phase3aa_r3_writer_and_cli_help(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        artifacts = write_phase3aa_r3_residual_audit_report(
            session,
            output_dir=Path(tmp_path) / "phase3aa_r3",
        )

    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.rows_path.exists()
    assert "Residual Exact Settlement" in artifacts.markdown_path.read_text(encoding="utf-8")

    result = CliRunner().invoke(app, ["phase3aa-r3-residual-settlement-audit", "--help"])
    assert result.exit_code == 0
    assert "phase3aa-r3-residual-settlement-audit" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3aa_r3.db'}")
    return get_session_factory(engine)


def _seed_order_market_settlement(
    session,
    ticker: str,
    *,
    yes_settlement_value: str,
) -> None:
    session.add(
        PaperOrder(
            ticker=ticker,
            forecast_id=None,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            model_name="market_implied_v1",
            side=BUY_YES,
            probability="0.60",
            market_price="0.40",
            limit_price="0.40",
            edge="0.20",
            quantity=1,
            status=ORDER_FILLED,
            reason="phase3aa-r3 test",
            raw_decision_json="{}",
        )
    )
    upsert_market(
        session,
        {
            "ticker": ticker,
            "status": "settled",
            "title": "Phase 3AA-R3 exact settlement",
        },
    )
    upsert_settlement(
        session,
        {
            "ticker": ticker,
            "result": "scalar" if yes_settlement_value not in {"0", "1"} else None,
            "yes_settlement_value": yes_settlement_value,
            "settlement_ts": "2026-06-24T12:00:00Z",
        },
    )
