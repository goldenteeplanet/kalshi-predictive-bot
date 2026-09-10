"""Pure frozen-input diagnostics; no readiness, repricing or execution authority."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def frozen_economics(decision: dict[str, Any]) -> dict[str, Any]:
    """Recompute only from explicit frozen values; missing costs are never zero."""
    probability = _number(decision.get("forecast_probability"))
    if probability is not None and not 0 <= probability <= 1:
        probability = None
    side = decision.get("side")
    if side == "BUY_NO" and probability is not None:
        probability = 1 - probability
    elif side != "BUY_YES":
        probability = None
    price = _number(decision.get("executable_price"))
    if price is not None and not 0 < price < 1:
        price = None
    costs = {
        name: _number(decision.get(key))
        for name, key in (
            ("fees", "estimated_fee"),
            ("slippage", "slippage"),
            ("uncertainty", "uncertainty"),
        )
    }
    costs = {
        key: value if value is not None and value >= 0 else None for key, value in costs.items()
    }
    gross = probability - price if probability is not None and price is not None else None
    total_cost = sum((value for value in costs.values() if value is not None), Decimal(0))
    after_fee = gross - costs["fees"] if gross is not None and costs["fees"] is not None else None
    net = (
        gross - total_cost
        if gross is not None and all(value is not None for value in costs.values())
        else None
    )
    settings = decision.get("settings")
    threshold = _number(settings.get("paper_min_edge")) if isinstance(settings, dict) else None
    if threshold is not None and threshold < 0:
        threshold = None
    values = {
        "probability": probability,
        "executable_price": price,
        **costs,
        "gross_edge": gross,
        "after_fee_ev": after_fee,
        "net_ev": net,
        "minimum_net_ev": threshold,
    }
    # Price sensitivity keeps all recorded costs constant; fees can change with price.
    ceiling = (
        probability - total_cost - threshold
        if (net is not None and threshold is not None and probability is not None)
        else None
    )
    probability_scenarios = []
    for points in (1, 2, 5):
        adjusted = (
            min(Decimal(1), probability + Decimal(points) / 100)
            if probability is not None
            else None
        )
        scenario_net = (
            net + adjusted - probability
            if (net is not None and adjusted is not None and probability is not None)
            else None
        )
        probability_scenarios.append(
            {
                "increase_probability_points": points,
                "adjusted_side_probability": None if adjusted is None else str(adjusted),
                "net_ev": None if scenario_net is None else str(scenario_net),
            }
        )
    price_scenarios = []
    for cents in (1, 2):
        adjusted_price = price - Decimal(cents) / 100 if price is not None else None
        if adjusted_price is not None and not 0 < adjusted_price < 1:
            adjusted_price = None
        scenario_net = (
            net + price - adjusted_price
            if (net is not None and price is not None and adjusted_price is not None)
            else None
        )
        price_scenarios.append(
            {
                "decrease_price_cents": cents,
                "adjusted_executable_price": None
                if adjusted_price is None
                else str(adjusted_price),
                "net_ev": None if scenario_net is None else str(scenario_net),
            }
        )
    dominant = None
    if all(value is not None for value in costs.values()):
        maximum = max(value for value in costs.values() if value is not None)
        dominant = [key for key, value in costs.items() if value == maximum and maximum > 0]
    return {
        "frozen_decision_inputs": {
            **{
                key: decision.get(key)
                for key in (
                    "ticker",
                    "event_id",
                    "category",
                    "model_name",
                    "decision_at",
                    "side",
                    "forecast_probability",
                    "executable_price",
                    "estimated_fee",
                    "slippage",
                    "uncertainty",
                )
            },
            "settings": {"paper_min_edge": settings.get("paper_min_edge")}
            if isinstance(settings, dict)
            else {},
        },
        **{key: None if value is None else str(value) for key, value in values.items()},
        "diagnostic_basis": "UNVERIFIED_FROZEN_DECISION_INPUTS",
        "decision_at": decision.get("decision_at"),
        "side": side,
        "current_execution_ev": None,
        "dominant_costs": dominant,
        "strictly_above_recorded_threshold": None
        if net is None or threshold is None
        else net > threshold,
        "counterfactual": {
            "basis": "FROZEN_PROBABILITY_AND_CONSTANT_RECORDED_COSTS",
            "requires_new_decision": True,
            "caveat": (
                "Recorded costs held constant; fees may change with price. "
                "No liquidity or fill prediction."
            ),
            "probability_increases": probability_scenarios,
            "price_decreases": price_scenarios,
            "strict_price_ceiling": None if ceiling is None else str(ceiling),
            "net_ev_if_fees_zero": None
            if net is None or costs["fees"] is None
            else str(net + costs["fees"]),
        },
    }


def summarize_funnel(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Sequential counts only for stages actually verified by this scan."""
    predicates = (
        ("unique_preparation", lambda row: row.get("preparation_present") is True),
        ("rule_and_timing_verified", lambda row: row.get("rule_status") == "CERTIFIED"),
        (
            "original_provenance_verified",
            lambda row: row.get("source_status") == "FRESH_ORIGINALS_VERIFIED",
        ),
        ("model_evaluation_verified", lambda row: row.get("model_evaluation_verified") is True),
        ("selected_for_book_refresh", lambda row: row.get("selected_for_book_refresh") is True),
        ("refreshed_executable_book", lambda row: row.get("book_status") == "EXECUTABLE"),
        ("current_decision_qualified", lambda row: False),
    )
    retained = rows
    stages = [("reported_candidates", len(rows))]
    for stage, predicate in predicates:
        retained = [row for row in retained if predicate(row)]
        stages.append((stage, len(retained)))
    output = []
    previous = None
    for stage, count in stages:
        output.append(
            {
                "stage": stage,
                "count": count,
                "conversion_from_previous": (
                    None if previous is None or previous == 0 else count / previous
                ),
            }
        )
        previous = count
    by_category: dict[str, Any] = {}
    for category in sorted({str(row.get("category") or "UNKNOWN") for row in rows}):
        category_rows = [row for row in rows if str(row.get("category") or "UNKNOWN") == category]
        summary = {"reported_candidates": len(category_rows)}
        for field in ("gross_edge", "after_fee_ev", "net_ev"):
            numbers = [_number(row.get(field)) for row in category_rows]
            summary["positive_" + field] = sum(value is not None and value > 0 for value in numbers)
            summary["unknown_" + field] = sum(value is None for value in numbers)
        by_category[category] = summary
    return {
        "stages": output,
        "first_blocker_counts": dict(
            sorted(Counter(str(row.get("first_blocker") or "UNKNOWN") for row in rows).items())
        ),
        "frozen_economics": {
            "by_category": by_category,
            "complete": sum(row.get("net_ev") is not None for row in rows),
            "unknown": sum(row.get("net_ev") is None for row in rows),
            "above_recorded_threshold": sum(
                row.get("strictly_above_recorded_threshold") is True for row in rows
            ),
            "authority": "DIAGNOSTIC_ONLY",
        },
    }
