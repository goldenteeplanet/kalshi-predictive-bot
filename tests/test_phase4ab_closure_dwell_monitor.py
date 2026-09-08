from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ab_closure_dwell_monitor.py"
    spec = importlib.util.spec_from_file_location("phase4ab_closure_dwell_monitor", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gate(module, at: datetime, *, state="WAITING_SETTLEMENT", canonical=0, evaluated=0):
    payload = {
        "schema": module.SOURCE_SCHEMA,
        "generated_at": at.isoformat(),
        "state": state,
        "reason": "fixture",
        "safe_to_advance": state == "COMPLETE",
        "production_database_written": False,
        "trading_mode_changed": False,
        "source_rows_hash": "a" * 64,
        "source_hint_artifact_hash": "b" * 64,
        "hint_count": 14,
        "canonical_count": canonical,
        "fully_evaluated_count": evaluated,
        "blocking_counts": {"HINT_NO_CAPTURE": 1} if state == "ATTENTION" else {},
    }
    payload["artifact_hash"] = module.artifact_hash(payload)
    return payload


def _build(module, gates, now, *, settlement=100, reconciliation=50, age=30):
    return module.build_status(
        gates,
        now=now,
        maximum_evidence_age_seconds=age,
        settlement_dwell_seconds=settlement,
        reconciliation_dwell_seconds=reconciliation,
    )


def test_progress_resets_dwell_and_preserves_hash() -> None:
    module = _module()
    start = datetime(2026, 8, 25, 18, tzinfo=UTC)
    gates = [
        _gate(module, start),
        _gate(module, start + timedelta(seconds=90), canonical=1),
        _gate(module, start + timedelta(seconds=100), canonical=1),
    ]
    result = _build(module, gates, start + timedelta(seconds=101))
    assert result["state"] == "WAITING"
    assert result["progress_events"] == 1
    assert result["dwell_seconds"] == 10
    assert result["artifact_hash"] == module.artifact_hash(result)


@pytest.mark.parametrize(
    ("state", "threshold", "expected_reason"),
    [
        ("WAITING_SETTLEMENT", 100, "SETTLEMENT_DWELL_THRESHOLD_REACHED"),
        ("WAITING_RECONCILIATION", 50, "RECONCILIATION_DWELL_THRESHOLD_REACHED"),
    ],
)
def test_dwell_threshold_is_inclusive(state: str, threshold: int, expected_reason: str) -> None:
    module = _module()
    start = datetime(2026, 8, 25, 18, tzinfo=UTC)
    gates = [
        _gate(module, start, state=state),
        _gate(module, start + timedelta(seconds=threshold), state=state),
    ]
    result = _build(
        module,
        gates,
        start + timedelta(seconds=threshold + 1),
        settlement=threshold if state == "WAITING_SETTLEMENT" else 1000,
        reconciliation=threshold if state == "WAITING_RECONCILIATION" else 1000,
    )
    assert result["state"] == "ESCALATE"
    assert result["reason"] == expected_reason


def test_evidence_age_threshold_and_future_evidence_fail_closed() -> None:
    module = _module()
    now = datetime(2026, 8, 25, 18, tzinfo=UTC)
    stale = _build(module, [_gate(module, now - timedelta(seconds=30))], now)
    assert (stale["state"], stale["reason"]) == (
        "STALE",
        "LATEST_EVIDENCE_AGE_THRESHOLD_REACHED",
    )
    future = _build(module, [_gate(module, now + timedelta(seconds=1))], now)
    assert (future["state"], future["safe_to_advance"]) == ("ATTENTION", False)


def test_lineage_failure_and_regression_take_precedence() -> None:
    module = _module()
    start = datetime(2026, 8, 25, 18, tzinfo=UTC)
    lineage = _build(module, [_gate(module, start, state="ATTENTION")], start)
    assert lineage["reason"] == "LINEAGE_FAILURE_REQUIRES_REVIEW"
    regressed = _build(
        module,
        [
            _gate(module, start, canonical=2),
            _gate(module, start + timedelta(seconds=1), canonical=1),
        ],
        start + timedelta(seconds=2),
    )
    assert regressed["reason"] == "CLOSURE_COUNTS_REGRESSED"


def test_tampering_is_detected_and_invalid_status_is_fail_closed(tmp_path: Path) -> None:
    module = _module()
    now = datetime(2026, 8, 25, 18, tzinfo=UTC)
    payload = _gate(module, now)
    payload["canonical_count"] = 99
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        module.load_gate(path)
    status = module.invalid_source_status(now=now, error=ValueError("HASH_MISMATCH"))
    assert status["state"] == "ATTENTION"
    assert status["safe_to_advance"] is False
    assert status["artifact_hash"] == module.artifact_hash(status)


def test_complete_is_only_safe_to_advance_state() -> None:
    module = _module()
    now = datetime(2026, 8, 25, 18, tzinfo=UTC)
    complete = _build(
        module,
        [_gate(module, now, state="COMPLETE", canonical=14, evaluated=14)],
        now,
    )
    assert (complete["state"], complete["safe_to_advance"]) == ("COMPLETE", True)
