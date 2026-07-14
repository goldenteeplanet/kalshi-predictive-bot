from __future__ import annotations

from typer.testing import CliRunner

from kalshi_predictor import phase3bb_r4_economic_parser_backfill as backfill
from kalshi_predictor.cli import app
from kalshi_predictor.market_legs import ParsedMarketLeg


def _parsed_economic_leg(confidence: str = "0.80") -> ParsedMarketLeg:
    return ParsedMarketLeg(
        leg_index=0,
        side="YES",
        category="economic",
        market_type="economic_release",
        entity_name="Federal Reserve",
        operator="ABOVE",
        threshold_value="5.00",
        unit="percent",
        confidence=confidence,
        raw_text="Will the Federal Reserve hike rates?",
        reason="economic calendar keyword matched",
        raw_json={},
    )


def test_economic_pattern_classification_priority() -> None:
    assert backfill.classify_economic_pattern("Will CPI inflation exceed 3%?") == "CPI"
    assert backfill.classify_economic_pattern("Will nonfarm payrolls beat expectations?") == (
        "jobs/payroll"
    )
    assert backfill.classify_economic_pattern("Will unemployment rise?") == "unemployment"
    assert backfill.classify_economic_pattern("Will the Federal Reserve hike rates?") == (
        "Fed rates"
    )
    assert backfill.classify_economic_pattern("Will GDP growth be positive?") == "GDP"
    assert backfill.classify_economic_pattern("Will Treasury yields rise?") == "Treasury"


def test_source_mapping_defer_tradingeconomics() -> None:
    fed = backfill.source_mapping_for_pattern("Fed rates")

    assert fed["official_source_family"] == "Federal Reserve"
    assert "FRED" in fed["source_options"]
    assert fed["paid_deferred_sources"] == "TradingEconomics=DEFERRED"
    assert fed["supported_pattern"] is True


def test_parser_ready_requires_supported_pattern_and_parser_confidence() -> None:
    ready = backfill.parser_preview_status(
        market_active=True,
        link_confidence=0.85,
        missing_parsed_legs=True,
        supported_pattern=True,
        parsed_economic_legs=[_parsed_economic_leg()],
        feature_present=True,
    )
    unsupported = backfill.parser_preview_status(
        market_active=True,
        link_confidence=0.85,
        missing_parsed_legs=True,
        supported_pattern=False,
        parsed_economic_legs=[_parsed_economic_leg()],
        feature_present=True,
    )
    low_confidence = backfill.parser_preview_status(
        market_active=True,
        link_confidence=0.85,
        missing_parsed_legs=True,
        supported_pattern=True,
        parsed_economic_legs=[_parsed_economic_leg("0.40")],
        feature_present=True,
    )

    assert ready["parser_ready"] is True
    assert ready["forecast_safe_preview"] is True
    assert unsupported["first_blocker"] == "UNSUPPORTED_PATTERN"
    assert low_confidence["first_blocker"] == "PARSER_CONFIDENCE_TOO_LOW"


def test_phase3bb_r4_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r4-economic-parser-backfill", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r4-economic-parser-backfill" in result.output
