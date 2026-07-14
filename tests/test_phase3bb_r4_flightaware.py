import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.phase3ax import _source_evidence_gap_status
from kalshi_predictor.phase3bb_r4_flightaware import (
    build_phase3bb_r4_flightaware_review_link_gate,
    write_phase3bb_r4_flightaware_review_link_gate_report,
)


def test_phase3bb_r4_blocks_flightaware_without_date_stable_source(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    _write_flightaware_fixture(reports_dir)

    payload = build_phase3bb_r4_flightaware_review_link_gate(
        reports_dir=reports_dir,
        registered_commands={
            "phase3bb-r4-flightaware-review-link-gate",
            "phase3bb-r3-source-evidence-activation",
            "phase3bb-r2-general-source-evidence",
            "phase3ax-gap-analysis",
        },
    )

    summary = payload["summary"]
    checks = {row["gate"]: row for row in payload["flightaware_review_checks"]}
    assert summary["review_gate_status"] == "BLOCKED"
    assert summary["first_hard_blocker"] == "DATE_STABLE_FLIGHTAWARE_SOURCE_MISSING"
    assert summary["evidence_ready_rows"] == 9
    assert summary["observed_value"] == "1247"
    assert summary["date_stable_evidence_available"] is False
    assert summary["link_safe_rows"] == 0
    assert summary["forecast_safe_rows"] == 0
    assert summary["promoted_to_link_safe_rows"] == 0
    assert summary["promoted_to_forecast_safe_rows"] == 0
    assert checks["exact_date_stable_source"]["status"] == "FAIL"
    assert checks["entity_scope_mapping"]["status"] == "PASS_REVIEW_ONLY"
    assert checks["time_window_mapping"]["status"] == "PASS_REVIEW_ONLY"
    assert checks["no_leakage"]["status"] == "PASS_REVIEW_ONLY"
    assert checks["review_approval"]["status"] == "FAIL"
    assert payload["next_codex_task"]["task_phase_name"] == (
        "Phase 3BB-R5 FlightAware Date-Stable Evidence Capture"
    )
    assert payload["paper_trade_creation"] is False
    assert payload["live_or_demo_execution"] is False
    assert payload["fabricated_evidence"] is False


def test_phase3bb_r4_next_actions_only_registered(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    _write_flightaware_fixture(reports_dir)

    artifacts = write_phase3bb_r4_flightaware_review_link_gate_report(
        output_dir=reports_dir / "phase3bb_r4_flightaware",
        reports_dir=reports_dir,
        registered_commands={"phase3bb-r4-flightaware-review-link-gate"},
    )

    next_actions = artifacts.next_actions_path.read_text(encoding="utf-8")
    audit = json.loads(artifacts.command_audit_path.read_text(encoding="utf-8"))
    assert "phase3bb-r4-flightaware-review-link-gate" in next_actions
    assert "phase3bb-r3-source-evidence-activation" not in next_actions
    assert "phase3bb-r2-general-source-evidence" not in next_actions
    assert audit["next_actions_reference_only_registered_commands"] is True
    assert "phase3bb-r3-source-evidence-activation" in audit["missing_command_names"]
    assert artifacts.executive_summary_path.exists()
    assert artifacts.next_codex_task_path.exists()
    assert artifacts.gate_json_path.exists()
    assert artifacts.review_checks_path.exists()
    assert artifacts.manifest_path.exists()


def test_phase3ax_uses_phase3bb_r4_followup_task(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    _write_flightaware_fixture(reports_dir)
    write_phase3bb_r4_flightaware_review_link_gate_report(
        output_dir=reports_dir / "phase3bb_r4_flightaware",
        reports_dir=reports_dir,
        registered_commands={"phase3bb-r4-flightaware-review-link-gate"},
    )

    source_status = _source_evidence_gap_status(reports_dir)

    assert source_status["next_codex_task_phase_name"] == (
        "Phase 3BB-R5 FlightAware Date-Stable Evidence Capture"
    )
    assert source_status["flightaware_status"] == "BLOCKED"
    assert source_status["first_hard_blocker"] == (
        "DATE_STABLE_FLIGHTAWARE_SOURCE_MISSING"
    )


def test_phase3bb_r4_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r4-flightaware-review-link-gate", "--help"])

    assert result.exit_code == 0
    assert "Usage" in result.output


def _write_flightaware_fixture(reports_dir: Path) -> None:
    r2_dir = reports_dir / "phase3bb_r2_sources"
    r3_dir = reports_dir / "phase3bb_r3_source_activation"
    r2_dir.mkdir(parents=True, exist_ok=True)
    r3_dir.mkdir(parents=True, exist_ok=True)
    tickers = [
        f"KXUSFLYCAN-26JUL03-T{threshold}"
        for threshold in (
            "2000",
            "2500",
            "3000",
            "3500",
            "4000",
            "4500",
            "5000",
            "5500",
            "6000",
        )
    ]
    evidence_rows = [
        {
            "source_adapter_key": "transportation_flight_cancellation_source",
            "ticker": ticker,
            "evidence_status": "EXACT_EVIDENCE_READY_FOR_REVIEW",
            "safe_to_link": False,
            "safe_to_forecast": False,
            "parsed_fields": {
                "region": "United States",
                "time_window": "July 3, 2026",
            },
            "matched_evidence": {
                "cancellation_count": 1247,
                "period_start": "June 27, 2026",
                "period_end": "July 3, 2026",
                "source_name": "Kalshi outcome page citing FlightAware",
                "source_url": "https://kalshi.com/markets/kxusflycan",
                "underlying_source_name": "FlightAware",
                "underlying_source_url": "https://www.flightaware.com/live/cancelled/week",
            },
        }
        for ticker in tickers
    ]
    _write_json(
        r2_dir / "phase3bb_r2_general_source_evidence.json",
        {
            "summary": {
                "exact_evidence_ready_rows": 9,
                "safe_to_link_rows": 0,
                "safe_to_forecast_rows": 0,
            },
            "evidence_rows": evidence_rows,
        },
    )
    _write_json(
        r2_dir / "phase3bb_r2_general_source_availability.json",
        {
            "summary": {"safe_to_link_rows": 0, "safe_to_forecast_rows": 0},
            "availability_rows": [
                {
                    "source_adapter_key": "transportation_flight_cancellation_source",
                    "availability_status": "SOURCE_VALUE_AVAILABLE_FOR_REVIEW",
                    "affected_diagnostic_rows": 9,
                    "affected_tickers": tickers,
                    "observed_value": "1247",
                    "source_name": "Kalshi outcome page citing FlightAware",
                    "source_url": "https://kalshi.com/markets/kxusflycan",
                    "target_observation": "July 3, 2026",
                    "target_publication": "FlightAware weekly cancellation outcome",
                }
            ],
        },
    )
    _write_json(
        r2_dir / "flightaware_cancellation_date_resolution.json",
        {
            "exact_july_3_report_found": False,
            "observed_value_filled": False,
            "latest_public_recent_snapshot": {
                "accepted_as_exact_july_3_evidence": False,
                "url": "https://www.flightaware.com/live/cancelled/minus2days",
            },
            "target": {
                "region": "United States",
                "target_date": "July 3, 2026",
                "tickers": tickers,
            },
        },
    )
    _write_json(
        r3_dir / "source_evidence_activation.json",
        {
            "source_activation_decisions": [
                {
                    "source_adapter_key": "transportation_flight_cancellation_source",
                    "source_name": "FlightAware",
                    "affected_rows": 9,
                    "readiness_state": "READY_FOR_REVIEW",
                    "review_approved": False,
                }
            ]
        },
    )


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
