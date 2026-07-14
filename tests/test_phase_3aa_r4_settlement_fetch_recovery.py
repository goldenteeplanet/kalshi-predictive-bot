from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.phase3aa_r4 import (
    build_phase3aa_r4_settlement_fetch_recovery,
    write_phase3aa_r4_settlement_fetch_recovery_report,
)


def test_phase3aa_r4_groups_fetch_errors_and_blocks_sibling_settlement(tmp_path) -> None:
    reports_dir = _write_reports(tmp_path)

    payload = build_phase3aa_r4_settlement_fetch_recovery(reports_dir=reports_dir)

    assert payload["summary"]["fetch_error_rows"] == 2
    assert payload["summary"]["exact_settlement_rows_now_realizable"] == 0
    assert payload["safety"]["sibling_resolution_allowed"] is False
    assert payload["freshness_reconciliation"]["paper_sibling_different_contract_leg"] == 2
    group = payload["fetch_error_groups"][0]
    assert group["error_type"] == "HTTP_404_NOT_FOUND"
    assert group["http_status"] == 404
    assert group["retryable"] is False
    assert group["exact_ticker_identity_confidence"] == "EXACT_TICKER_REQUESTED_NO_RESPONSE"
    assert all(row["safe_to_realize"] is False for row in payload["diagnostic_rows"])


def test_phase3aa_r4_marks_closed_source_rows_diagnostic_only_after_r3_clear(
    tmp_path,
) -> None:
    reports_dir = _write_reports(tmp_path)

    payload = build_phase3aa_r4_settlement_fetch_recovery(reports_dir=reports_dir)

    assert payload["summary"]["source_closed_without_outcome_rows"] == 1
    assert payload["summary"]["source_settled_without_usable_outcome_rows"] == 0
    group = payload["source_closed_without_outcome_groups"][0]
    assert group["source_status"] == "closed"
    assert group["outcome_shape"] == "MISSING_OUTCOME"
    assert group["phase3aa_r3_already_proved_non_actionable"] is True
    assert "settlement_value" in group["missing_outcome_fields"]


def test_phase3aa_r4_detects_stale_placeholder_realization_prompt(tmp_path) -> None:
    reports_dir = _write_reports(tmp_path)

    payload = build_phase3aa_r4_settlement_fetch_recovery(reports_dir=reports_dir)

    freshness = payload["freshness_reconciliation"]
    assert freshness["realization_cleared_by_fresher_reports"] is True
    assert freshness["stale_realization_prompt_detected"] is True
    assert payload["summary"]["stale_realization_prompt_fixed_by_r4_logic"] is True


def test_phase3aa_r4_writer_and_cli_help(tmp_path) -> None:
    reports_dir = _write_reports(tmp_path)
    output_dir = tmp_path / "phase3aa_r4"

    artifacts = write_phase3aa_r4_settlement_fetch_recovery_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
    )
    result = CliRunner().invoke(app, ["phase3aa-r4-settlement-fetch-recovery", "--help"])

    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.rows_path.exists()
    assert "Exact Settlement Fetch Recovery" in artifacts.markdown_path.read_text(
        encoding="utf-8"
    )
    assert result.exit_code == 0
    assert "phase3aa-r4-settlement-fetch-recovery" in result.output


def _write_reports(tmp_path: Path) -> Path:
    reports_dir = tmp_path / "reports"
    _write_json(
        reports_dir / "phase3aa" / "phase3aa_outcome_realizer.json",
        {
            "generated_at": "2026-06-28T01:38:52+00:00",
            "eligible_after_realize": 0,
            "eta_schedule": {
                "summary": {
                    "active_unsettled": 130,
                    "due_or_overdue": 117,
                    "eligible_exact_settlements": 0,
                }
            },
        },
    )
    _write_json(
        reports_dir / "phase3aa_r2" / "phase3aa_r2_exact_settlement_harvest.json",
        {
            "generated_at": "2026-06-28T00:00:00+00:00",
            "summary": {
                "exact_settlements_written": 20,
                "eligible_exact_settlements_after": 20,
                "fetch_errors": 2,
                "source_closed_without_outcome": 1,
                "source_settled_without_usable_outcome": 0,
            },
        },
    )
    _write_json(
        reports_dir / "phase3aa_r2" / "phase3aa_r2_exact_settlement_harvest_rows.json",
        [
            {
                "ticker": "KXMVECROSSCATEGORY-S2026001-ABC",
                "source_fetch_status": "FETCH_ERROR",
                "error": (
                    "Kalshi GET /markets/KXMVECROSSCATEGORY-S2026001-ABC "
                    'returned HTTP 404: {"error":{"code":"not_found"}}'
                ),
                "before_close_time_buckets": ["overdue"],
                "before_market_statuses": ["active"],
            },
            {
                "ticker": "KXMVECROSSCATEGORY-S2026002-DEF",
                "source_fetch_status": "FETCH_ERROR",
                "error": (
                    "Kalshi GET /markets/KXMVECROSSCATEGORY-S2026002-DEF "
                    'returned HTTP 404: {"error":{"code":"not_found"}}'
                ),
                "before_close_time_buckets": ["overdue"],
                "before_market_statuses": ["active"],
            },
            {
                "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-S2026003-GHI",
                "source_fetch_status": "SOURCE_CLOSED_WITHOUT_OUTCOME",
                "source_status": "closed",
                "source_result": None,
                "source_settlement_value_dollars": None,
                "source_yes_settlement_value": None,
                "source_settlement_ts": None,
            },
        ],
    )
    _write_json(
        reports_dir / "phase3aa_r3" / "phase3aa_r3_residual_settlement_audit.json",
        {
            "generated_at": "2026-06-28T01:39:00+00:00",
            "summary": {
                "residue_cleared": True,
                "residual_rows": 0,
                "eligible_to_settle_now": 0,
            },
        },
    )
    _write_json(
        reports_dir
        / "paper_settlement_reconciliation"
        / "paper_settlement_reconciliation.json",
        {
            "generated_at": "2026-06-28T01:39:05+00:00",
            "summary": {
                "eligible_to_settle_now": 0,
                "missing_exact_settlement": 130,
                "sibling_different_contract_leg": 2,
            },
        },
    )
    _write_json(
        reports_dir / "phase3ah_sports" / "phase3ah_sports_placeholder_watch.json",
        {
            "generated_at": "2026-06-28T01:37:27+00:00",
            "summary": {
                "settlement_exact_settlements_written": 20,
                "settlement_eligible_after_harvest": 20,
            },
            "settlement_watch": {"status": "EXACT_SETTLEMENTS_READY_TO_REALIZE"},
            "recommended_next_action": (
                "Realize the newly harvested exact paper settlements first, then rerun "
                "this watch report."
            ),
        },
    )
    return reports_dir


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
