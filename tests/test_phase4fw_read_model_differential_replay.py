from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from kalshi_predictor.phase4cd.read_model_chain import ReadModelChainResult
from kalshi_predictor.phase4cd.read_model_compatibility import CompatibilityResult
from kalshi_predictor.phase4cd.read_model_consumer import ReadModelView
from kalshi_predictor.phase4cd.read_model_differential_replay import (
    DifferentialReplayError,
    compare_dashboard_replays,
    validate_replay_result,
)
from kalshi_predictor.phase4cd.read_model_provenance_dashboard import (
    build_provenance_dashboard,
)
from kalshi_predictor.phase4cd.read_model_staleness import StalenessEscalation


def test_equivalent_replays_match_deterministically() -> None:
    rows = [_dashboard(), _dashboard()]
    result = compare_dashboard_replays(rows, copy.deepcopy(rows), max_snapshots=2)
    validate_replay_result(result)
    assert result.status == "MATCH"
    assert result.compared_snapshots == 2
    assert result.primary_digest == result.replay_digest


def test_divergence_reports_first_exact_index() -> None:
    primary = [_dashboard(), _dashboard()]
    replay = copy.deepcopy(primary)
    replay[1] = _dashboard(staleness="WARNING")
    result = compare_dashboard_replays(primary, replay, max_snapshots=2)
    assert result.status == "DIVERGENCE"
    assert result.first_divergence_index == 1


def test_empty_and_partial_replay_fail_closed() -> None:
    with pytest.raises(DifferentialReplayError, match="REPLAY_EMPTY"):
        compare_dashboard_replays([], [])
    with pytest.raises(DifferentialReplayError, match="REPLAY_EMPTY"):
        compare_dashboard_replays([_dashboard()], [])


def test_exact_bound_and_overflow() -> None:
    rows = [_dashboard(), _dashboard()]
    assert compare_dashboard_replays(rows, rows, max_snapshots=2).compared_snapshots == 2
    with pytest.raises(DifferentialReplayError, match="SNAPSHOT_BOUND_EXCEEDED"):
        compare_dashboard_replays(rows, rows, max_snapshots=1)


def test_length_mismatch_and_malformed_input_fail_closed() -> None:
    with pytest.raises(DifferentialReplayError, match="REPLAY_LENGTH_MISMATCH"):
        compare_dashboard_replays([_dashboard()], [_dashboard(), _dashboard()])
    with pytest.raises(DifferentialReplayError, match="DASHBOARD_INVALID"):
        compare_dashboard_replays([{"status": "HEALTHY"}], [{"status": "HEALTHY"}])


def test_dashboard_tampering_fails_before_comparison() -> None:
    row = _dashboard()
    tampered = copy.deepcopy(row)
    tampered["lane"]["evaluated"] = 2
    with pytest.raises(DifferentialReplayError, match="DASHBOARD_INVALID"):
        compare_dashboard_replays([row], [tampered])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    result = compare_dashboard_replays([_dashboard()], [_dashboard()])
    with pytest.raises(DifferentialReplayError, match="REPLAY_EVIDENCE_HASH_MISMATCH"):
        validate_replay_result(replace(result, evidence_hash="0" * 64))
    with pytest.raises(DifferentialReplayError, match="REPLAY_SAFETY_BOUNDARY_INVALID"):
        validate_replay_result(replace(result, execution_authorized=True))


def test_replay_is_pure_and_has_no_writer_surface() -> None:
    rows = [_dashboard()]
    original = copy.deepcopy(rows)
    compare_dashboard_replays(rows, rows)
    assert rows == original
    assert "execute" not in dir(compare_dashboard_replays)
    assert "commit" not in dir(compare_dashboard_replays)


def _dashboard(*, staleness: str = "FRESH"):
    return build_provenance_dashboard(
        view=_view(),
        compatibility=_compatibility("COMPATIBLE"),
        chain=_chain(),
        staleness=_staleness(staleness),
    )


def _view() -> ReadModelView:
    return ReadModelView(
        schema_version="phase4fm-evidence-read-model-v1",
        generated_at="2026-08-27T00:00:00+00:00",
        source_watermark="paper_pnl:10",
        source_database_identity_hash="a" * 64,
        age_seconds=1,
        guarded_paper_settled=203,
        realized_pnl="0",
        paper_order_count=204,
        evidence_lane={"evaluated": 1},
        payload_hash="b" * 64,
        manifest_hash="c" * 64,
    )


def _compatibility(decision: str) -> CompatibilityResult:
    return CompatibilityResult(
        producer_schema="phase4fm-evidence-read-model-v1",
        consumer_schema="phase4fm-evidence-read-model-v1",
        decision=decision,
        reason="test",
        matrix_hash="e" * 64,
        age_seconds=1,
    )


def _chain() -> ReadModelChainResult:
    return ReadModelChainResult(
        node_count=2,
        genesis_hash="1" * 64,
        head_hash="d" * 64,
        source="paper_pnl",
        source_identity_hash="a" * 64,
        first_sequence=9,
        last_sequence=10,
        progress=1,
        head_age_seconds=1,
    )


def _staleness(status: str) -> StalenessEscalation:
    return StalenessEscalation(
        status=status,  # type: ignore[arg-type]
        action="NO_ACTION" if status == "FRESH" else "ESCALATE",
        snapshot_age_seconds=1,
        progress_age_seconds=1,
        reason="test",
        evidence_hash="f" * 64,
    )
