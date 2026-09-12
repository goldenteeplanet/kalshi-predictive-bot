from decimal import Decimal

import pytest

from kalshi_predictor.crypto.ev_funnel import build_funnel


def case(value=None, status="UNKNOWN"):
    component = {
        "value": value,
        "status": status,
        "method_version": "test-evidence",
        "evidence_hashes": ["a" * 64] if value is not None else [],
    }
    return {
        "gross_edge": "0.10",
        "probability": "0.60",
        "executable_price": "0.50",
        "event": "event1",
        "ticker": "ticker1",
        "model": "model1",
        "side": "YES",
        "decision_id": "decision1",
        "rule_version": "rule1",
        "fee": component.copy(),
        "slippage": component.copy(),
        "uncertainty": component.copy(),
        "blockers": ["RULE_UNCERTIFIED"],
    }


def test_unknown_costs_do_not_become_zero_or_rank_as_net_near_misses():
    result = build_funnel({"economics": [case()]}, "b" * 64)
    assert result["stages"]["gross_edge"]["positive"] == 1
    assert result["stages"]["full_net_ev"] == {"known": 0, "unknown": 1, "positive": 0}
    assert result["near_misses"] == []
    row = result["cases"][0]
    assert row["uncertainty"]["value"] is None
    assert row["worst_case_edge_before_fee_and_slippage"] == Decimal("-0.50")
    assert not row["paper_eligible"]


def test_known_economics_still_does_not_grant_admission():
    result = build_funnel({"economics": [case("0.01", "CERTIFIED")]}, "b" * 64)
    assert result["cases"][0]["full_net_ev"] == Decimal("0.07")
    assert result["full_net_gt_five_cents"]["positive"] == 1
    assert result["paper_eligible"] == 0
    assert result["phase_3n_allow"]["unknown"] == 1


def test_five_cent_threshold_is_strict():
    row = case("0.01", "CERTIFIED")
    row["uncertainty"]["value"] = "0.03"
    result = build_funnel({"economics": [row]}, "b" * 64)
    assert result["cases"][0]["full_net_ev"] == Decimal("0.05")
    assert result["full_net_gt_five_cents"]["positive"] == 0


def test_estimated_fee_does_not_establish_applicability():
    row = case("0.01", "ESTIMATED")
    result = build_funnel({"economics": [row]}, "b" * 64)
    assert result["stages"]["after_fee"]["unknown"] == 1


def test_frozen_gross_mismatch_is_rejected():
    row = case()
    row["gross_edge"] = "0.30"
    with pytest.raises(ValueError, match="ARITHMETIC_MISMATCH"):
        build_funnel({"economics": [row]}, "b" * 64)


def test_cases_do_not_inflate_event_count():
    result = build_funnel({"economics": [case(), case()]}, "b" * 64)
    assert result["event_n"] == 1
    assert result["independent_event_n"] is None
