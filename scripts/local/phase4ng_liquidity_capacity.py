"""Offline liquidity-capacity curves and nonlinear market-impact stress."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

SCHEMA = "phase4ng.liquidity-capacity.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def build_capacity_curves(
    book: object,
    *,
    decision_time: str,
    requested_sizes: list[int],
    yes_probability: str,
    fee_per_contract: str,
    maximum_book_age_ms: int,
    price_cap: str,
    tick_size: str,
    withdrawal_haircut: str,
    queue_ahead: str,
    correlated_demand: str,
    self_impact_coefficient: str,
) -> dict[str, object]:
    errors: list[str] = []
    if (
        not isinstance(book, dict)
        or not requested_sizes
        or requested_sizes != sorted(set(requested_sizes))
        or any(size <= 0 for size in requested_sizes)
    ):
        return _result(["INPUT_INVALID"], {})
    moment, captured = _time(decision_time), _time(book.get("captured_at"))
    if moment is None or captured is None or captured > moment:
        errors.append("BOOK_TIME_INVALID_OR_FUTURE")
    elif (moment - captured).total_seconds() * 1000 > maximum_book_age_ms:
        errors.append("STALE_DEPTH")
    asks, bids = book.get("asks"), book.get("bids")
    if not isinstance(asks, list) or not isinstance(bids, list) or not asks or not bids:
        errors.append("ZERO_OR_INVALID_DEPTH")
        return _result(sorted(set(errors)), {})
    tick = Decimal(tick_size)
    ask_prices, bid_prices = [], []
    for side, levels, prices in (("ASK", asks, ask_prices), ("BID", bids, bid_prices)):
        seen = set()
        for index, level in enumerate(levels):
            try:
                price, size = Decimal(str(level["price"])), Decimal(str(level["size"]))
            except Exception:
                errors.append(f"{side}_{index}_INVALID")
                continue
            if price in seen:
                errors.append(f"{side}_DUPLICATE_LEVEL")
            if size <= 0 or price <= 0 or price >= 1 or price % tick != 0:
                errors.append(f"{side}_{index}_PRICE_SIZE_OR_TICK_INVALID")
            seen.add(price)
            prices.append(price)
    if ask_prices != sorted(ask_prices) or bid_prices != sorted(bid_prices, reverse=True):
        errors.append("NON_MONOTONIC_BOOK_LEVELS")
    if bid_prices and ask_prices and bid_prices[0] >= ask_prices[0]:
        errors.append("LOCKED_OR_CROSSED_BOOK")
    if errors:
        return _result(sorted(set(errors)), {})
    parameters = {
        "optimistic": {
            "size_multiplier": Decimal("1.20"),
            "queue": Decimal("0"),
            "impact_multiplier": Decimal("0.5"),
        },
        "central": {
            "size_multiplier": Decimal("1"),
            "queue": Decimal(queue_ahead) / 2,
            "impact_multiplier": Decimal("1"),
        },
        "pessimistic": {
            "size_multiplier": max(Decimal("0"), Decimal("1") - Decimal(withdrawal_haircut)),
            "queue": Decimal(queue_ahead) + Decimal(correlated_demand),
            "impact_multiplier": Decimal("1.5"),
        },
    }
    curves = {}
    for name, assumptions in parameters.items():
        curves[name] = [
            _execute(
                asks,
                Decimal(size),
                probability=Decimal(yes_probability),
                fee=Decimal(fee_per_contract),
                price_cap=Decimal(price_cap),
                size_multiplier=assumptions["size_multiplier"],
                queue=assumptions["queue"],
                impact=Decimal(self_impact_coefficient) * assumptions["impact_multiplier"],
            )
            for size in requested_sizes
        ]
    profitable = [
        row["requested_size"]
        for row in curves["pessimistic"]
        if row["full_fill"] and Decimal(row["net_expected_pnl"]) > 0
    ]
    capacity = max(profitable, default=0)
    result = _result([], curves)
    result.update(
        {
            "pessimistic_profitable_capacity": capacity,
            "readiness_curve": "pessimistic",
            "book_sha256": _digest(book),
            "execution_capability": False,
        }
    )
    result["capacity_sha256"] = _digest(result)
    return result


def assess_proposed_size(curves: dict[str, object], *, proposed_size: int) -> dict[str, object]:
    capacity = int(curves.get("pessimistic_profitable_capacity", 0))
    pessimistic = next(
        (
            row
            for row in curves.get("curves", {}).get("pessimistic", [])
            if row["requested_size"] == proposed_size
        ),
        None,
    )
    errors = []
    if pessimistic is None:
        errors.append("PROPOSED_SIZE_NOT_EVALUATED")
    elif (
        proposed_size > capacity
        or not pessimistic["full_fill"]
        or Decimal(pessimistic["net_expected_pnl"]) <= 0
    ):
        errors.append("PROPOSED_SIZE_EXCEEDS_PESSIMISTIC_PROFITABLE_CAPACITY")
    result = {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "proposed_size": proposed_size,
        "pessimistic_profitable_capacity": capacity,
        "order_authorized": False,
        "safety": _safety(),
    }
    result["assessment_sha256"] = _digest(result)
    return result


def compare_fixed_size(
    curves: dict[str, object], *, requested_size: int, fixed_price: str
) -> dict[str, object]:
    row = next(
        item for item in curves["curves"]["pessimistic"] if item["requested_size"] == requested_size
    )
    fixed_cost = Decimal(fixed_price) * requested_size
    actual_cost = Decimal(row["average_price"]) * Decimal(row["filled_size"])
    unfilled = Decimal(requested_size) - Decimal(row["filled_size"])
    inflation = actual_cost + unfilled * Decimal(fixed_price) - fixed_cost
    result = {
        "requested_size": requested_size,
        "fixed_price_cost": str(fixed_cost),
        "pessimistic_executable_cost": str(actual_cost),
        "unfilled_size": str(unfilled),
        "fixed_assumption_cost_error": str(inflation),
        "fixed_size_accepted_for_readiness": False,
        "safety": _safety(),
    }
    result["comparison_sha256"] = _digest(result)
    return result


def _execute(levels, requested, *, probability, fee, price_cap, size_multiplier, queue, impact):
    remaining = requested
    filled = cost = Decimal("0")
    queue_remaining = queue
    total_visible = sum((Decimal(str(level["size"])) for level in levels), Decimal("0")) or Decimal(
        "1"
    )
    marginal = None
    for level in levels:
        base_price = Decimal(str(level["price"]))
        available = Decimal(str(level["size"])) * size_multiplier
        consumed_queue = min(queue_remaining, available)
        queue_remaining -= consumed_queue
        available -= consumed_queue
        if available <= 0:
            continue
        projected_fraction = (filled + min(remaining, available)) / total_visible
        execution_price = min(Decimal("0.99"), base_price + impact * projected_fraction**2)
        if execution_price > price_cap:
            break
        take = min(remaining, available)
        cost += take * execution_price
        filled += take
        remaining -= take
        marginal = execution_price
        if remaining <= 0:
            break
    average = cost / filled if filled else Decimal("0")
    fees = fee * filled
    expected_payout = probability * filled
    return {
        "requested_size": int(requested),
        "filled_size": str(filled),
        "full_fill": filled == requested,
        "fill_rate": str(filled / requested),
        "average_price": str(average),
        "marginal_price": str(marginal) if marginal is not None else None,
        "fees": str(fees),
        "net_expected_pnl": str(expected_payout - cost - fees),
        "depth_exhausted": remaining > 0,
    }


def _result(errors, curves):
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "curves": curves,
        "safety": _safety(),
    }


def _safety():
    return {
        "offline_only": True,
        "order_creation": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_execution": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
