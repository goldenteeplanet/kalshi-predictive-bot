import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.phase3ax import _source_evidence_gap_status
from kalshi_predictor.phase3bb_r5_flightaware import (
    build_phase3bb_r5_flightaware_date_stable_evidence,
    write_phase3bb_r5_flightaware_date_stable_evidence_report,
)


def test_phase3bb_r5_documents_missing_date_stable_flightaware_evidence(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    evidence_dir = Path(tmp_path) / "evidence"
    _write_flightaware_fixture(reports_dir, evidence_dir)

    payload = build_phase3bb_r5_flightaware_date_stable_evidence(
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
        registered_commands={
            "phase3bb-r5-flightaware-date-stable-evidence",
            "phase3bb-r4-flightaware-review-link-gate",
            "phase3bb-r2-general-source-availability",
            "phase3ax-gap-analysis",
        },
    )

    summary = payload["summary"]
    rows = {row["candidate_id"]: row for row in payload["candidate_evidence_rows"]}
    assert summary["date_stable_evidence_status"] == "NOT_FOUND"
    assert summary["first_hard_blocker"] == (
        "OFFICIAL_FLIGHTAWARE_HISTORICAL_AGGREGATE_UNAVAILABLE"
    )
    assert summary["accepted_date_stable_evidence_rows"] == 0
    assert summary["rejected_kalshi_outcome_page_rows"] >= 1
    assert summary["rejected_relative_live_page_rows"] >= 1
    assert summary["access_required_rows"] == 1
    assert summary["link_safe_rows"] == 0
    assert summary["forecast_safe_rows"] == 0
    assert summary["network_fetches_performed"] is False
    assert rows["canonical_local_record_1"]["rejection_code"] == (
        "KALSHI_OUTCOME_NOT_OFFICIAL"
    )
    assert rows["flightaware_latest_public_recent_snapshot"]["rejection_code"] == (
        "RELATIVE_OR_MUTABLE_PAGE"
    )
    assert payload["next_codex_task"]["task_phase_name"] == (
        "Phase 3AH-R3 Sports Provenance Repair"
    )
    assert payload["paper_trade_creation"] is False
    assert payload["live_or_demo_execution"] is False
    assert payload["fabricated_evidence"] is False


def test_phase3bb_r5_can_accept_verified_official_date_stable_evidence(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    evidence_dir = Path(tmp_path) / "evidence"
    _write_flightaware_fixture(
        reports_dir,
        evidence_dir,
        official_verified_record=True,
    )

    payload = build_phase3bb_r5_flightaware_date_stable_evidence(
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
        registered_commands={"phase3bb-r5-flightaware-date-stable-evidence"},
    )

    summary = payload["summary"]
    assert summary["date_stable_evidence_status"] == "FOUND_READY_FOR_REVIEW"
    assert summary["accepted_date_stable_evidence_rows"] == 1
    assert summary["first_hard_blocker"] == "NONE"
    assert payload["accepted_date_stable_evidence_rows"][0]["source_url"].startswith(
        "https://www.flightaware.com/commercial/aeroapi/history/"
    )
    assert payload["next_codex_task"]["task_phase_name"] == (
        "Phase 3BB-R6 FlightAware Manual Review Approval"
    )


def test_phase3bb_r5_next_actions_only_registered(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    evidence_dir = Path(tmp_path) / "evidence"
    _write_flightaware_fixture(reports_dir, evidence_dir)

    artifacts = write_phase3bb_r5_flightaware_date_stable_evidence_report(
        output_dir=reports_dir / "phase3bb_r5_flightaware",
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
        registered_commands={"phase3bb-r5-flightaware-date-stable-evidence"},
    )

    next_actions = artifacts.next_actions_path.read_text(encoding="utf-8")
    audit = json.loads(artifacts.command_audit_path.read_text(encoding="utf-8"))
    assert "phase3bb-r5-flightaware-date-stable-evidence" in next_actions
    assert "phase3bb-r4-flightaware-review-link-gate" not in next_actions
    assert "phase3bb-r2-general-source-availability" not in next_actions
    assert audit["next_actions_reference_only_registered_commands"] is True
    assert "phase3bb-r4-flightaware-review-link-gate" in audit["missing_command_names"]
    assert artifacts.executive_summary_path.exists()
    assert artifacts.next_codex_task_path.exists()
    assert artifacts.evidence_json_path.exists()
    assert artifacts.candidate_rows_path.exists()
    assert artifacts.manifest_path.exists()


def test_phase3ax_uses_phase3bb_r5_followup_task(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    evidence_dir = Path(tmp_path) / "evidence"
    _write_flightaware_fixture(reports_dir, evidence_dir)
    write_phase3bb_r5_flightaware_date_stable_evidence_report(
        output_dir=reports_dir / "phase3bb_r5_flightaware",
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
        registered_commands={"phase3bb-r5-flightaware-date-stable-evidence"},
    )

    source_status = _source_evidence_gap_status(reports_dir)

    assert source_status["next_codex_task_phase_name"] == (
        "Phase 3AH-R3 Sports Provenance Repair"
    )
    assert source_status["flightaware_status"] == "NOT_FOUND"
    assert source_status["first_hard_blocker"] == (
        "OFFICIAL_FLIGHTAWARE_HISTORICAL_AGGREGATE_UNAVAILABLE"
    )


def test_phase3bb_r5_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r5-flightaware-date-stable-evidence", "--help"],
    )

    assert result.exit_code == 0
    assert "Usage" in result.output


def _write_flightaware_fixture(
    reports_dir: Path,
    evidence_dir: Path,
    *,
    official_verified_record: bool = False,
) -> None:
    r2_dir = reports_dir / "phase3bb_r2_sources"
    r4_dir = reports_dir / "phase3bb_r4_flightaware"
    r2_dir.mkdir(parents=True, exist_ok=True)
    r4_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    record = (
        {
            "region": "United States",
            "metric": "total_flight_cancellations",
            "period_start": "June 27, 2026",
            "period_end": "July 3, 2026",
            "cancellation_count": 1247,
            "source_name": "FlightAware AeroAPI historical aggregate",
            "source_url": "https://www.flightaware.com/commercial/aeroapi/history/2026-07-03",
            "underlying_source_name": "FlightAware",
            "underlying_source_url": "https://www.flightaware.com/commercial/aeroapi/",
            "verification_status": "verified",
        }
        if official_verified_record
        else {
            "region": "United States",
            "metric": "total_flight_cancellations",
            "period_start": "June 27, 2026",
            "period_end": "July 3, 2026",
            "cancellation_count": 1247,
            "source_name": "Kalshi outcome page citing FlightAware",
            "source_url": "https://kalshi.com/markets/kxusflycan",
            "underlying_source_name": "FlightAware",
            "underlying_source_url": "https://www.flightaware.com/live/cancelled/week",
        }
    )
    _write_json(
        evidence_dir / "transportation_flight_cancellation_source.json",
        {"records": [record]},
    )
    _write_json(
        r2_dir / "flightaware_cancellation_date_resolution.json",
        {
            "exact_july_3_report_found": official_verified_record,
            "observed_value_filled": official_verified_record,
            "flightaware_signup_assessment": {
                (
                    "signup_or_paid_product_likely_required_for_audited_date_stable_"
                    "historical_aggregate"
                ): True
            },
            "latest_public_recent_snapshot": {
                "accepted_as_exact_july_3_evidence": False,
                "url": "https://www.flightaware.com/live/cancelled/minus2days",
                "us_scope_cancellations": "932",
                "us_scope_label": "Friday",
            },
            "sources_checked": [
                {
                    "name": "FlightAware exact-date-looking URL probes",
                    "urls": ["https://www.flightaware.com/live/cancelled/2026-07-03"],
                    "result": "Returned today page.",
                },
                {
                    "name": "FlightAware Rapid Reports and AeroAPI product pages",
                    "urls": [
                        "https://www.flightaware.com/commercial/rapidreports/",
                        "https://www.flightaware.com/commercial/aeroapi/",
                    ],
                    "result": "Historical data requires official product access.",
                },
            ],
        },
    )
    _write_json(
        r4_dir / "flightaware_review_link_gate.json",
        {
            "summary": {
                "affected_rows": 9,
                "observed_value": "1247",
                "link_safe_rows": 0,
                "forecast_safe_rows": 0,
            },
            "flightaware_evidence": {
                "source_name": "Kalshi outcome page citing FlightAware",
                "source_url": "https://kalshi.com/markets/kxusflycan",
                "underlying_source_name": "FlightAware",
                "underlying_source_url": "https://www.flightaware.com/live/cancelled/week",
                "observed_value": "1247",
                "target": {"target_date": "July 3, 2026"},
            },
        },
    )


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
