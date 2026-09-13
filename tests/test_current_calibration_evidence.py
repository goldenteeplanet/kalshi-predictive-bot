"""Synthetic registry fixtures exercise replay only; no production policy added."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.crypto import calibration_cost_evidence as calibration
from kalshi_predictor.crypto.current_calibration_evidence import (
    CurrentCalibrationOriginals,
    assess_current_conditional_calibration,
)
from kalshi_predictor.crypto.current_market_scan import evaluate_paginated_current_research


def fixture(at, model="TEST-MODEL"):
    rows = [
        dict(
            event=f"test-event-{i}",
            cluster_id=f"test-cluster-{i}",
            model_version=model,
            segment="TEST-ONLY-SEGMENT",
            probability="0.5",
            outcome=i % 2,
            forecast_at=(at - timedelta(hours=3)).isoformat(),
            target_at=(at - timedelta(hours=2)).isoformat(),
            final_available_at=(at - timedelta(hours=1)).isoformat(),
        )
        for i in range(2)
    ]
    originals = (json.dumps(rows).encode(), b"TEST-FROZEN-PROTOCOL", b"TEST-INDEPENDENCE-REVIEW")
    policy = calibration.ReviewedCalibrationPolicy(
        model,
        "TEST-ONLY-SEGMENT",
        *(hashlib.sha256(raw).hexdigest() for raw in originals),
        at - timedelta(hours=4),
        at - timedelta(minutes=30),
        at + timedelta(hours=1),
        2,
        0.05,
    )
    return policy, CurrentCalibrationOriginals(policy.version, policy.segment, *originals)


def request():
    from test_public_paper_costs import AT, TICKER, authority, book

    policy, originals = fixture(AT)
    return policy, dict(
        originals=originals,
        ticker=TICKER,
        event_id="KXSOLE-26SEP1317",
        series="KXSOLE",
        model_version="TEST-MODEL",
        side="YES",
        selected_probability=Decimal(".60"),
        executable_price=Decimal(".39"),
        decision_at=AT,
        book=book(),
        fee_originals=authority(),
    )


def test_unregistered_originals_remain_unknown_with_hash_provenance():
    _, args = request()
    assert calibration.REVIEWED_CALIBRATION_POLICIES == ()
    result = assess_current_conditional_calibration(**args)
    assert result["status"] == "UNKNOWN"
    assert result["conditional_uncertainty_value"] is result["conditional_net_ev"] is None
    component = result["uncertainty_evidence"]
    assert hashlib.sha256(args["originals"].dataset).hexdigest() in json.dumps(component)
    assert "INDEPENDENT_PROSPECTIVE_CALIBRATION_REVIEW_MISSING" in result["blockers"]


def test_supported_test_registry_consumes_actual_cost_engine_without_promotion(monkeypatch):
    policy, args = request()
    monkeypatch.setattr(calibration, "REVIEWED_CALIBRATION_POLICIES", (policy,))
    result = assess_current_conditional_calibration(**args)
    assert result["conditional_uncertainty_value"] == "1"
    assert Decimal(result["conditional_net_ev"]) == Decimal("-.81")
    assert (
        result["uncertainty_evidence"]["method"] == "ABS_CLUSTER_BIAS_PLUS_HOEFFDING_RANGE_TWO_V1"
    )
    assert result["candidate_applicability"] is result["paper_eligible"] is False
    assert "CALIBRATION_CANDIDATE_APPLICABILITY_NOT_ESTABLISHED" in result["blockers"]
    assert "RULE_UNCERTIFIED" in result["blockers"]


def test_no_side_uses_selected_probability_and_actual_yes_book_depth(monkeypatch):
    policy, args = request()
    monkeypatch.setattr(calibration, "REVIEWED_CALIBRATION_POLICIES", (policy,))
    args.update(side="NO", selected_probability=Decimal(".40"), executable_price=Decimal(".65"))
    result = assess_current_conditional_calibration(**args)
    assert Decimal(result["conditional_net_ev"]) == Decimal("-1.27")
    assert result["scenario_scope_verified"] is result["holdout_transportability_verified"] is False
    assert result["dataset_replay_scope"] == "PINNED_FLAT_ROWS_NOT_AUTOMATIC_SOURCE_RECEIPT_REPLAY"


@pytest.mark.parametrize("change", ["model", "segment", "dataset", "expired"])
def test_actual_verifier_scope_original_and_time_rejections(monkeypatch, change):
    policy, args = request()
    monkeypatch.setattr(calibration, "REVIEWED_CALIBRATION_POLICIES", (policy,))
    if change == "model":
        args["model_version"] = "OTHER"
    elif change == "segment":
        args["originals"] = replace(args["originals"], segment="OTHER")
    elif change == "dataset":
        args["originals"] = replace(args["originals"], dataset=b"[]")
    else:
        args["decision_at"] += timedelta(hours=1)
    result = assess_current_conditional_calibration(**args)
    assert result["conditional_uncertainty_value"] is result["conditional_net_ev"] is None
    assert result["status"] == "UNKNOWN"


def test_malformed_pinned_test_dataset_is_unknown_not_unhandled(monkeypatch):
    policy, args = request()
    policy = replace(policy, dataset_sha256=hashlib.sha256(b"{").hexdigest())
    args["originals"] = replace(args["originals"], dataset=b"{", policy_version=policy.version)
    monkeypatch.setattr(calibration, "REVIEWED_CALIBRATION_POLICIES", (policy,))
    result = assess_current_conditional_calibration(**args)
    assert result["status"] == "UNKNOWN"
    assert result["blockers"][0].startswith("CALIBRATION_ORIGINAL_REPLAY_REJECTED:")


def test_scanner_wires_real_verifier_and_keeps_canonical_full_net_unknown(monkeypatch):
    from test_current_market_scan import intake_scan_request

    from kalshi_predictor.crypto.current_research_intake import MODEL

    args = intake_scan_request()
    ticker = next(iter(args["research_inputs"]))
    policy, bundle = fixture(args["assessed_at"], MODEL)
    monkeypatch.setattr(calibration, "REVIEWED_CALIBRATION_POLICIES", (policy,))
    args["calibration_originals"] = {ticker: bundle}
    report = evaluate_paginated_current_research(**args)
    row = report["rows"][0]
    assert row["conditional_calibration"]["conditional_uncertainty_value"] == "1"
    assert row["uncertainty"] is row["full_net_ev"] is None
    assert row["calibrated"] is row["paper_eligible"] is row["rule_certified"] is False
    assert report["funnel"]["conditional_uncertainty_supported"] == 1
    assert report["funnel"]["uncertainty_known"] == report["funnel"]["full_net_gt_5c"] == 0


def test_calibration_bundle_outside_discovery_is_rejected():
    _, args = request()
    with pytest.raises(ValueError, match="OUTSIDE_VERIFIED_DISCOVERY"):
        evaluate_paginated_current_research(
            discovery_pages={},
            books={},
            fee_originals={},
            assessed_at=args["decision_at"],
            calibration_originals={"absent": args["originals"]},
        )


def test_opaque_claimed_reserve_is_not_an_input():
    _, args = request()
    with pytest.raises(ValueError, match="EXACT_CALIBRATION_ORIGINAL"):
        assess_current_conditional_calibration(
            **(args | {"originals": {"reserve": 0, "paper_support": True}})
        )
