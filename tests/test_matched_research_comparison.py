import copy

import pytest

from kalshi_predictor.crypto.matched_research_comparison import matched_comparisons


def row(ticker, model, brier):
    return dict(
        ticker=ticker,
        model=model,
        outcome=0,
        decision_at="2026-09-11T16:00:00Z",
        prediction_sha256=ticker * 64,
        outcome_original_sha256="f" * 64,
        scores=dict(brier=brier),
    )


def test_missing_model_is_not_zero_and_both_means_use_same_contracts():
    rows = [
        row("a", "gaussian", 0.04),
        row("a", "market_implied_v1", 0.09),
        row("b", "market_implied_v1", 0.81),
    ]
    original = copy.deepcopy(rows)
    result = matched_comparisons(rows, {"a": {}, "b": {}})["comparisons"]["gaussian"]
    assert result["matched_tickers"] == ["a"]
    assert result["model_mean_brier"] == 0.04
    assert result["market_mean_brier"] == 0.09
    assert result["missing_matched_scores"] == 1
    assert rows == original


def test_quality_missing_is_excluded_and_real_fifty_percent_is_retained():
    rows = [row(t, m, 0.25) for t in ("a", "b", "c") for m in ("gaussian", "market_implied_v1")]
    baselines = {
        "a": {
            "quote_quality": {"label": "BOOK_TWO_SIDED", "book_midpoint_comparison_eligible": True}
        },
        "b": {"quote_quality": {"label": "LISTING_FULL_RANGE"}},
        "c": {},
    }
    result = matched_comparisons(rows, baselines, subset="book_midpoint")
    assert result["comparisons"]["gaussian"]["matched_tickers"] == ["a"]
    assert result["excluded_by_quote_quality"] == {
        "LISTING_FULL_RANGE": 1,
        "QUALITY_NOT_CAPTURED": 1,
    }
    assert not result["promotion_authority"]


def test_duplicate_or_mismatched_outcome_rejected():
    model = row("a", "gaussian", 0.04)
    market = row("a", "market_implied_v1", 0.09)
    with pytest.raises(ValueError, match="DUPLICATE"):
        matched_comparisons([model, model], {"a": {}})
    market["outcome_original_sha256"] = "different"
    with pytest.raises(ValueError, match="PROVENANCE"):
        matched_comparisons([model, market], {"a": {}})


def test_empty_subset_reports_null_scores():
    result = matched_comparisons([row("a", "gaussian", 0.04)], {"a": {}}, subset="book_midpoint")
    assert result["comparisons"]["gaussian"]["model_mean_brier"] is None


def test_missing_provenance_cannot_match_two_incomplete_rows():
    candidate = row("a", "gaussian", 0.04)
    del candidate["prediction_sha256"]
    with pytest.raises(ValueError, match="MISSING_SCORE_PROVENANCE"):
        matched_comparisons([candidate], {"a": {}})
