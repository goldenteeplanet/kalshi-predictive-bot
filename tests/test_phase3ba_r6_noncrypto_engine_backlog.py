from __future__ import annotations

from typer.testing import CliRunner

from kalshi_predictor import phase3ba_r6
from kalshi_predictor.cli import app


def test_phase3ba_r6_selects_sports_after_weather_when_ready() -> None:
    rows = [
        {
            "category": "weather",
            "score_after_weather": -1,
            "parsed_market_count": 96,
            "linked_count": 96,
            "source_readiness": "SOURCE_AND_FEATURES_READY",
            "next_implementation_step": "finish weather",
            "primary_blocker": "EV_NOT_POSITIVE",
        },
        {
            "category": "sports",
            "score_after_weather": 250,
            "parsed_market_count": 50000,
            "linked_count": 45000,
            "source_readiness": "SPORTS_SOURCE_PARTIAL_WITH_FEATURES",
            "next_implementation_step": "build sports provenance sprint",
            "primary_blocker": "UNSUPPORTED_KXMVE_COMPOSITES_PARKED",
        },
        {
            "category": "economic",
            "score_after_weather": -50,
            "parsed_market_count": 0,
            "linked_count": 0,
            "source_readiness": "ECONOMIC_SOURCE_NOT_INGESTED",
            "next_implementation_step": "build economic parser",
            "primary_blocker": "NO_PARSED_MARKET_INVENTORY",
        },
    ]

    selected = phase3ba_r6._select_next_category_after_weather(rows)

    assert selected["category"] == "sports"
    assert selected["primary_blocker"] == "UNSUPPORTED_KXMVE_COMPOSITES_PARKED"


def test_phase3ba_r6_weather_blocker_prefers_current_r5_truth() -> None:
    blocker = phase3ba_r6._weather_blocker_from_reports(
        {
            "category_summaries": {
                "weather": {"blocker_counts": {"SNAPSHOT_MISSING": 10, "EV_NOT_POSITIVE": 2}}
            }
        },
        {"after_summary": {"first_hard_blocker_counts": {"EV_NOT_POSITIVE": 99}}},
        {"summary": {"ranking_gap_rows": 1}},
    )

    assert blocker == "SNAPSHOT_MISSING"


def test_phase3ba_r6_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3ba-r6-noncrypto-engine-backlog", "--help"])

    assert result.exit_code == 0
    assert "phase3ba-r6-noncrypto-engine-backlog" in result.output
