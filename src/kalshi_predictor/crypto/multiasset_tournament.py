"""Matched descriptive scoring; never certifies independent N or promotes paper."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from decimal import Decimal

MODELS = (
    "credible_book_midpoint_v1",
    "settlement_average_gaussian_v1",
    "settlement_average_student_t6_v1",
    "empirical_matched_average_v1",
    "existing_distribution_terminal_proxy_v1",
)


def _p(value):
    result = Decimal(str(value))
    if not result.is_finite() or not 0 <= result <= 1:
        raise ValueError("FINITE_PROBABILITY_REQUIRED")
    return result


def _metrics(rows, model):
    events = Counter(r["event"] for r in rows)
    bins = defaultdict(list)
    brier = Decimal(0)
    loss = 0.0
    infinite = False
    for row in rows:
        weight = Decimal(1) / len(events) / events[row["event"]]
        p = _p(row["probabilities"][model])
        y = row["outcome"]
        brier += weight * (p - y) ** 2
        assigned = p if y else 1 - p
        if assigned == 0:
            infinite = True
        else:
            loss += float(weight) * -float(assigned.ln())
        bins[min(int(p * 10), 9)].append((weight, p, y))
    ece = Decimal(0)
    bin_report = []
    for index in range(10):
        items = bins[index]
        mass = sum((w for w, _, _ in items), Decimal(0))
        p_sum = sum((w * p for w, p, _ in items), Decimal(0))
        y_sum = sum((w * y for w, _, y in items), Decimal(0))
        ece += abs(p_sum - y_sum)
        bin_report.append(
            {
                "index": index,
                "weight": str(mass),
                "probability": str(p_sum / mass) if mass else None,
                "outcome": str(y_sum / mass) if mass else None,
            }
        )
    return {
        "brier": str(brier),
        "log_loss": "POSITIVE_INFINITY" if infinite else loss,
        "ece": str(ece),
        "bins": bin_report,
    }


def segments(row):
    """Fixed pre-outcome bins; shared market probability keeps comparisons matched."""
    p = _p(row["probabilities"][MODELS[0]])
    variance = float(row["variance_per_second"])
    if not math.isfinite(variance) or variance < 0:
        raise ValueError("FINITE_NONNEGATIVE_VARIANCE_REQUIRED")
    hourly_sigma = math.sqrt(variance * 3600)
    spread = row.get("spread")
    liquidity = "UNKNOWN"
    if spread is not None:
        spread = Decimal(str(spread))
        if not spread.is_finite() or not 0 <= spread <= 1:
            raise ValueError("VALID_SPREAD_REQUIRED")
        liquidity = (
            "SPREAD_LE_2C"
            if spread <= Decimal(".02")
            else ("SPREAD_LE_5C" if spread <= Decimal(".05") else "SPREAD_GT_5C")
        )
    return {
        "asset": row["asset"],
        "horizon": str(row["registered_lead_minutes"]),
        "market_probability_band": str(min(int(p * 10), 9)),
        "liquidity_regime": liquidity,
        "volatility_regime": "HOUR_SIGMA_LT_0.25PCT"
        if hourly_sigma < 0.0025
        else ("HOUR_SIGMA_LT_1PCT" if hourly_sigma < 0.01 else "HOUR_SIGMA_GE_1PCT"),
    }


def aggregate(rows):
    """Rows must be authenticated prospective decisions with verified official labels.

    This pure calculator does not supply the artifact authentication layer.
    Missing any primary model excludes the decision from every primary comparison.
    Rule endpoint alternatives stay separate; no outcome-driven model substitution.
    """
    seen, contracts = set(), set()
    retained, excluded = [], []
    for row in rows:
        key = (row["endpoint_hypothesis"], row["event"], row["ticker"])
        if row["decision_id"] in seen or key in contracts:
            raise ValueError("DUPLICATE_DECISION_OR_CONTRACT_HYPOTHESIS")
        seen.add(row["decision_id"])
        contracts.add(key)
        if type(row["outcome"]) is not int or row["outcome"] not in (0, 1):
            raise ValueError("BINARY_OFFICIAL_OUTCOME_REQUIRED")
        missing = [m for m in MODELS if row["probabilities"].get(m) is None]
        if missing:
            excluded.append({"decision_id": row["decision_id"], "missing_models": missing})
            continue
        for model in MODELS:
            _p(row["probabilities"][model])
        retained.append(row)
    groups = defaultdict(list)
    for row in retained:
        endpoint = row["endpoint_hypothesis"]
        groups[(endpoint, "overall", "all")].append(row)
        for dimension, value in segments(row).items():
            groups[(endpoint, dimension, value)].append(row)
    reports = []
    for (endpoint, dimension, value), subset in sorted(groups.items()):
        metrics = {model: _metrics(subset, model) for model in MODELS}
        baseline = Decimal(metrics[MODELS[0]]["brier"])
        for model in MODELS:
            metrics[model]["brier_minus_market"] = str(Decimal(metrics[model]["brier"]) - baseline)
        reports.append(
            {
                "endpoint_hypothesis": endpoint,
                "dimension": dimension,
                "segment": value,
                "decisions": len(subset),
                "contracts": len({r["ticker"] for r in subset}),
                "events": len({r["event"] for r in subset}),
                "asset_hours": len({(r["asset"], r["target_at"]) for r in subset}),
                "simultaneous_time_clusters": sorted({r["target_at"] for r in subset}),
                "utc_day_clusters": sorted({r["target_at"][:10] for r in subset}),
                "independent_event_n": None,
                "models": metrics,
            }
        )
    return {
        "schema": "matched-multiasset-descriptive-tournament-v1",
        "groups": reports,
        "input_decisions": len(rows),
        "matched_decisions": len(retained),
        "excluded_from_all_primary_models": excluded,
        "weighting": "EQUAL_EVENT_THEN_EQUAL_CONTRACT_WITHIN_ENDPOINT_AND_SEGMENT",
        "ece": "FIXED_TENTHS_EVENT_WEIGHTED_DESCRIPTIVE_ONLY",
        "independent_event_n": None,
        "performance_promotion": False,
        "paper_pnl": None,
        "full_net_ev_certified": False,
        "limitations": [
            "SYNCHRONOUS_ASSETS_AND_ADJACENT_HOURS_MAY_BE_DEPENDENT",
            "ENDPOINT_GROUPS_ARE_EXPLICIT_UNCERTIFIED_FAMILY_HYPOTHESES",
            "EMPIRICAL_LOW_N_AND_INFINITE_LOG_LOSS_REMAIN_VISIBLE",
            "NO_WINNER_SIGNIFICANCE_OR_SEGMENT_PROMOTION_FROM_POINT_ESTIMATES",
        ],
    }
