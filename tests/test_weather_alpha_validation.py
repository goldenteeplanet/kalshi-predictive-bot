# ruff: noqa: E501

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kalshi_predictor.data.db import init_db
from kalshi_predictor.kalshi.protocol_math import trading_fee
from kalshi_predictor.weather_alpha_validation import (
    DUPLICATE_WINDOW,
    IDENTITY_MISMATCH,
    INVALID_RESULT,
    MARKET_RESULT_MISSING_ROW,
    UNRESOLVED,
    VALID_EXACT_SETTLEMENT,
    brier_score,
    classify_lineage,
    executable_decision,
    log_loss,
    maximum_drawdown,
    paper_readiness,
    timestamps_in_order,
    write_weather_alpha_validation,
)


def test_all_settlement_lineage_classes() -> None:
    base = {"duplicate": False, "has_market": True, "has_link": True}
    assert (
        classify_lineage(**base, settlement_result="yes", market_result=None)
        == VALID_EXACT_SETTLEMENT
    )
    assert (
        classify_lineage(**base, settlement_result=None, market_result="no")
        == MARKET_RESULT_MISSING_ROW
    )
    assert classify_lineage(**base, settlement_result=None, market_result=None) == UNRESOLVED
    assert (
        classify_lineage(**{**base, "has_link": False}, settlement_result=None, market_result=None)
        == IDENTITY_MISMATCH
    )
    assert classify_lineage(**base, settlement_result="void", market_result=None) == INVALID_RESULT
    assert (
        classify_lineage(
            **{**base, "duplicate": True}, settlement_result="yes", market_result="yes"
        )
        == DUPLICATE_WINDOW
    )


def test_no_lookahead_order_is_strict() -> None:
    source = datetime(2026, 1, 1, tzinfo=UTC)
    assert timestamps_in_order(
        source,
        source + timedelta(minutes=1),
        source + timedelta(minutes=2),
        source + timedelta(minutes=3),
    )
    assert not timestamps_in_order(
        source,
        source + timedelta(minutes=2),
        source + timedelta(minutes=1),
        source + timedelta(minutes=3),
    )
    assert not timestamps_in_order(None, source, source, source)


def test_yes_and_no_executable_prices() -> None:
    yes = executable_decision(Decimal("0.70"), {"yes_ask": "0.60", "no_ask": "0.42"})
    no = executable_decision(Decimal("0.30"), {"yes_ask": "0.40", "no_ask": "0.60"})
    assert yes == ("YES", Decimal("0.60"), Decimal("0.10"))
    assert no == ("NO", Decimal("0.60"), Decimal("0.10"))


def test_scores_and_drawdown() -> None:
    assert brier_score(Decimal("0.8"), 1) == Decimal("0.04")
    assert log_loss(Decimal("0.8"), 1) > 0
    assert trading_fee(price=Decimal("0.60"), contracts=1) == Decimal("0.0168")
    assert maximum_drawdown(
        [Decimal("1"), Decimal("-0.4"), Decimal("-0.8"), Decimal("0.5")]
    ) == Decimal("1.2")


def test_readiness_cannot_enable_paper_creation() -> None:
    performance = {
        "settled_observations": 100,
        "no_lookahead_violations": 0,
        "outperforms_market": True,
        "positive_post_cost": True,
    }
    result = paper_readiness(performance, {"guarded_counts_unchanged": True})
    assert result["settled_sample_gate"] is True
    assert result["paper_order_creation_enabled"] is False
    assert result["phase8_ready"] is False


def test_query_only_writer_preserves_guarded_tables(tmp_path) -> None:
    database_path = tmp_path / "weather.db"
    init_db(f"sqlite:///{database_path.as_posix()}")
    artifacts = write_weather_alpha_validation(
        database_url=f"sqlite:///{database_path.as_posix()}",
        output_dir=tmp_path / "reports",
    )
    assert artifacts.readiness.exists()
    assert "Guarded counts unchanged: `True`" in (
        artifacts.output_dir / "SAFETY_INVARIANTS.md"
    ).read_text(encoding="utf-8")
