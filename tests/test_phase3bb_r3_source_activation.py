import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.phase3bb_r3_activation import (
    build_phase3bb_r3_source_evidence_activation,
    write_phase3bb_r3_source_evidence_activation_report,
)


def test_phase3bb_r3_source_activation_blocks_unapproved_sources(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    _write_source_fixture(reports_dir)

    payload = build_phase3bb_r3_source_evidence_activation(
        reports_dir=reports_dir,
        registered_commands={
            "phase3bb-r3-source-evidence-activation",
            "phase3bb-r2-general-source-evidence",
            "phase3bb-r2-general-source-availability",
            "phase3ax-gap-analysis",
        },
    )

    summary = payload["summary"]
    decisions = {
        row["source_name"]: row for row in payload["source_activation_decisions"]
    }
    assert summary["activation_readiness"] == "NOT_READY"
    assert summary["activation_outcome"] == "NO_ACTIVATION_UNSAFE_OR_UNAPPROVED"
    assert summary["evidence_ready_rows"] == 9
    assert summary["link_safe_rows"] == 0
    assert summary["forecast_safe_rows"] == 0
    assert summary["first_hard_blocker"] == "SOURCE_DATE_MISMATCH_BLOCKER"
    assert summary["promoted_to_link_safe_rows"] == 0
    assert summary["promoted_to_forecast_safe_rows"] == 0
    assert decisions["USDA"]["first_blocker"] == "SOURCE_DATE_MISMATCH_BLOCKER"
    assert decisions["USDA"]["link_safe_decision"] == "BLOCK"
    assert decisions["Cushman"]["first_blocker"] == "PROPRIETARY_REVIEW_REQUIRED"
    assert decisions["FlightAware"]["first_blocker"] == "READY_FOR_REVIEW_NOT_LINK_SAFE"
    assert decisions["FlightAware"]["evidence_ready_rows"] == 9
    assert all(row["activation_allowed"] is False for row in decisions.values())
    assert payload["paper_trade_creation"] is False
    assert payload["live_or_demo_execution"] is False
    assert payload["thresholds_lowered"] is False
    assert payload["fabricated_evidence"] is False


def test_phase3bb_r3_source_activation_next_actions_only_registered(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    _write_source_fixture(reports_dir)

    artifacts = write_phase3bb_r3_source_evidence_activation_report(
        output_dir=reports_dir / "phase3bb_r3_source_activation",
        reports_dir=reports_dir,
        registered_commands={"phase3bb-r3-source-evidence-activation"},
    )

    next_actions = artifacts.next_actions_path.read_text(encoding="utf-8")
    audit = json.loads(artifacts.command_audit_path.read_text(encoding="utf-8"))
    assert "phase3bb-r3-source-evidence-activation" in next_actions
    assert "phase3bb-r2-general-source-evidence" not in next_actions
    assert "phase3bb-r2-general-source-availability" not in next_actions
    assert audit["next_actions_reference_only_registered_commands"] is True
    assert "phase3bb-r2-general-source-evidence" in audit["missing_command_names"]
    assert artifacts.executive_summary_path.exists()
    assert artifacts.next_codex_task_path.exists()
    assert artifacts.activation_json_path.exists()
    assert artifacts.activation_decisions_path.exists()
    assert artifacts.manifest_path.exists()


def test_phase3bb_r3_source_activation_selects_flightaware_next_codex_task(
    tmp_path,
) -> None:
    reports_dir = Path(tmp_path) / "reports"
    _write_source_fixture(reports_dir)

    payload = build_phase3bb_r3_source_evidence_activation(
        reports_dir=reports_dir,
        registered_commands={"phase3bb-r3-source-evidence-activation"},
    )

    assert payload["next_codex_task"]["task_phase_name"] == (
        "Phase 3BB-R4 FlightAware Review-to-Link Gate"
    )
    assert "link/forecast unsafe" in payload["next_codex_task"]["reason"]


def test_phase3bb_r3_source_activation_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r3-source-evidence-activation", "--help"])

    assert result.exit_code == 0
    assert "Usage" in result.output


def _write_source_fixture(reports_dir: Path) -> None:
    evidence_dir = reports_dir / "phase3bb_r2_sources"
    phase3an_dir = reports_dir / "phase3an"
    phase3ax_dir = reports_dir / "phase3ax"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    phase3an_dir.mkdir(parents=True, exist_ok=True)
    phase3ax_dir.mkdir(parents=True, exist_ok=True)

    evidence_rows = []
    for threshold in ("0.80", "1.00", "1.20", "1.40", "1.60", "1.80", "2.00"):
        evidence_rows.append(
            {
                "source_adapter_key": "commodity_advertised_price_source",
                "ticker": f"KXAMSAVO-26JUL03-T{threshold}",
                "evidence_status": "SOURCE_EVIDENCE_UNAVAILABLE",
                "safe_to_link": False,
                "safe_to_forecast": False,
                "matched_evidence": {
                    "source_name": "USDA Agricultural Marketing Service / USDA Market News",
                    "source_url": "https://www.ams.usda.gov/mnreports/fvwretail.pdf",
                    "price_usd_each": None,
                },
                "source_file": "data/general_source_evidence/commodity.json",
            }
        )
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
    ):
        evidence_rows.append(
            {
                "source_adapter_key": "transportation_flight_cancellation_source",
                "ticker": f"KXUSFLYCAN-26JUL03-T{threshold}",
                "evidence_status": "EXACT_EVIDENCE_READY_FOR_REVIEW",
                "safe_to_link": False,
                "safe_to_forecast": False,
                "matched_evidence": {
                    "source_name": "Kalshi outcome page citing FlightAware",
                    "source_url": "https://kalshi.com/markets/kxusflycan",
                    "cancellation_count": 1247,
                },
                "source_file": "data/general_source_evidence/flightaware.json",
            }
        )
    for threshold in (
        "45.0",
        "47.5",
        "50.0",
        "52.5",
        "55.0",
        "57.5",
        "60.0",
        "62.5",
        "65.0",
    ):
        evidence_rows.append(
            {
                "source_adapter_key": "infrastructure_data_center_capacity_source",
                "ticker": f"KXUSDCCAPACITY-27MAR05-T{threshold}",
                "evidence_status": "SOURCE_EVIDENCE_UNAVAILABLE",
                "safe_to_link": False,
                "safe_to_forecast": False,
                "matched_evidence": {
                    "source_name": "Cushman & Wakefield Americas Data Center Update",
                    "source_url": "https://www.cushmanwakefield.com/en/insights/americas-data-center-update",
                    "capacity_gw": None,
                },
                "source_file": "data/general_source_evidence/cushman.json",
            }
        )

    _write_json(
        evidence_dir / "phase3bb_r2_general_source_evidence.json",
        {
            "summary": {
                "exact_evidence_ready_rows": 9,
                "safe_to_link_rows": 0,
                "safe_to_forecast_rows": 0,
                "source_evidence_unavailable_rows": 16,
            },
            "evidence_rows": evidence_rows,
        },
    )
    _write_json(
        evidence_dir / "phase3bb_r2_general_source_availability.json",
        {
            "summary": {
                "source_value_available_rows": 1,
                "safe_to_link_rows": 0,
                "safe_to_forecast_rows": 0,
            },
            "availability_rows": [
                {
                    "source_adapter_key": "commodity_advertised_price_source",
                    "availability_status": "PENDING_SOURCE_PUBLICATION",
                    "affected_diagnostic_rows": 7,
                    "affected_tickers": [
                        row["ticker"]
                        for row in evidence_rows
                        if row["source_adapter_key"] == "commodity_advertised_price_source"
                    ],
                    "block_reason": "Waiting for USDA July 3, 2026 FVWRETAIL.",
                    "source_name": "USDA Agricultural Marketing Service / USDA Market News",
                    "source_url": "https://www.ams.usda.gov/mnreports/fvwretail.pdf",
                    "target_publication": "USDA July 3, 2026 FVWRETAIL",
                },
                {
                    "source_adapter_key": "transportation_flight_cancellation_source",
                    "availability_status": "SOURCE_VALUE_AVAILABLE_FOR_REVIEW",
                    "affected_diagnostic_rows": 9,
                    "affected_tickers": [
                        row["ticker"]
                        for row in evidence_rows
                        if row["source_adapter_key"]
                        == "transportation_flight_cancellation_source"
                    ],
                    "observed_value": "1247",
                    "block_reason": "The required source value is present in local evidence.",
                    "source_name": "Kalshi outcome page citing FlightAware",
                    "source_url": "https://kalshi.com/markets/kxusflycan",
                    "target_publication": "FlightAware weekly cancellation outcome",
                },
                {
                    "source_adapter_key": "infrastructure_data_center_capacity_source",
                    "availability_status": "PENDING_SOURCE_PUBLICATION",
                    "affected_diagnostic_rows": 9,
                    "affected_tickers": [
                        row["ticker"]
                        for row in evidence_rows
                        if row["source_adapter_key"]
                        == "infrastructure_data_center_capacity_source"
                    ],
                    "block_reason": "Waiting for exact 2026 capacity_gw value.",
                    "source_name": "Cushman & Wakefield Americas Data Center Update",
                    "source_url": "https://www.cushmanwakefield.com/en/insights/americas-data-center-update",
                    "target_publication": "Cushman & Wakefield first H2 2026 update",
                },
            ],
        },
    )
    _write_json(
        evidence_dir / "source_readiness_matrix.json",
        {
            "data": [
                {
                    "source_name": "USDA",
                    "readiness_state": "CONFIGURED_NO_VALUES",
                    "current_blocker": "USDA values are currently unavailable.",
                },
                {
                    "source_name": "FlightAware",
                    "readiness_state": "READY_FOR_REVIEW",
                    "current_blocker": "Review approval tests have not passed.",
                },
                {
                    "source_name": "Cushman",
                    "readiness_state": "PROPRIETARY_SOURCE_REVIEW_REQUIRED",
                    "current_blocker": (
                        "Cushman values are unavailable and licensing review is required."
                    ),
                },
            ]
        },
    )
    _write_json(
        evidence_dir / "usda_fvwretail_date_resolution.json",
        {
            "status": "BLOCKED_NO_EXACT_JULY_3_REPORT_FOUND",
            "exact_july_3_report_found": False,
            "next_action": "Preserve needs_review until exact official July 3 evidence exists.",
        },
    )
    _write_json(
        evidence_dir / "flightaware_cancellation_date_resolution.json",
        {
            "status": "READY_FOR_REVIEW",
            "next_action": "Run report-only FlightAware ambiguity and freshness tests.",
        },
    )
    _write_json(
        phase3an_dir / "general_sources_status.json",
        {
            "summary": {
                "source_evidence_ready_rows": 9,
                "link_safe_rows": 0,
                "forecast_safe_rows": 0,
            }
        },
    )
    _write_json(
        phase3ax_dir / "source_evidence_gap_status.json",
        {
            "activation_readiness": "NOT_READY",
            "evidence_ready_rows": 9,
            "link_safe_rows": 0,
            "forecast_safe_rows": 0,
            "source_date_mismatch_blockers": True,
            "proprietary_review_blockers": True,
        },
    )


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
