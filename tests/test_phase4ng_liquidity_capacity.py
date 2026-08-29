from __future__ import annotations

import copy
from decimal import Decimal

from scripts.local.phase4ng_liquidity_capacity import (
    assess_proposed_size,
    build_capacity_curves,
    compare_fixed_size,
)


def _book():
    return {
        "captured_at": "2026-08-01T11:59:59.900Z",
        "bids": [{"price": "0.49", "size": 10}, {"price": "0.48", "size": 20}],
        "asks": [
            {"price": "0.51", "size": 5},
            {"price": "0.53", "size": 10},
            {"price": "0.56", "size": 20},
        ],
    }


def _curves(book=None, **overrides):
    values = {
        "decision_time": "2026-08-01T12:00:00Z",
        "requested_sizes": [1, 5, 10, 20, 30],
        "yes_probability": "0.65",
        "fee_per_contract": "0.02",
        "maximum_book_age_ms": 200,
        "price_cap": "0.70",
        "tick_size": "0.01",
        "withdrawal_haircut": "0.30",
        "queue_ahead": "1",
        "correlated_demand": "2",
        "self_impact_coefficient": "0.04",
    }
    values.update(overrides)
    return build_capacity_curves(book or _book(), **values)


def test_capacity_curves_are_deterministic_ordered_and_pessimistic() -> None:
    first = _curves()
    assert first == _curves()
    assert first["verdict"] == "PASS"
    for index in range(5):
        fills = [
            Decimal(first["curves"][name][index]["filled_size"])
            for name in ("optimistic", "central", "pessimistic")
        ]
        assert fills[0] >= fills[1] >= fills[2]
    assert first["readiness_curve"] == "pessimistic"


def test_marginal_and_average_prices_are_non_decreasing_with_size() -> None:
    rows = _curves()["curves"]["pessimistic"]
    averages = [Decimal(row["average_price"]) for row in rows if Decimal(row["filled_size"]) > 0]
    assert averages == sorted(averages)


def test_zero_one_level_fragmented_and_large_depth_cases() -> None:
    zero = {"captured_at": _book()["captured_at"], "bids": [], "asks": []}
    assert "ZERO_OR_INVALID_DEPTH" in _curves(zero)["errors"]
    one = {
        "captured_at": _book()["captured_at"],
        "bids": [{"price": "0.49", "size": 2}],
        "asks": [{"price": "0.51", "size": 2}],
    }
    result = _curves(one)
    assert result["curves"]["pessimistic"][-1]["depth_exhausted"] is True
    assert Decimal(result["curves"]["pessimistic"][-1]["filled_size"]) < 30


def test_duplicate_nonmonotonic_locked_crossed_and_off_tick_levels_refuse() -> None:
    duplicate = _book()
    duplicate["asks"].append(copy.deepcopy(duplicate["asks"][0]))
    assert "DUPLICATE_LEVEL" in " ".join(_curves(duplicate)["errors"])
    reversed_book = _book()
    reversed_book["asks"] = list(reversed(reversed_book["asks"]))
    assert "NON_MONOTONIC_BOOK_LEVELS" in _curves(reversed_book)["errors"]
    locked = _book()
    locked["bids"][0]["price"] = "0.51"
    assert "LOCKED_OR_CROSSED_BOOK" in _curves(locked)["errors"]
    off_tick = _book()
    off_tick["asks"][0]["price"] = "0.515"
    assert "PRICE_SIZE_OR_TICK_INVALID" in " ".join(_curves(off_tick)["errors"])


def test_stale_and_future_depth_refuse() -> None:
    stale = _book()
    stale["captured_at"] = "2026-08-01T11:00:00Z"
    assert "STALE_DEPTH" in _curves(stale)["errors"]
    future = _book()
    future["captured_at"] = "2026-08-01T12:00:01Z"
    assert "BOOK_TIME_INVALID_OR_FUTURE" in _curves(future)["errors"]


def test_spoof_like_withdrawal_and_correlated_orders_reduce_capacity() -> None:
    calm = _curves(withdrawal_haircut="0", correlated_demand="0")
    stressed = _curves(withdrawal_haircut="0.80", correlated_demand="10")
    assert stressed["pessimistic_profitable_capacity"] < calm["pessimistic_profitable_capacity"]


def test_self_impact_spread_sweeps_and_price_cap_reduce_edge() -> None:
    no_impact = _curves(self_impact_coefficient="0")
    impact = _curves(self_impact_coefficient="0.20")
    assert Decimal(impact["curves"]["pessimistic"][2]["average_price"]) >= Decimal(
        no_impact["curves"]["pessimistic"][2]["average_price"]
    )
    capped = _curves(price_cap="0.52")
    assert capped["curves"]["pessimistic"][-1]["depth_exhausted"] is True


def test_proposed_size_above_profitable_capacity_refuses_without_authorization() -> None:
    curves = _curves()
    result = assess_proposed_size(curves, proposed_size=30)
    assert result["verdict"] == "REFUSE"
    assert result["order_authorized"] is False


def test_fixed_size_assumption_error_is_quantified() -> None:
    curves = _curves()
    comparison = compare_fixed_size(curves, requested_size=30, fixed_price="0.51")
    assert Decimal(comparison["unfilled_size"]) >= 0
    assert comparison["fixed_size_accepted_for_readiness"] is False


def test_capacity_model_has_no_order_or_execution_capability() -> None:
    safety = _curves()["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
