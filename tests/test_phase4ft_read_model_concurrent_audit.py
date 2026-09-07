from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import kalshi_predictor.phase4cd.read_model_concurrent_audit as audit_module
import pytest
from kalshi_predictor.phase4cd.read_model_concurrent_audit import (
    ConcurrentReaderAuditError,
    audit_concurrent_readers,
)
from kalshi_predictor.phase4cd.read_model_crash_simulation import initialize_workspace

NOW = datetime(2026, 8, 27, tzinfo=UTC)


def test_concurrent_readers_see_only_complete_monotonic_artifacts(tmp_path: Path) -> None:
    result = _audit(_workspace(tmp_path), reader_count=8, reads_per_reader=40)
    assert result.verdict == "PASS"
    assert result.total_reads == 320
    assert result.final_sequence == 4
    assert all(tuple(sorted(trace.sequences)) == trace.sequences for trace in result.traces)


def test_exact_reader_and_read_bounds_pass(tmp_path: Path) -> None:
    result = _audit(
        _workspace(tmp_path),
        reader_count=4,
        reads_per_reader=10,
        max_readers=4,
        max_reads=10,
    )
    assert result.verdict == "PASS"


def test_reader_and_read_bound_overflow_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ConcurrentReaderAuditError, match="READER_BOUND_EXCEEDED"):
        _audit(_workspace(tmp_path), reader_count=5, reads_per_reader=1, max_readers=4)
    with pytest.raises(ConcurrentReaderAuditError, match="READ_BOUND_EXCEEDED"):
        _audit(
            _workspace(tmp_path, "phase4fs-read-overflow"),
            reader_count=1,
            reads_per_reader=11,
            max_reads=10,
        )


def test_empty_and_out_of_order_publication_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ConcurrentReaderAuditError, match="PUBLISH_SEQUENCE_EMPTY"):
        _audit(_workspace(tmp_path), publish_sequences=())
    with pytest.raises(ConcurrentReaderAuditError, match="PUBLISH_SEQUENCE_ORDER_INVALID"):
        _audit(_workspace(tmp_path, "phase4fs-order"), publish_sequences=(3, 2))


def test_exact_staleness_boundary_and_future_evidence_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ConcurrentReaderAuditError, match="EVIDENCE_STALE"):
        _audit(_workspace(tmp_path), now=NOW + timedelta(seconds=30))
    with pytest.raises(ConcurrentReaderAuditError, match="EVIDENCE_FUTURE_DATED"):
        _audit(_workspace(tmp_path, "phase4fs-future"), now=NOW - timedelta(seconds=1))


def test_missing_workspace_marker_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "phase4fs-unmarked"
    root.mkdir()
    with pytest.raises(ValueError, match="SIMULATION_MARKER_MISSING"):
        _audit(root)


def test_reader_partial_failure_is_captured_fail_closed(monkeypatch, tmp_path: Path) -> None:
    original = audit_module.inspect_workspace
    calls = 0

    def fail_once(root: Path, *, max_artifact_bytes: int):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected reader failure")
        return original(root, max_artifact_bytes=max_artifact_bytes)

    monkeypatch.setattr(audit_module, "inspect_workspace", fail_once)
    result = _audit(_workspace(tmp_path), reader_count=2, reads_per_reader=5)
    assert result.verdict == "FAIL_CLOSED"
    assert any("injected reader failure" in item for item in result.violations)


def test_audit_writes_only_marked_fixture_workspace(tmp_path: Path) -> None:
    sibling = tmp_path / "production.db"
    sibling.write_bytes(b"unchanged")
    _audit(_workspace(tmp_path))
    assert sibling.read_bytes() == b"unchanged"


def _audit(
    root: Path,
    *,
    reader_count: int = 3,
    reads_per_reader: int = 20,
    max_readers: int = 8,
    max_reads: int = 50,
    publish_sequences: tuple[int, ...] = (2, 3, 4),
    now: datetime = NOW,
):
    return audit_concurrent_readers(
        root,
        initial_sequence=1,
        publish_sequences=publish_sequences,
        reader_count=reader_count,
        reads_per_reader=reads_per_reader,
        max_readers=max_readers,
        max_reads_per_reader=max_reads,
        max_artifact_bytes=4096,
        evidence_generated_at=NOW,
        now=now,
        max_evidence_age_seconds=30,
    )


def _workspace(tmp_path: Path, name: str = "phase4fs-concurrent") -> Path:
    root = tmp_path / name
    initialize_workspace(root)
    return root
