"""Synthetic reviewed-policy fixtures; production calibration remains unreviewed."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.crypto import calibration_cost_evidence as calibration

AT = datetime(2026, 9, 12, 12, tzinfo=UTC)
PROTOCOL = b"SYNTHETIC PRECOMMITTED TEST PROTOCOL"
REVIEW = b"SYNTHETIC INDEPENDENCE REVIEW TEST ONLY"


def inputs(rows=None):
    rows = rows if rows is not None else [dict(
        event=f"event-{i}", cluster_id=f"cluster-{i}", model_version="TEST-MODEL",
        segment="TEST-SEGMENT", probability="0.5", outcome=i % 2,
        forecast_at=(AT - timedelta(hours=3)).isoformat(),
        target_at=(AT - timedelta(hours=2)).isoformat(),
        final_available_at=(AT - timedelta(hours=1)).isoformat(),
    ) for i in range(2)]
    dataset = json.dumps(rows).encode()
    hashes = [hashlib.sha256(b).hexdigest() for b in (dataset, PROTOCOL, REVIEW)]
    policy = calibration.ReviewedCalibrationPolicy(
        "TEST-MODEL", "TEST-SEGMENT", *hashes, AT - timedelta(hours=4),
        AT - timedelta(minutes=30), AT + timedelta(hours=1), 2, .05,
    )
    args = dict(model_version="TEST-MODEL", segment="TEST-SEGMENT", decision_at=AT,
                policy_version=policy.version, dataset=dataset, protocol=PROTOCOL,
                independence_review=REVIEW)
    return rows, policy, args


def test_caller_hashes_and_counts_do_not_create_independence():
    _, _, args = inputs()
    assert calibration.REVIEWED_CALIBRATION_POLICIES == ()
    result = calibration.verify_uncertainty_evidence(**args)
    assert result.value is None and not result.paper_support


def test_small_reviewed_fixture_gets_conservative_nonzero_penalty(monkeypatch):
    _, policy, args = inputs()
    monkeypatch.setattr(calibration, "REVIEWED_CALIBRATION_POLICIES", (policy,))
    result = calibration.verify_uncertainty_evidence(**args)
    assert result.value == Decimal(1)
    assert result.paper_support


@pytest.mark.parametrize("change", [
    {"segment": "OTHER"}, {"dataset": b"[]"},
    {"decision_at": AT + timedelta(hours=1)},
])
def test_scope_original_and_expiry_rejected(monkeypatch, change):
    _, policy, args = inputs()
    monkeypatch.setattr(calibration, "REVIEWED_CALIBRATION_POLICIES", (policy,))
    result = calibration.verify_uncertainty_evidence(**(args | change))
    assert result.value is None and not result.paper_support


def test_reviewed_dataset_still_rejects_outcome_before_forecast(monkeypatch):
    rows, _, _ = inputs()
    rows[0]["forecast_at"] = AT.isoformat()
    _, policy, args = inputs(rows)
    monkeypatch.setattr(calibration, "REVIEWED_CALIBRATION_POLICIES", (policy,))
    assert calibration.verify_uncertainty_evidence(**args).value is None


def test_insufficient_independent_events_stays_unknown(monkeypatch):
    _, policy, args = inputs()
    policy = replace(policy, minimum_independent_events=3)
    args["policy_version"] = policy.version
    monkeypatch.setattr(calibration, "REVIEWED_CALIBRATION_POLICIES", (policy,))
    result = calibration.verify_uncertainty_evidence(**args)
    assert result.value is None
    assert result.blockers == ("INSUFFICIENT_INDEPENDENT_CALIBRATION_EVIDENCE",)
