from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.data.schema import Base
from kalshi_predictor.phase3bb_r5_usda import (
    build_phase3bb_r5_usda_source_activation,
    evaluate_usda_row,
    write_phase3bb_r5_usda_source_activation_report,
)


def test_phase3bb_r5_blocks_usda_date_mismatch_and_cushman(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    evidence_dir = Path(tmp_path) / "evidence"
    _write_blocked_fixture(reports_dir, evidence_dir)
    session = _session()

    payload = build_phase3bb_r5_usda_source_activation(
        session,
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
        command_args=["phase3bb-r5-usda-source-activation"],
    )

    summary = payload["summary"]
    usda_rows = payload["usda_rows"]
    assert summary["usda_inventory_rows"] == 7
    assert summary["usda_promoted_rows"] == 0
    assert summary["usda_blocked_rows"] == 7
    assert summary["first_hard_blocker"] == "SOURCE_DATE_MISMATCH_BLOCKER"
    assert summary["cushman_status"] == "PROPRIETARY_REVIEW_REQUIRED"
    assert all(row["promoted_source_evidence"] is False for row in usda_rows)
    assert usda_rows[0]["source_publication_date"] == "June 26, 2026"
    assert usda_rows[0]["effective_date"] == "July 3, 2026"
    assert usda_rows[0]["date_stable"] is False
    assert payload["safety_flags"]["uses_paid_or_proprietary_sources"] is False
    assert payload["safety_flags"]["db_writes_performed"] == 0


def test_phase3bb_r5_promotes_exact_date_stable_usda_feature_preview() -> None:
    row = evaluate_usda_row(
        market_ticker="KXAMSAVO-26JUL03-T1.20",
        evidence_row={
            "parsed_fields": {
                "time_window": "July 3, 2026",
                "threshold": "1.20",
                "direction": "above",
            },
            "evidence_status": "EXACT_EVIDENCE_READY_FOR_REVIEW",
        },
        matched_evidence={
            "as_of_date": "July 3, 2026",
            "source_publication_date": "July 3, 2026",
            "price_usd_each": "1.15",
            "metric": "weighted_average_advertised_price",
            "retrieved_at": "2026-07-05T04:15:42Z",
            "source_name": "USDA Agricultural Marketing Service / USDA Market News",
            "source_url": "https://www.ams.usda.gov/mnreports/fvwretail.pdf",
            "verification_status": "verified",
            "evidence_available": True,
        },
        usda_date_report={"exact_july_3_report_found": True},
    )

    assert row["first_blocker"] == "NONE"
    assert row["activation_decision"] == "PROMOTE_FEATURE_PREVIEW"
    assert row["promoted_source_evidence"] is True
    assert row["candidate_feature_row"] is True
    assert row["feature_value"] == "1.15"
    assert row["proposed_db_writes"] == 0


def test_phase3bb_r5_writes_requested_artifacts(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    evidence_dir = Path(tmp_path) / "evidence"
    _write_blocked_fixture(reports_dir, evidence_dir)
    session = _session()

    artifacts = write_phase3bb_r5_usda_source_activation_report(
        session,
        output_dir=reports_dir / "phase3bb_r5",
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
    )

    assert artifacts.executive_summary_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.usda_rows_csv_path.exists()
    assert artifacts.blocked_rows_csv_path.exists()
    assert artifacts.manifest_path.exists()
    assert "SOURCE_DATE_MISMATCH_BLOCKER" in artifacts.blocked_rows_csv_path.read_text(
        encoding="utf-8"
    )


def test_phase3bb_r5_usda_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r5-usda-source-activation", "--help"])

    assert result.exit_code == 0
    assert "Usage" in result.output


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _write_blocked_fixture(reports_dir: Path, evidence_dir: Path) -> None:
    r2_dir = reports_dir / "phase3bb_r2_sources"
    r3_dir = reports_dir / "phase3bb_r3_source_activation"
    r2_dir.mkdir(parents=True, exist_ok=True)
    r3_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    tickers = [
        "KXAMSAVO-26JUL03-T0.80",
        "KXAMSAVO-26JUL03-T1.00",
        "KXAMSAVO-26JUL03-T1.20",
        "KXAMSAVO-26JUL03-T1.40",
        "KXAMSAVO-26JUL03-T1.60",
        "KXAMSAVO-26JUL03-T1.80",
        "KXAMSAVO-26JUL03-T2.00",
    ]
    usda_record = {
        "as_of_date": "July 3, 2026",
        "commodity": "Avocados",
        "evidence_available": False,
        "evidence_notes": (
            "report date mismatch: expected July 3, 2026 but USDA source text "
            "reported June 26, 2026; manual review required."
        ),
        "matched_tickers": tickers,
        "metric": "weighted_average_advertised_price",
        "price_usd_each": None,
        "retrieved_at": "2026-07-05T04:15:42Z",
        "source_adapter_key": "commodity_advertised_price_source",
        "source_name": "USDA Agricultural Marketing Service / USDA Market News",
        "source_url": "https://www.ams.usda.gov/mnreports/fvwretail.pdf",
        "variety": "Hass",
        "verification_status": "SOURCE_NOT_AVAILABLE",
    }
    _write_json(
        evidence_dir / "commodity_advertised_price_source.json",
        {"records": [usda_record]},
    )
    _write_json(
        evidence_dir / "infrastructure_data_center_capacity_source.json",
        {
            "records": [
                {
                    "capacity_gw": None,
                    "evidence_notes": "Cushman values are unavailable.",
                    "measurement_year": "2026",
                    "retrieved_at": "2026-07-04T15:25:30Z",
                    "source_name": "Cushman & Wakefield Americas Data Center Update",
                    "source_url": "https://www.cushmanwakefield.com/en/insights/americas-data-center-update",
                }
            ]
        },
    )
    evidence_rows = [
        {
            "source_adapter_key": "commodity_advertised_price_source",
            "ticker": ticker,
            "evidence_status": "SOURCE_EVIDENCE_UNAVAILABLE",
            "safe_to_link": False,
            "safe_to_forecast": False,
            "matched_evidence": usda_record,
            "parsed_fields": {
                "time_window": "July 3, 2026",
                "threshold": ticker.rsplit("T", 1)[-1],
                "direction": "above",
                "metric": "weighted_average_advertised_price",
            },
            "block_reason": "USDA source date mismatch.",
        }
        for ticker in tickers
    ]
    _write_json(
        r2_dir / "phase3bb_r2_general_source_evidence.json",
        {"evidence_rows": evidence_rows},
    )
    _write_json(
        r2_dir / "usda_fvwretail_date_resolution.json",
        {
            "exact_july_3_report_found": False,
            "observed_value_filled": False,
            "sources_checked": [
                {
                    "name": "USDA AMS current FVWRETAIL PDF",
                    "result": "Rejected; report header Fri Jun 26, 2026 FVWRETAIL Page 1.",
                }
            ],
            "target": {"target_date": "July 3, 2026"},
        },
    )
    _write_json(
        r3_dir / "source_evidence_activation.json",
        {
            "source_activation_decisions": [
                {
                    "source_adapter_key": "infrastructure_data_center_capacity_source",
                    "first_blocker": "PROPRIETARY_REVIEW_REQUIRED",
                    "blocker_codes": ["PROPRIETARY_REVIEW_REQUIRED"],
                    "affected_tickers": ["KXUSDCCAPACITY-27MAR05-T45.0"],
                    "block_reason": (
                        "Cushman values are unavailable and licensing review is required."
                    ),
                    "evidence_reference": {
                        "source_name": "Cushman & Wakefield Americas Data Center Update",
                        "source_url": "https://www.cushmanwakefield.com/en/insights/americas-data-center-update",
                        "target_observation": "2026",
                    },
                },
                {
                    "source_adapter_key": "transportation_flight_cancellation_source",
                    "first_blocker": "READY_FOR_REVIEW_NOT_LINK_SAFE",
                    "blocker_codes": ["READY_FOR_REVIEW_NOT_LINK_SAFE"],
                    "affected_tickers": ["KXUSFLYCAN-26JUL03-T2000"],
                    "block_reason": "FlightAware review gates are not link-safe.",
                    "evidence_reference": {
                        "source_name": "Kalshi outcome page citing FlightAware",
                        "source_url": "https://kalshi.com/markets/kxusflycan",
                        "observed_value": "1247",
                        "target_observation": "July 3, 2026",
                    },
                },
            ]
        },
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
