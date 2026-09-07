from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.phase4cd.evidence_latency_workstream_gate import (
    EvidenceLatencyWorkstreamGateError,
    evaluate_evidence_latency_workstream_gate,
    make_evidence_latency_gate_artifact,
    validate_evidence_latency_workstream_gate,
)


def test_valid_gate_is_ready_deterministic_and_read_only() -> None:
    first = evaluate_evidence_latency_workstream_gate(_artifacts())
    second = evaluate_evidence_latency_workstream_gate(list(reversed(_artifacts())))
    validate_evidence_latency_workstream_gate(first)
    assert first.status == "READY"
    assert first.artifact_set_hash == second.artifact_set_hash
    assert first.execution_authorized is False


def test_empty_partial_and_bound_fail_closed() -> None:
    with pytest.raises(EvidenceLatencyWorkstreamGateError, match="ARTIFACTS_EMPTY"):
        evaluate_evidence_latency_workstream_gate([])
    with pytest.raises(EvidenceLatencyWorkstreamGateError, match="ARTIFACT_STAGE_SET_INVALID"):
        evaluate_evidence_latency_workstream_gate(_artifacts()[:-1])
    with pytest.raises(EvidenceLatencyWorkstreamGateError, match="ARTIFACT_BOUND_EXCEEDED"):
        evaluate_evidence_latency_workstream_gate(_artifacts(), max_artifacts=4)


def test_exact_freshness_boundary_is_ready_and_one_second_over_is_stale() -> None:
    assert evaluate_evidence_latency_workstream_gate(_artifacts(age=300)).status == "READY"
    stale = evaluate_evidence_latency_workstream_gate(_artifacts(age=301))
    assert stale.status == "STALE"
    assert stale.reasons == ("WORKSTREAM_EVIDENCE_STALE",)


def test_non_ready_and_incomplete_artifacts_block() -> None:
    blocked = evaluate_evidence_latency_workstream_gate(
        _artifacts(overrides={"plan_drift": {"outcome": "DRIFT"}})
    )
    assert blocked.status == "BLOCKED"
    assert blocked.reasons == ("PLAN_DRIFT_NOT_READY",)
    partial = evaluate_evidence_latency_workstream_gate(
        _artifacts(overrides={"cold_start_seed": {"complete": False}})
    )
    assert partial.reasons == ("COLD_START_SEED_INCOMPLETE",)


def test_malformed_duplicate_lineage_and_tampering_fail_closed() -> None:
    with pytest.raises(EvidenceLatencyWorkstreamGateError, match="ARTIFACT_FIELD_INVALID"):
        _artifact("memory_bounds", age=-1)
    duplicate = _artifacts()[:-1] + [_artifact("regression_corpus")]
    with pytest.raises(EvidenceLatencyWorkstreamGateError, match="ARTIFACT_STAGE_DUPLICATE"):
        evaluate_evidence_latency_workstream_gate(duplicate)
    mixed = _artifacts()
    mixed[-1] = _artifact("plan_drift", identity="b" * 64)
    with pytest.raises(EvidenceLatencyWorkstreamGateError, match="SOURCE_LINEAGE_MIXED"):
        evaluate_evidence_latency_workstream_gate(mixed)
    item = _artifact("plan_drift")
    with pytest.raises(EvidenceLatencyWorkstreamGateError, match="ARTIFACT_HASH_MISMATCH"):
        evaluate_evidence_latency_workstream_gate(
            _artifacts()[:-1] + [replace(item, outcome="DRIFT")]
        )


def test_gate_result_tampering_and_safety_boundary_fail_closed() -> None:
    gate = evaluate_evidence_latency_workstream_gate(_artifacts())
    with pytest.raises(EvidenceLatencyWorkstreamGateError, match="GATE_HASH_MISMATCH"):
        validate_evidence_latency_workstream_gate(replace(gate, gate_hash="0" * 64))
    with pytest.raises(EvidenceLatencyWorkstreamGateError, match="GATE_SAFETY_BOUNDARY_INVALID"):
        validate_evidence_latency_workstream_gate(replace(gate, execution_authorized=True))


def test_gate_has_no_database_artifact_publication_or_mutation_surface() -> None:
    names = set(evaluate_evidence_latency_workstream_gate.__code__.co_names)
    assert names.isdisjoint(
        {"commit", "connect", "execute", "open", "publish", "replace", "unlink", "write"}
    )


def _artifacts(*, age: int = 1, overrides: dict[str, dict] | None = None):
    overrides = overrides or {}
    return [
        _artifact(stage, age=age, **overrides.get(stage, {}))
        for stage in (
            "memory_bounds",
            "provenance_integrity",
            "cold_start_seed",
            "regression_corpus",
            "plan_drift",
        )
    ]


def _artifact(
    stage: str,
    *,
    age: int = 1,
    identity: str = "a" * 64,
    outcome: str | None = None,
    complete: bool = True,
):
    outcomes = {
        "memory_bounds": "WITHIN_BOUNDS",
        "provenance_integrity": "INTACT",
        "cold_start_seed": "PROPOSE",
        "regression_corpus": "READY",
        "plan_drift": "STABLE",
    }
    return make_evidence_latency_gate_artifact(
        stage=stage,
        upstream_schema_version="v1",
        artifact_hash="hash:" + stage,
        source_identity_hash=identity,
        source_watermark="w",
        outcome=outcome or outcomes[stage],
        evidence_age_seconds=age,
        complete=complete,
    )
