"""Matched frozen score comparisons; no refitting or missing-value substitution."""

from collections import Counter
from math import isfinite


def matched_comparisons(rows, baselines, *, subset="all_named"):
    fields = {
        "all_named": None,
        "nonvacuous_midpoint": "nonvacuous_midpoint_comparison_eligible",
        "book_midpoint": "book_midpoint_comparison_eligible",
    }
    if subset not in fields:
        raise ValueError("UNKNOWN_COMPARISON_SUBSET")
    scored = {}
    for row in rows:
        if row.get("scores") is None:
            continue
        key = row["ticker"], row["model"]
        if (
            any(
                not row.get(k)
                for k in ("decision_at", "prediction_sha256", "outcome_original_sha256")
            )
            or type(row.get("outcome")) is not int
            or row["outcome"] not in (0, 1)
        ):
            raise ValueError("MISSING_SCORE_PROVENANCE")
        if key in scored:
            raise ValueError("DUPLICATE_FORECAST_MODEL_SCORE")
        brier = row["scores"]["brier"]
        if (
            isinstance(brier, bool)
            or not isinstance(brier, float | int)
            or not isfinite(brier)
            or not 0 <= brier <= 1
        ):
            raise ValueError("INVALID_BRIER_SCORE")
        scored[key] = row
    field = fields[subset]
    eligible = set()
    excluded: Counter[str] = Counter()
    for ticker, baseline in baselines.items():
        quality = baseline.get("quote_quality", {})
        if field is None or quality.get(field) is True:
            eligible.add(ticker)
        else:
            excluded[quality.get("label", "QUALITY_NOT_CAPTURED")] += 1
    results = {}
    for model in sorted({model for _, model in scored} - {"market_implied_v1"}):
        pairs = []
        for ticker in sorted(eligible):
            candidate = scored.get((ticker, model))
            market = scored.get((ticker, "market_implied_v1"))
            if candidate is None or market is None:
                continue
            if any(
                candidate.get(k) != market.get(k)
                for k in ("outcome", "decision_at", "prediction_sha256", "outcome_original_sha256")
            ):
                raise ValueError("UNMATCHED_FORECAST_OUTCOME_PROVENANCE")
            pairs.append((ticker, candidate["scores"]["brier"], market["scores"]["brier"]))
        count = len(pairs)
        results[model] = {
            "matched_tickers": [p[0] for p in pairs],
            "matched_contracts": count,
            "model_mean_brier": sum(p[1] for p in pairs) / count if count else None,
            "market_mean_brier": sum(p[2] for p in pairs) / count if count else None,
            "missing_matched_scores": len(eligible) - count,
        }
    return {
        "subset": subset,
        "eligible_contracts": len(eligible),
        "excluded_by_quote_quality": dict(excluded),
        "comparisons": results,
        "independence_warning": (
            "Strikes and repeated origins can share outcomes; counts are not independent trials"
        ),
        "promotion_authority": False,
    }
