import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.phase3aa_r5 import (
    build_phase3aa_r5_closed_market_outcome_capture,
    write_phase3aa_r5_closed_market_outcome_capture_report,
)


def test_phase3aa_r5_captures_closed_market_fields_without_enabling_pnl(tmp_path) -> None:
    reports_dir = _write_r2_rows(tmp_path)
    session_factory = _session_factory(tmp_path)
    ticker = "KXMVESPORTSMULTIGAMEEXTENDED-S2026003-GHI"
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": ticker,
                "status": "closed",
                "result": "",
                "expiration_value": "",
                "market_type": "binary",
                "custom_strike": {
                    "associated_events": [{"ticker": "KXEVENT"}],
                    "associated_markets": [{"ticker": "KXCHILD"}],
                    "associated_market_sides": [{"ticker": "KXCHILD", "side": "yes"}],
                },
                "mve_selected_legs": [{"ticker": "KXCHILD"}],
                "title": "Closed exact source market",
            },
        )

        payload = build_phase3aa_r5_closed_market_outcome_capture(
            session,
            reports_dir=reports_dir,
        )

    row = payload["rows"][0]
    assert payload["summary"]["rows_reviewed"] == 1
    assert payload["summary"]["closed_without_outcome_rows"] == 1
    assert payload["summary"]["safe_to_settle_rows"] == 0
    assert payload["summary"]["paper_pnl_realization_allowed_rows"] == 0
    assert row["classification"] == "SOURCE_CLOSED_WITHOUT_OUTCOME"
    assert row["associated_markets_count"] == 1
    assert row["mve_selected_legs_count"] == 1
    assert row["safe_to_write_exact_settlement_from_current_parser"] is False
    assert row["paper_pnl_realization_allowed"] is False


def test_phase3aa_r5_flags_known_outcome_field_as_parser_candidate(tmp_path) -> None:
    reports_dir = _write_r2_rows(tmp_path, status="settled")
    session_factory = _session_factory(tmp_path)
    ticker = "KXMVESPORTSMULTIGAMEEXTENDED-S2026003-GHI"
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": ticker,
                "status": "settled",
                "settlement_value": "1",
                "settlement_ts": "2026-06-28T00:00:00+00:00",
            },
        )

        payload = build_phase3aa_r5_closed_market_outcome_capture(
            session,
            reports_dir=reports_dir,
        )

    assert payload["summary"]["usable_outcome_candidate_rows"] == 1
    assert payload["rows"][0]["classification"] == "EXACT_OUTCOME_FIELD_USABLE"
    assert payload["rows"][0]["safe_to_write_exact_settlement_from_current_parser"] is True
    assert payload["rows"][0]["paper_pnl_realization_allowed"] is False


def test_phase3aa_r5_writer_and_cli_help(tmp_path) -> None:
    reports_dir = _write_r2_rows(tmp_path)
    session_factory = _session_factory(tmp_path)
    output_dir = tmp_path / "phase3aa_r5"
    with session_factory() as session:
        artifacts = write_phase3aa_r5_closed_market_outcome_capture_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
        )
    result = CliRunner().invoke(app, ["phase3aa-r5-closed-market-outcome-capture", "--help"])

    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.rows_path.exists()
    assert "Closed Market Outcome Capture" in artifacts.markdown_path.read_text(
        encoding="utf-8"
    )
    assert result.exit_code == 0
    assert "phase3aa-r5-closed-market-outcome-capture" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3aa_r5.db'}")
    return get_session_factory(engine)


def _write_r2_rows(tmp_path: Path, *, status: str = "closed") -> Path:
    reports_dir = tmp_path / "reports"
    row_status = (
        "SOURCE_CLOSED_WITHOUT_OUTCOME"
        if status == "closed"
        else "SOURCE_SETTLED_WITHOUT_USABLE_OUTCOME"
    )
    rows = [
        {
            "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-S2026003-GHI",
            "source_fetch_status": row_status,
            "source_status": status,
            "source_result": None,
            "source_settlement_value_dollars": None,
            "source_settlement_value": None,
            "source_yes_settlement_value": None,
            "source_expiration_value": None,
        }
    ]
    rows_path = reports_dir / "phase3aa_r2" / "phase3aa_r2_exact_settlement_harvest_rows.json"
    report_path = reports_dir / "phase3aa_r2" / "phase3aa_r2_exact_settlement_harvest.json"
    rows_path.parent.mkdir(parents=True, exist_ok=True)
    rows_path.write_text(json.dumps(rows), encoding="utf-8")
    report_path.write_text(json.dumps({"rows": rows, "summary": {}}), encoding="utf-8")
    return reports_dir
