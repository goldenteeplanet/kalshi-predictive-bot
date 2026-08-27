from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.phase4cd.read_model_staleness import (
    StalenessEscalationError,
    build_staleness_evidence,
    classify_staleness,
)

NOW = datetime(2026, 8, 27, tzinfo=UTC)


def test_fresh_and_warning_states() -> None:
    assert _classify(_evidence(snapshot_age=9, progress_age=9)).status == "FRESH"
    warning = _classify(_evidence(snapshot_age=10, progress_age=10))
    assert warning.status == "WARNING"
    assert warning.action == "MONITOR_CLOSELY"


def test_exact_stale_and_stall_boundaries() -> None:
    stale = _classify(_evidence(snapshot_age=20, progress_age=20))
    assert stale.status == "STALE"
    stalled = _classify(_evidence(snapshot_age=1, progress_age=30))
    assert stalled.status == "STALLED"
    assert stalled.action == "ESCALATE_RECONCILIATION"


def test_lineage_failure_has_highest_precedence() -> None:
    evidence = _evidence(snapshot_age=100, progress_age=100, lineage=False)
    result = _classify(evidence)
    assert result.status == "LINEAGE_FAILURE"
    assert result.action == "ESCALATE_CRITICAL"


def test_empty_and_partial_evidence_fail_closed() -> None:
    with pytest.raises(StalenessEscalationError, match="EVIDENCE_FIELDS_INVALID"):
        _classify({})
    partial = _evidence(snapshot_age=1, progress_age=1)
    partial.pop("watermark")
    with pytest.raises(StalenessEscalationError, match="EVIDENCE_FIELDS_INVALID"):
        _classify(partial)


def test_tampering_and_malformed_timestamp_fail_closed() -> None:
    tampered = _evidence(snapshot_age=1, progress_age=1)
    tampered["watermark"] = "paper_pnl:999"
    with pytest.raises(StalenessEscalationError, match="EVIDENCE_HASH_MISMATCH"):
        _classify(tampered)
    malformed = _evidence(snapshot_age=1, progress_age=1)
    malformed["snapshot_generated_at"] = "not-a-time"
    with pytest.raises(StalenessEscalationError, match="EVIDENCE_TIMESTAMP_INVALID"):
        _classify(malformed)


def test_future_snapshot_and_progress_fail_closed() -> None:
    with pytest.raises(StalenessEscalationError, match="SNAPSHOT_FUTURE_DATED"):
        _classify(_evidence(snapshot_age=-1, progress_age=1))
    with pytest.raises(StalenessEscalationError, match="PROGRESS_FUTURE_DATED"):
        _classify(_evidence(snapshot_age=1, progress_age=-1))


@pytest.mark.parametrize(
    ("warning", "stale", "stall"),
    [(0, 20, 30), (10, 10, 30), (20, 10, 30), (10, 20, 0)],
)
def test_invalid_thresholds_fail_closed(warning: int, stale: int, stall: int) -> None:
    with pytest.raises(StalenessEscalationError, match="THRESHOLD_INVALID"):
        classify_staleness(
            _evidence(snapshot_age=1, progress_age=1),
            now=NOW,
            warning_age_seconds=warning,
            stale_age_seconds=stale,
            stall_age_seconds=stall,
        )


def test_classifier_is_pure_and_exposes_no_writer_surface() -> None:
    evidence = _evidence(snapshot_age=1, progress_age=1)
    original = copy.deepcopy(evidence)
    _classify(evidence)
    assert evidence == original
    assert "execute" not in dir(classify_staleness)
    assert "commit" not in dir(classify_staleness)


def _classify(evidence: dict):
    return classify_staleness(
        evidence,
        now=NOW,
        warning_age_seconds=10,
        stale_age_seconds=20,
        stall_age_seconds=30,
    )


def _evidence(*, snapshot_age: int, progress_age: int, lineage: bool = True) -> dict:
    return build_staleness_evidence(
        snapshot_generated_at=NOW - timedelta(seconds=snapshot_age),
        last_progress_at=NOW - timedelta(seconds=progress_age),
        lineage_valid=lineage,
        source_identity_hash="a" * 64,
        watermark="paper_pnl:140680",
    )
