from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from kalshi_predictor.crypto.research_costs import DependenceCounts, EvidenceStatus
from kalshi_predictor.crypto.research_uncertainty import CalibrationCluster, uncertainty_allowance

NOW = datetime(2026, 9, 12, tzinfo=UTC)
HASH = "a" * 64


def args(n=30):
    return dict(
        clusters=tuple(
            CalibrationCluster(str(i), D(0), NOW - timedelta(days=1), HASH) for i in range(n)
        ),
        counts=DependenceCounts(n, n, n, n, n),
        decision_at=NOW,
        minimum_independent_events=30,
        alpha=0.05,
        independence_review_sha256=HASH,
        segment_protocol_sha256=HASH,
    )


def test_low_n_and_unknown_independence_block():
    assert uncertainty_allowance(**args(5)).status == EvidenceStatus.UNKNOWN
    values = args()
    values["counts"] = DependenceCounts(30, 30, 30, None, 1)
    assert uncertainty_allowance(**values).value is None


def test_bound_decreases_with_evidence_and_never_becomes_zero():
    small = uncertainty_allowance(**args(30))
    large = uncertainty_allowance(**args(300))
    assert small.value is not None and large.value is not None
    assert D(".49") < small.value < D(".50")
    assert D(0) < large.value < small.value
    assert large.status == EvidenceStatus.ESTIMATED


def test_no_outcome_leakage_or_duplicate_clusters():
    values = args()
    values["clusters"] = (CalibrationCluster("future", D(0), NOW, HASH),) + values["clusters"][1:]
    with pytest.raises(ValueError, match="PREDECISION"):
        uncertainty_allowance(**values)
    values = args()
    values["clusters"] = (values["clusters"][1],) + values["clusters"][1:]
    with pytest.raises(ValueError, match="DISTINCT"):
        uncertainty_allowance(**values)
