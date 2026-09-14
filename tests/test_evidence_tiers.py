from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.crypto.evidence_tiers import (
    EvidenceTier,
    ModelEvidence,
    TierPolicy,
    classify_evidence,
    conservative_diagnostic_allowance,
)

NOW = datetime(2026, 9, 12, tzinfo=UTC)
POLICY = TierPolicy((2, 30, 100, 500), frozenset({"SOL"}), NOW, "a" * 64)


def evidence(n):
    return ModelEvidence(
        n,
        frozenset({"SOL"}),
        True,
        NOW + timedelta(hours=1),
        NOW + timedelta(hours=2),
        "b" * 64,
        True,
        "c" * 64,
        True,
        "d" * 64,
        True,
        "e" * 64,
    )


def classify(value):
    return classify_evidence(POLICY, value, decision_at=NOW + timedelta(hours=3))


@pytest.mark.parametrize(
    "n,tier",
    [
        (None, "INSUFFICIENT"),
        (1, "INSUFFICIENT"),
        (2, "EARLY"),
        (30, "PRELIMINARY"),
        (100, "USEFUL"),
        (500, "STRONG"),
    ],
)
def test_predeclared_milestones(n, tier):
    assert classify(evidence(n)) == tier


@pytest.mark.parametrize(
    "changes",
    [
        {"prospective_only": False},
        {"assets": frozenset({"BTC"})},
        {"independence_review_sha256": None},
    ],
)
def test_large_count_cannot_replace_provenance(changes):
    assert classify(replace(evidence(10000), **changes)) == EvidenceTier.INSUFFICIENT


def test_missing_reviews_cap_tier():
    assert classify(replace(evidence(10000), calibration_review_sha256=None)) == "EARLY"
    assert classify(replace(evidence(10000), segment_stable=False)) == "PRELIMINARY"
    assert classify(replace(evidence(10000), replication_passed=False)) == "USEFUL"


def test_retroactive_policy_and_future_evidence_rejected():
    with pytest.raises(ValueError, match="POLICY_MUST"):
        classify(replace(evidence(100), first_forecast_at=NOW - timedelta(seconds=1)))
    with pytest.raises(ValueError, match="PREDECISION"):
        classify(replace(evidence(100), evidence_available_at=NOW + timedelta(hours=3)))


def test_worst_case_haircut_prevents_positive_edge_without_skill():
    for probability in (Decimal(0), Decimal(".6"), Decimal(1)):
        bound = conservative_diagnostic_allowance(probability=probability, evidence_sha256="a" * 64)
        assert bound.value == probability
        assert probability - Decimal(".4") - bound.value == Decimal("-.4")
        assert "NOT_CALIBRATED" in bound.reason
