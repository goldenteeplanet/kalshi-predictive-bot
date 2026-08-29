"""Offline effective-time fee schedule and worst-case cost proof."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal

SCHEMA = "phase4nf.fee-envelope.v1"


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


def select_schedule(schedules: object, *, order_time: str) -> dict[str, object]:
    errors: list[str] = []
    moment = _time(order_time)
    if not isinstance(schedules, list) or moment is None:
        return {"verdict": "REFUSE", "errors": ["INPUT_INVALID"], "schedule": None}
    eligible = []
    for index, schedule in enumerate(schedules):
        if not isinstance(schedule, dict):
            errors.append(f"SCHEDULE_{index}_INVALID")
            continue
        start, end, published = (
            _time(schedule.get(key))
            for key in ("effective_from", "effective_until", "published_at")
        )
        if start is None or end is None or published is None or start >= end:
            errors.append(f"SCHEDULE_{index}_TIME_INVALID")
            continue
        numeric = (
            "maker_rate",
            "taker_rate",
            "minimum_fee",
            "maximum_fee",
            "rebate_rate",
            "surcharge_rate",
            "cancel_fee",
            "settlement_fee",
            "rounding_increment",
        )
        if any(
            _decimal(schedule.get(key)) is None or _decimal(schedule[key]) < 0 for key in numeric
        ):
            errors.append(f"SCHEDULE_{index}_NEGATIVE_OR_INVALID_FEE")
            continue
        if (
            _decimal(schedule["maximum_fee"]) < _decimal(schedule["minimum_fee"])
            or _decimal(schedule["rounding_increment"]) <= 0
        ):
            errors.append(f"SCHEDULE_{index}_CAP_OR_ROUNDING_INVALID")
            continue
        if published <= moment and start <= moment < end:
            eligible.append(schedule)
    if len(eligible) == 0:
        errors.append("NO_PUBLICLY_EFFECTIVE_SCHEDULE")
    elif len(eligible) > 1:
        errors.append("OVERLAPPING_EFFECTIVE_SCHEDULES")
    return {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "schedule": eligible[0] if len(eligible) == 1 else None,
    }


def fee_envelope(trade: object, schedules: object) -> dict[str, object]:
    if not isinstance(trade, dict):
        return _result(["TRADE_INVALID"], None, None)
    selection = select_schedule(schedules, order_time=str(trade.get("order_time")))
    if selection["verdict"] != "PASS":
        return _result(selection["errors"], None, None)
    schedule = selection["schedule"]
    role = trade.get("liquidity_role")
    if role not in {"maker", "taker", "ambiguous"}:
        return _result(["LIQUIDITY_ROLE_INVALID"], None, schedule)
    baseline_role = role if role != "ambiguous" else "maker"
    worst_role = (
        role
        if role != "ambiguous"
        else max(("maker", "taker"), key=lambda value: _decimal(schedule[f"{value}_rate"]))
    )
    baseline = _calculate(trade, schedule, baseline_role, per_fill_minimum=False)
    worst = _calculate(trade, schedule, worst_role, per_fill_minimum=True)
    errors = sorted(set(baseline["errors"] + worst["errors"]))
    result = _result(errors, {"baseline": baseline, "worst_case": worst}, schedule)
    result.update(
        {
            "selected_version": schedule.get("version"),
            "baseline_role": baseline_role,
            "worst_case_role": worst_role,
            "readiness_uses": "worst_case",
            "schedule_sha256": _digest(schedule),
        }
    )
    result["envelope_sha256"] = _digest(result)
    return result


def _calculate(trade, schedule, role, *, per_fill_minimum):
    errors = []
    price = _decimal(trade.get("price"))
    quantity = _decimal(trade.get("quantity"))
    fills = trade.get("partial_fills") or [quantity]
    if (
        price is None
        or quantity is None
        or not 0 <= price <= 1
        or quantity <= 0
        or not isinstance(fills, list)
    ):
        return {"errors": ["TRADE_ECONOMICS_INVALID"], "total_fee": "0"}
    fill_values = [_decimal(value) for value in fills]
    if (
        any(value is None or value <= 0 for value in fill_values)
        or sum(fill_values, Decimal("0")) > quantity
    ):
        return {"errors": ["PARTIAL_FILLS_INVALID"], "total_fee": "0"}
    rate = _decimal(schedule[f"{role}_rate"])
    minimum = _decimal(schedule["minimum_fee"])
    maximum = _decimal(schedule["maximum_fee"])
    increment = _decimal(schedule["rounding_increment"])
    surcharge_rate = _decimal(schedule["surcharge_rate"])
    rebate_rate = _decimal(schedule["rebate_rate"])
    components = []
    for fill in fill_values:
        raw = rate * price * (1 - price) * fill
        charge = max(minimum if per_fill_minimum else Decimal("0"), _ceil(raw, increment))
        charge = min(maximum, charge)
        surcharge = _ceil(charge * surcharge_rate, increment)
        rebate = _ceil(raw * rebate_rate, increment) if role == "maker" else Decimal("0")
        if rebate > charge + surcharge:
            errors.append("REBATE_EXCEEDS_CHARGES")
        components.append(
            {
                "fill_quantity": str(fill),
                "charge": str(charge),
                "surcharge": str(surcharge),
                "rebate": str(rebate),
            }
        )
    trading = sum(
        (
            Decimal(row["charge"]) + Decimal(row["surcharge"]) - Decimal(row["rebate"])
            for row in components
        ),
        Decimal("0"),
    )
    if not per_fill_minimum:
        trading = max(minimum, trading)
    trading = min(maximum, trading)
    cancel = _decimal(schedule["cancel_fee"]) * int(trade.get("cancel_replace_count", 0))
    settlement = (
        _decimal(schedule["settlement_fee"]) * sum(fill_values, Decimal("0"))
        if trade.get("settled")
        else Decimal("0")
    )
    total = trading + cancel + settlement
    return {
        "errors": sorted(set(errors)),
        "role": role,
        "components": components,
        "trading_fee": str(trading),
        "cancel_fees": str(cancel),
        "settlement_fees": str(settlement),
        "total_fee": str(total),
        "per_fill_minimum": per_fill_minimum,
    }


def compare_flat_fee(
    envelope: dict[str, object], *, flat_fee_per_contract: str, quantity: str, gross_pnl: str
) -> dict[str, object]:
    simple_fee = Decimal(flat_fee_per_contract) * Decimal(quantity)
    verified = Decimal(envelope["envelopes"]["worst_case"]["total_fee"])
    gross = Decimal(gross_pnl)
    result = {
        "simple_fee": str(simple_fee),
        "verified_worst_case_fee": str(verified),
        "simple_net_pnl": str(gross - simple_fee),
        "verified_net_pnl": str(gross - verified),
        "pnl_inflation": str(verified - simple_fee),
        "net_edge_survives": gross - verified > 0,
        "readiness": "PASS" if gross - verified > 0 else "REFUSE",
        "safety": _safety(),
    }
    result["comparison_sha256"] = _digest(result)
    return result


def _ceil(value, increment):
    return (value / increment).to_integral_value(rounding=ROUND_CEILING) * increment


def _decimal(value):
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _result(errors, envelopes, schedule):
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "envelopes": envelopes,
        "schedule": schedule,
        "execution_capability": False,
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
