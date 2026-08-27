from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.workstation.keepalive_gap_classifier import (
    KeepaliveGapClassifierError,
    classify_keepalive_gaps,
    make_keepalive_sample,
    validate_keepalive_gap_classification,
)


def test_healthy_samples_are_deterministic_and_read_only() -> None:
    first = classify_keepalive_gaps(list(reversed(_samples())))
    second = classify_keepalive_gaps(_samples())
    validate_keepalive_gap_classification(first)
    assert first.status == "HEALTHY"
    assert first.classification_hash == second.classification_hash
    assert first.host_restart_authorized is False


def test_exact_gap_boundary_passes_and_excess_requires_alert() -> None:
    assert classify_keepalive_gaps([_sample(1, 100), _sample(2, 160)]).status == "HEALTHY"
    result = classify_keepalive_gaps([_sample(1, 100), _sample(2, 161)])
    assert result.status == "GAP_DETECTED"
    assert result.gap_count == 1
    assert result.alert_required is True
    assert result.recovery_authorized is False


def test_empty_resource_and_numeric_bounds_fail_closed() -> None:
    with pytest.raises(KeepaliveGapClassifierError, match="SAMPLES_EMPTY"):
        classify_keepalive_gaps([])
    with pytest.raises(KeepaliveGapClassifierError, match="SAMPLE_BOUND_EXCEEDED"):
        classify_keepalive_gaps(_samples(), max_samples=1)
    with pytest.raises(KeepaliveGapClassifierError, match="CLASSIFIER_BOUND_INVALID"):
        classify_keepalive_gaps(_samples(), max_gap_seconds=True)


def test_exact_freshness_boundary_passes_and_older_is_stale() -> None:
    exact = [_sample(1, 100, age=120), _sample(2, 130, age=120)]
    assert classify_keepalive_gaps(exact).status == "HEALTHY"
    stale = classify_keepalive_gaps([_sample(1, 100, age=121), _sample(2, 130)])
    assert stale.status == "STALE"
    assert stale.reasons == ("KEEPALIVE_EVIDENCE_STALE",)


def test_partial_missing_and_sequence_gap_evidence_is_incomplete() -> None:
    single = classify_keepalive_gaps([_sample(1, 100)])
    assert single.status == "INCOMPLETE"
    partial = classify_keepalive_gaps(
        [_sample(1, 100, present=False, complete=False), _sample(3, 130)]
    )
    assert partial.status == "INCOMPLETE"
    assert partial.missing_sample_count == 1
    assert "SAMPLE_SEQUENCE_GAP" in partial.reasons
    assert "KEEPALIVE_ABSENT:1" in partial.reasons


def test_malformed_lineage_sample_and_result_tampering_fail_closed() -> None:
    with pytest.raises(KeepaliveGapClassifierError, match="SAMPLE_TIME_NOT_MONOTONIC"):
        classify_keepalive_gaps([_sample(1, 100), _sample(2, 100)])
    with pytest.raises(KeepaliveGapClassifierError, match="SAMPLE_LINEAGE_MIXED"):
        classify_keepalive_gaps([_sample(1, 100), _sample(2, 130, source="b" * 64)])
    item = _sample(1, 100)
    with pytest.raises(KeepaliveGapClassifierError, match="SAMPLE_HASH_MISMATCH"):
        classify_keepalive_gaps([replace(item, present=False), _sample(2, 130)])
    result = classify_keepalive_gaps(_samples())
    with pytest.raises(KeepaliveGapClassifierError, match="CLASSIFICATION_HASH_MISMATCH"):
        validate_keepalive_gap_classification(replace(result, classification_hash="0" * 64))
    with pytest.raises(
        KeepaliveGapClassifierError, match="CLASSIFICATION_SAFETY_BOUNDARY_INVALID"
    ):
        validate_keepalive_gap_classification(replace(result, service_control_authorized=True))


def test_classifier_has_no_query_control_notification_or_mutation_surface() -> None:
    names = set(classify_keepalive_gaps.__code__.co_names)
    assert names.isdisjoint(
        {
            "Popen",
            "connect",
            "execute",
            "open",
            "restart",
            "shutdown",
            "start",
            "stop",
            "systemctl",
            "toast",
            "write",
        }
    )


def _sample(
    sequence: int,
    observed_at: int,
    *,
    present: bool = True,
    complete: bool = True,
    age: int = 1,
    source: str = "a" * 64,
):
    return make_keepalive_sample(
        sequence=sequence,
        observed_at_epoch_seconds=observed_at,
        present=present,
        complete=complete,
        evidence_age_seconds=age,
        source_identity_hash=source,
    )


def _samples():
    return [_sample(1, 100), _sample(2, 130)]
