from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Lock

from kalshi_predictor.phase4cd.read_model_crash_simulation import (
    build_simulated_artifact,
    inspect_workspace,
    simulate_publication,
)


class ConcurrentReaderAuditError(ValueError):
    """Stable fail-closed concurrent-reader audit error."""


@dataclass(frozen=True)
class ReaderTrace:
    reader_id: int
    sequences: tuple[int, ...]


@dataclass(frozen=True)
class ConcurrentReaderAuditResult:
    verdict: str
    reader_count: int
    total_reads: int
    initial_sequence: int
    final_sequence: int
    traces: tuple[ReaderTrace, ...]
    violations: tuple[str, ...]


def audit_concurrent_readers(
    root: Path,
    *,
    initial_sequence: int,
    publish_sequences: tuple[int, ...],
    reader_count: int,
    reads_per_reader: int,
    max_readers: int,
    max_reads_per_reader: int,
    max_artifact_bytes: int,
    evidence_generated_at: datetime,
    now: datetime,
    max_evidence_age_seconds: float,
) -> ConcurrentReaderAuditResult:
    _validate_bounds(
        reader_count=reader_count,
        reads_per_reader=reads_per_reader,
        max_readers=max_readers,
        max_reads_per_reader=max_reads_per_reader,
    )
    age = _age(evidence_generated_at, now)
    if max_evidence_age_seconds <= 0:
        raise ConcurrentReaderAuditError("EVIDENCE_AGE_BOUND_INVALID")
    if age < 0:
        raise ConcurrentReaderAuditError("EVIDENCE_FUTURE_DATED")
    if age >= max_evidence_age_seconds:
        raise ConcurrentReaderAuditError("EVIDENCE_STALE")
    if not publish_sequences:
        raise ConcurrentReaderAuditError("PUBLISH_SEQUENCE_EMPTY")
    expected = (initial_sequence, *publish_sequences)
    invalid_sequence = any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in expected
    )
    if invalid_sequence:
        raise ConcurrentReaderAuditError("SEQUENCE_INVALID")
    if tuple(sorted(set(expected))) != expected:
        raise ConcurrentReaderAuditError("PUBLISH_SEQUENCE_ORDER_INVALID")

    _publish(root, initial_sequence, max_artifact_bytes=max_artifact_bytes)
    start = Barrier(reader_count + 1)
    traces: list[ReaderTrace] = []
    violations: list[str] = []
    result_lock = Lock()

    def read_many(reader_id: int) -> None:
        observed: list[int] = []
        try:
            start.wait()
            for _ in range(reads_per_reader):
                state = inspect_workspace(root, max_artifact_bytes=max_artifact_bytes)
                if state.status != "CURRENT_ARTIFACT_VALID" or state.current_sequence is None:
                    raise ConcurrentReaderAuditError("READER_CURRENT_INVALID")
                observed.append(state.current_sequence)
        except Exception as exc:  # audit captures every reader failure as evidence
            with result_lock:
                violations.append(f"reader:{reader_id}:{type(exc).__name__}:{exc}")
            return
        with result_lock:
            traces.append(ReaderTrace(reader_id=reader_id, sequences=tuple(observed)))

    with ThreadPoolExecutor(max_workers=reader_count) as executor:
        futures = [executor.submit(read_many, reader_id) for reader_id in range(reader_count)]
        start.wait()
        for sequence in publish_sequences:
            _publish(root, sequence, max_artifact_bytes=max_artifact_bytes)
        for future in futures:
            future.result()

    final = inspect_workspace(root, max_artifact_bytes=max_artifact_bytes)
    if final.current_sequence != publish_sequences[-1]:
        violations.append("FINAL_SEQUENCE_MISMATCH")
    allowed = set(expected)
    for trace in traces:
        if len(trace.sequences) != reads_per_reader:
            violations.append(f"reader:{trace.reader_id}:READ_COUNT_MISMATCH")
        if any(sequence not in allowed for sequence in trace.sequences):
            violations.append(f"reader:{trace.reader_id}:UNKNOWN_SEQUENCE")
        if tuple(sorted(trace.sequences)) != trace.sequences:
            violations.append(f"reader:{trace.reader_id}:SEQUENCE_REGRESSION")
    traces.sort(key=lambda trace: trace.reader_id)
    if len(traces) != reader_count:
        violations.append("READER_COMPLETION_COUNT_MISMATCH")
    return ConcurrentReaderAuditResult(
        verdict="PASS" if not violations else "FAIL_CLOSED",
        reader_count=reader_count,
        total_reads=sum(len(trace.sequences) for trace in traces),
        initial_sequence=initial_sequence,
        final_sequence=publish_sequences[-1],
        traces=tuple(traces),
        violations=tuple(sorted(violations)),
    )


def _publish(root: Path, sequence: int, *, max_artifact_bytes: int) -> None:
    artifact = build_simulated_artifact(sequence=sequence, data={"audit": "phase4ft"})
    simulate_publication(
        root,
        artifact,
        crash_point="AFTER_REPLACE",
        max_artifact_bytes=max_artifact_bytes,
    )


def _validate_bounds(
    *,
    reader_count: int,
    reads_per_reader: int,
    max_readers: int,
    max_reads_per_reader: int,
) -> None:
    values = (reader_count, reads_per_reader, max_readers, max_reads_per_reader)
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in values):
        raise ConcurrentReaderAuditError("CONCURRENCY_BOUND_INVALID")
    if reader_count > max_readers:
        raise ConcurrentReaderAuditError("READER_BOUND_EXCEEDED")
    if reads_per_reader > max_reads_per_reader:
        raise ConcurrentReaderAuditError("READ_BOUND_EXCEEDED")


def _age(generated_at: datetime, now: datetime) -> float:
    if generated_at.tzinfo is None or now.tzinfo is None:
        raise ConcurrentReaderAuditError("EVIDENCE_TIMESTAMP_NAIVE")
    return (now.astimezone(UTC) - generated_at.astimezone(UTC)).total_seconds()
