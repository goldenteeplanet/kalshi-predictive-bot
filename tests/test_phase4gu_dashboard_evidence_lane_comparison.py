from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.ui.dashboard_evidence_lane_comparison import (
    DashboardEvidenceLaneComparisonError,
    compare_dashboard_evidence_lanes,
    make_dashboard_evidence_lane,
    validate_dashboard_evidence_lane_comparison,
)


def test_consistent_lanes_are_deterministic_and_read_only() -> None:
    first = _compare(list(reversed(_lanes())))
    second = _compare(_lanes())
    validate_dashboard_evidence_lane_comparison(first)
    assert first.status == "CONSISTENT"
    assert first.agreement_groups == (("exchange", "local"),)
    assert first.comparison_hash == second.comparison_hash
    assert first.execution_authorized is False


def test_empty_partial_and_lane_bound_fail_closed() -> None:
    with pytest.raises(DashboardEvidenceLaneComparisonError, match="LANES_EMPTY"):
        _compare([])
    partial = _compare([_lane("local")])
    assert partial.status == "INCOMPLETE"
    assert partial.reasons == ("LANE_MISSING:exchange",)
    with pytest.raises(DashboardEvidenceLaneComparisonError, match="LANE_BOUND_EXCEEDED"):
        _compare([*_lanes(), _lane("third")], max_lanes=2)


def test_exact_freshness_boundary_is_consistent_and_one_second_over_is_stale() -> None:
    assert _compare(_lanes(age=300)).status == "CONSISTENT"
    stale = _compare(_lanes(age=301))
    assert stale.status == "STALE"
    assert stale.reasons == ("LANE_EVIDENCE_STALE",)


def test_divergence_and_incomplete_lane_are_preserved() -> None:
    divergent = _compare([_lane("local"), _lane("exchange", verdict="SETTLED:NO")])
    assert divergent.status == "DIVERGENT"
    assert len(divergent.agreement_groups) == 2
    incomplete = _compare([_lane("local"), _lane("exchange", complete=False)])
    assert incomplete.status == "INCOMPLETE"
    assert incomplete.reasons == ("LANE_INCOMPLETE:exchange",)


def test_malformed_duplicate_subject_and_tampering_fail_closed() -> None:
    with pytest.raises(DashboardEvidenceLaneComparisonError, match="LANE_FIELD_INVALID"):
        _lane("local", age=-1)
    with pytest.raises(DashboardEvidenceLaneComparisonError, match="LANE_ID_DUPLICATE"):
        _compare([_lane("local"), _lane("local")])
    with pytest.raises(DashboardEvidenceLaneComparisonError, match="LANE_SUBJECT_MIXED"):
        _compare([_lane("local"), _lane("exchange", subject="other")])
    item = _lane("local")
    with pytest.raises(DashboardEvidenceLaneComparisonError, match="LANE_HASH_MISMATCH"):
        _compare([replace(item, verdict="SETTLED:NO"), _lane("exchange")])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    result = _compare(_lanes())
    with pytest.raises(DashboardEvidenceLaneComparisonError, match="COMPARISON_HASH_MISMATCH"):
        validate_dashboard_evidence_lane_comparison(replace(result, comparison_hash="0" * 64))
    with pytest.raises(
        DashboardEvidenceLaneComparisonError, match="COMPARISON_SAFETY_BOUNDARY_INVALID"
    ):
        validate_dashboard_evidence_lane_comparison(replace(result, execution_authorized=True))


def test_comparison_has_no_query_publication_or_mutation_surface() -> None:
    names = set(compare_dashboard_evidence_lanes.__code__.co_names)
    assert names.isdisjoint({"commit", "connect", "execute", "open", "publish", "unlink", "write"})


def _lane(lane_id, *, verdict="SETTLED:YES", age=1, complete=True, subject="order:204"):
    return make_dashboard_evidence_lane(
        lane_id=lane_id,
        subject_id=subject,
        verdict=verdict,
        value_hash="value:" + verdict,
        source_identity_hash="source:" + lane_id,
        source_watermark="w",
        lineage_hash="lineage:231:231",
        evidence_age_seconds=age,
        complete=complete,
    )


def _lanes(*, age=1):
    return [_lane("local", age=age), _lane("exchange", age=age)]


def _compare(lanes, **kwargs):
    return compare_dashboard_evidence_lanes(
        lanes, expected_lane_ids=("local", "exchange"), **kwargs
    )
