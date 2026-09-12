from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from kalshi_predictor.crypto.cf_settlement_windows import (
    CFTick,
    CFWindow,
    CFWindowRules,
    reconstruct_cf_windows,
)


def rules() -> CFWindowRules:
    return CFWindowRules(
        market_ticker="SYNTHETIC-TEST", index_id="BRTI",
        closing=CFWindow(60000, 120000, False, True, 1000, 60),
        opening=CFWindow(0, 60000, False, True, 1000, 60),
        decimal_places=2, rounding="HALF_UP", amendments="LATEST_KNOWN_AS_OF",
        rule_source="synthetic test fixture; not certified", rule_sha256="a" * 64,
    )


def ticks() -> tuple[CFTick, ...]:
    return tuple(
        CFTick("BRTI", time, Decimal(time // 1000), time + 1, None, "b" * 64)
        for time in range(1000, 120001, 1000)
    )


def test_complete_both_windows_exact_average_and_audit() -> None:
    result = reconstruct_cf_windows(rules(), ticks(), as_of_ms=130000)
    assert result.opening is not None
    assert result.opening.value == Decimal("30.50")
    assert result.closing.value == Decimal("90.50")
    assert result.closing.tick_count == 60
    assert result.status == "RECONSTRUCTED_NOT_CERTIFIED_SETTLEMENT"
    assert len(result.rules_sha256) == 64
    assert result == reconstruct_cf_windows(rules(), tuple(reversed(ticks())), as_of_ms=130000)


def test_trailing_and_quarter_hour_windows_differ() -> None:
    trailing = replace(
        rules(), opening=None, closing=CFWindow(60000, 120000, True, False, 1000, 60),
    )
    left_closed = reconstruct_cf_windows(trailing, ticks(), as_of_ms=130000)
    right_closed = reconstruct_cf_windows(rules(), ticks(), as_of_ms=130000)
    assert left_closed.closing.value == Decimal("89.50")
    assert right_closed.closing.value == Decimal("90.50")


@pytest.mark.parametrize("bad", [ticks()[:-1], ticks()[-1:]])
def test_missing_or_latest_only_cannot_reconstruct(bad: tuple[CFTick, ...]) -> None:
    with pytest.raises(ValueError, match="INCOMPLETE_WINDOW"):
        reconstruct_cf_windows(rules(), bad, as_of_ms=130000)


def test_duplicate_revision_fails_even_identical() -> None:
    with pytest.raises(ValueError, match="DUPLICATE_TICK_REVISION"):
        reconstruct_cf_windows(rules(), ticks() + (ticks()[0],), as_of_ms=130000)


def test_amendment_and_receipt_cutoff_no_hindsight() -> None:
    amended = replace(ticks()[-1], value=Decimal(180), amend_time_ms=125000, received_ms=126000)
    data = ticks() + (amended,)
    before = reconstruct_cf_windows(rules(), data, as_of_ms=125500)
    after = reconstruct_cf_windows(rules(), data, as_of_ms=130000)
    assert before.closing.value == Decimal("90.50")
    assert after.closing.value == Decimal("91.50")
    assert after.closing.amended_tick_count == 1
    assert before.closing.selected_ticks_sha256 != after.closing.selected_ticks_sha256
    with pytest.raises(ValueError, match="AMENDMENT_POLICY_REJECTED"):
        reconstruct_cf_windows(replace(rules(), amendments="REJECT"), data, as_of_ms=130000)


@pytest.mark.parametrize("field,value,error", [
    ("value", Decimal("NaN"), "INVALID_OR_SYNTHETIC_TICK"),
    ("value", Decimal("-1"), "INVALID_OR_SYNTHETIC_TICK"),
    ("repeat_of_previous_value", True, "INVALID_OR_SYNTHETIC_TICK"),
    ("source_sha256", "", "INVALID_OR_SYNTHETIC_TICK"),
    ("index_id", "ETHUSD_RTI", "INDEX_MISMATCH"),
    ("time_ms", 1001, "OFF_GRID_TICK"),
    ("received_ms", 999, "INVALID_TICK_TIME"),
])
def test_invalid_tick_fails(field: str, value: object, error: str) -> None:
    bad = replace(ticks()[0], **{field: value})
    with pytest.raises(ValueError, match=error):
        reconstruct_cf_windows(rules(), (bad,) + ticks()[1:], as_of_ms=130000)


def test_incomplete_at_cutoff_and_missing_historical_original() -> None:
    with pytest.raises(ValueError, match="WINDOW_NOT_COMPLETE_AS_OF"):
        reconstruct_cf_windows(rules(), ticks(), as_of_ms=119999)
    amended = replace(ticks()[-1], amend_time_ms=125000, received_ms=126000)
    with pytest.raises(ValueError, match="INCOMPLETE_WINDOW"):
        reconstruct_cf_windows(rules(), ticks()[:-1] + (amended,), as_of_ms=124000)


@pytest.mark.parametrize("mode,expected", [
    ("HALF_UP", "1.01"), ("HALF_EVEN", "1.00"),
    ("DOWN", "1.00"), ("FLOOR", "1.00"), ("CEILING", "1.01"),
])
def test_exact_rounding_ignores_decimal_context(mode: str, expected: str) -> None:
    same = tuple(replace(tick, value=Decimal("1.005")) for tick in ticks())
    with localcontext() as context:
        context.prec = 2
        result = reconstruct_cf_windows(replace(rules(), rounding=mode), same, as_of_ms=130000)
    assert result.closing.value == Decimal(expected)


@pytest.mark.parametrize("window", [
    CFWindow(60000, 120000, True, True, 1000, 60),
    CFWindow(60000, 120000, False, False, 1000, 60),
    CFWindow(60000, 120000, False, True, 200, 300),
])
def test_cannot_silently_change_counts_or_resolution(window: CFWindow) -> None:
    with pytest.raises(ValueError):
        reconstruct_cf_windows(replace(rules(), closing=window), ticks(), as_of_ms=130000)


def test_rule_ambiguity_fails_without_defaulting() -> None:
    with pytest.raises(ValueError, match="EXPLICIT_SUPPORTED_RULES_REQUIRED"):
        reconstruct_cf_windows(replace(rules(), rounding="UNKNOWN"), ticks(), as_of_ms=130000)
    with pytest.raises(ValueError, match="OPENING_CLOSING_OVERLAP"):
        reconstruct_cf_windows(replace(rules(), opening=rules().closing), ticks(), as_of_ms=130000)
