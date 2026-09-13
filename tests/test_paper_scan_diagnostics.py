"""Frozen economic diagnostics must never manufacture current qualification."""

from dataclasses import replace

import pytest
from test_paper_release_qualified_scan import prepared
from test_paper_release_rules_timing import NOW

from kalshi_predictor.overnight_paper.qualified_scan import shortlist_candidates
from kalshi_predictor.overnight_paper.scan_diagnostics import frozen_economics, summarize_funnel


def economic_inputs():
    return dict(
        side="BUY_NO",
        forecast_probability="0.30",
        executable_price="0.60",
        estimated_fee="0.02",
        slippage="0.01",
        uncertainty="0.03",
        settings={"paper_min_edge": "0.04"},
        decision_at=NOW.isoformat(),
    )


def test_cost_decomposition_and_strict_counterfactual_are_frozen_only():
    row = frozen_economics(economic_inputs())
    assert row["probability"] == "0.70"
    assert row["gross_edge"] == "0.10"
    assert row["net_ev"] == "0.04"
    assert row["after_fee_ev"] == "0.08"
    assert row["dominant_costs"] == ["uncertainty"]
    assert row["strictly_above_recorded_threshold"] is False
    assert row["counterfactual"]["strict_price_ceiling"] == "0.60"
    assert row["counterfactual"]["net_ev_if_fees_zero"] == "0.06"
    assert row["current_execution_ev"] is None
    assert row["counterfactual"]["requires_new_decision"] is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("estimated_fee", None),
        ("slippage", "NaN"),
        ("uncertainty", "-0.1"),
        ("forecast_probability", True),
        ("executable_price", "Infinity"),
        ("side", "SELL_YES"),
    ],
)
def test_missing_or_invalid_evidence_never_becomes_zero(field, value):
    row = frozen_economics(economic_inputs() | {field: value})
    assert row["net_ev"] is None
    assert row["strictly_above_recorded_threshold"] is None
    assert row["counterfactual"]["strict_price_ceiling"] is None


def test_empty_registry_retains_candidate_blocker_and_economics_without_selection():
    item, _ = prepared()
    item = replace(item, decision=item.decision | economic_inputs())
    result = shortlist_candidates((item,), now=NOW, registry=())
    assert result["selected"] == []
    row = result["rows"][0]
    assert row["first_blocker"] == "NO_UNAMBIGUOUS_CERTIFIED_RULE"
    assert row["net_ev"] == "0.04"
    assert row["book_status"] == "NOT_FETCHED"
    assert row["size"] is row["risk"] is None
    funnel = result["funnel"]
    assert funnel["first_blocker_counts"] == {"NO_UNAMBIGUOUS_CERTIFIED_RULE": 1}
    assert [stage["count"] for stage in funnel["stages"]] == [1, 1, 0, 0, 0, 0, 0, 0]
    assert funnel["stages"][2]["conversion_from_previous"] == 0
    assert funnel["stages"][3]["conversion_from_previous"] is None


def test_ambiguous_unpinned_inputs_do_not_choose_economic_evidence():
    item, _ = prepared()
    result = shortlist_candidates((item, item), now=NOW, registry=())
    assert len(result["rows"]) == 1
    assert result["rows"][0]["first_blocker"] == "AMBIGUOUS_PREPARATION"
    assert result["funnel"]["frozen_economics"]["unknown"] == 1


def test_requested_numerical_counterfactuals_and_caps():
    row = frozen_economics(economic_inputs())
    assert [entry["net_ev"] for entry in row["counterfactual"]["probability_increases"]] == [
        "0.05",
        "0.06",
        "0.09",
    ]
    assert [entry["net_ev"] for entry in row["counterfactual"]["price_decreases"]] == [
        "0.05",
        "0.06",
    ]
    capped = frozen_economics(
        economic_inputs() | {"side": "BUY_YES", "forecast_probability": "0.99"}
    )
    assert all(
        entry["adjusted_side_probability"] == "1"
        for entry in capped["counterfactual"]["probability_increases"]
    )
    invalid = frozen_economics(economic_inputs() | {"executable_price": "0.01"})
    assert all(
        entry["adjusted_executable_price"] is None and entry["net_ev"] is None
        for entry in invalid["counterfactual"]["price_decreases"]
    )


def test_partial_costs_and_dominant_ties():
    row = frozen_economics(economic_inputs() | {"uncertainty": None})
    assert row["after_fee_ev"] == "0.08"
    assert row["dominant_costs"] is None
    assert all(entry["net_ev"] is None for entry in row["counterfactual"]["probability_increases"])
    tied = frozen_economics(economic_inputs() | {"uncertainty": "0.02"})
    assert tied["dominant_costs"] == ["fees", "uncertainty"]


def test_sequential_stages_intersect_and_category_economics_are_separate():
    malformed = dict(category="Weather", rule_status="CERTIFIED", book_status="EXECUTABLE")
    frozen = frozen_economics(economic_inputs()) | {"category": "Weather"}
    result = summarize_funnel([malformed, frozen, {"category": "Crypto"}])
    assert [stage["count"] for stage in result["stages"]] == [3, 0, 0, 0, 0, 0, 0, 0]
    weather = result["frozen_economics"]["by_category"]["Weather"]
    assert (
        weather["positive_gross_edge"]
        == weather["positive_after_fee_ev"]
        == weather["positive_net_ev"]
        == 1
    )
    assert weather["unknown_net_ev"] == 1
    assert result["frozen_economics"]["by_category"]["Crypto"]["unknown_gross_edge"] == 1
