from copy import deepcopy
from decimal import Decimal

import pytest

from kalshi_predictor.crypto.multiasset_tournament import MODELS, aggregate


def row(ident, event, p, y):
    return {
        "decision_id": ident,
        "event": event,
        "ticker": ident,
        "endpoint_hypothesis": "A",
        "outcome": y,
        "probabilities": dict.fromkeys(MODELS, p),
        "asset": "SOL",
        "target_at": "2026-09-12T03:00:00+00:00",
        "registered_lead_minutes": 10,
        "variance_per_second": 0.000000001,
        "spread": ".04",
    }


def overall(report):
    return next(g for g in report["groups"] if g["dimension"] == "overall")


def test_equal_event_weighting_and_honest_dependence():
    result = aggregate([row("a", "e1", ".1", 0), row("b", "e1", ".1", 0), row("c", "e2", ".8", 0)])
    group = overall(result)
    assert Decimal(group["models"][MODELS[0]]["brier"]) == Decimal(".325")
    assert group["events"] == 2
    assert len(group["simultaneous_time_clusters"]) == 1
    assert group["independent_event_n"] is None


def test_missing_model_excludes_identical_row_from_all_comparisons():
    missing = row("b", "e2", ".9", 0)
    missing["probabilities"][MODELS[3]] = None
    result = aggregate([row("a", "e1", ".2", 0), missing])
    assert result["matched_decisions"] == 1
    assert result["excluded_from_all_primary_models"][0]["decision_id"] == "b"
    assert all(Decimal(v["brier"]) == Decimal(".04") for v in overall(result)["models"].values())


def test_zero_probability_wrong_result_is_infinite_without_clipping():
    result = aggregate([row("a", "e", 0, 1)])
    assert overall(result)["models"][MODELS[0]]["log_loss"] == "POSITIVE_INFINITY"
    assert result["paper_pnl"] is None
    assert result["performance_promotion"] is False


def test_duplicates_fail_closed_but_different_endpoints_stay_separate():
    first = row("a", "e", ".5", 1)
    with pytest.raises(ValueError, match="DUPLICATE"):
        aggregate([first, first])
    second = deepcopy(first)
    second.update(decision_id="b", endpoint_hypothesis="B")
    result = aggregate([first, second])
    assert len([g for g in result["groups"] if g["dimension"] == "overall"]) == 2


def test_invalid_probability_or_outcome_cannot_enter_scores():
    bad = row("a", "e", "NaN", 0)
    with pytest.raises(ValueError, match="PROBABILITY"):
        aggregate([bad])
    bad = row("a", "e", ".5", True)
    with pytest.raises(ValueError, match="OUTCOME"):
        aggregate([bad])
