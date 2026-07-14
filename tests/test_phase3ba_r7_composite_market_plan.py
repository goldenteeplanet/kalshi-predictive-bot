from __future__ import annotations

from typer.testing import CliRunner

from kalshi_predictor import phase3ba_r7
from kalshi_predictor.cli import app


def test_phase3ba_r7_classifies_composite_types() -> None:
    assert (
        phase3ba_r7._classify_composite_type("KXMVESPORTSMULTIGAME-ABC", None, None)
        == "sports_multigame"
    )
    assert (
        phase3ba_r7._classify_composite_type("KXMVE-ROW", "KXMVECROSSCATEGORY-ABC", None)
        == "cross_category"
    )
    assert (
        phase3ba_r7._classify_composite_type("KXMVEFOO-ABC", None, None)
        == "other_kxmve_composite"
    )


def test_phase3ba_r7_summary_keeps_rows_parked() -> None:
    rows = [
        {
            "category": "sports",
            "composite_type": "sports_multigame",
            "parsed_legs": 2,
            "excluded_from_single_market_remediation": True,
            "normal_link_remediation_allowed": False,
            "exact_component_evidence_exists": False,
        },
        {
            "category": "cross_category",
            "composite_type": "cross_category",
            "parsed_legs": 3,
            "excluded_from_single_market_remediation": True,
            "normal_link_remediation_allowed": False,
            "exact_component_evidence_exists": False,
        },
    ]

    summary = phase3ba_r7._summary(rows)

    assert summary["unsupported_composite_rows"] == 2
    assert summary["all_rows_parked"] is True
    assert summary["normal_single_market_remediation_allowed_rows"] == 0
    assert summary["exact_component_evidence_rows"] == 0
    assert summary["coverage_pollution_status"] == "PARKED_OUTSIDE_SINGLE_MARKET_LINK_COVERAGE"


def test_phase3ba_r7_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3ba-r7-composite-market-plan", "--help"])

    assert result.exit_code == 0
    assert "phase3ba-r7-composite-market-plan" in result.output
