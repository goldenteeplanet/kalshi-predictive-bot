from __future__ import annotations

from decimal import Decimal
from typing import Any

from kalshi_predictor.utils.decimals import to_decimal


def compare_forecast_rankings(
    baseline: list[dict[str, Any]], candidate: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compare exact ticker forecast probabilities and deterministic ranks."""
    baseline_by_ticker = {str(row["ticker"]): row for row in baseline}
    candidate_by_ticker = {str(row["ticker"]): row for row in candidate}
    shared = sorted(set(baseline_by_ticker) & set(candidate_by_ticker))
    baseline_rank = _ranks(baseline)
    candidate_rank = _ranks(candidate)
    rows = []
    for ticker in shared:
        old_probability = to_decimal(baseline_by_ticker[ticker].get("yes_probability"))
        new_probability = to_decimal(candidate_by_ticker[ticker].get("yes_probability"))
        probability_change = (
            new_probability - old_probability
            if old_probability is not None and new_probability is not None else None
        )
        rows.append({
            "ticker": ticker,
            "baseline_probability": str(old_probability) if old_probability is not None else None,
            "candidate_probability": str(new_probability) if new_probability is not None else None,
            "probability_change": (
                str(probability_change) if probability_change is not None else None
            ),
            "baseline_rank": baseline_rank[ticker],
            "candidate_rank": candidate_rank[ticker],
            "rank_change": baseline_rank[ticker] - candidate_rank[ticker],
        })
    return {
        "shared_tickers": len(shared),
        "added_tickers": sorted(set(candidate_by_ticker) - set(baseline_by_ticker)),
        "removed_tickers": sorted(set(baseline_by_ticker) - set(candidate_by_ticker)),
        "rank_changed": sum(row["rank_change"] != 0 for row in rows),
        "rows": rows,
    }


def _ranks(rows: list[dict[str, Any]]) -> dict[str, int]:
    ordered = sorted(
        rows,
        key=lambda row: (
            -(to_decimal(row.get("ranking_score")) or Decimal("0")),
            str(row["ticker"]),
        ),
    )
    return {str(row["ticker"]): index for index, row in enumerate(ordered, start=1)}
