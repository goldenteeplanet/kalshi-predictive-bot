from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.phase4cd.read_model_chain import ReadModelChainResult
from kalshi_predictor.phase4cd.read_model_compatibility import CompatibilityResult
from kalshi_predictor.phase4cd.read_model_consumer import ReadModelView
from kalshi_predictor.phase4cd.read_model_differential_replay import (
    compare_dashboard_replays,
)
from kalshi_predictor.phase4cd.read_model_provenance_dashboard import (
    build_provenance_dashboard,
)
from kalshi_predictor.phase4cd.read_model_release_candidate import (
    ReadModelReleaseCandidateError,
    build_release_candidate,
    validate_release_candidate,
)
from kalshi_predictor.phase4cd.read_model_staleness import StalenessEscalation


def test_healthy_matching_candidate_accepts() -> None:
    dashboard = _dashboard()
    candidate = build_release_candidate(
        dashboard=dashboard,
        replay=compare_dashboard_replays([dashboard], [dashboard]),
    )
    validate_release_candidate(candidate)
    assert candidate.decision == "ACCEPT"
    assert candidate.reasons == ()
    assert candidate.execution_authorized is False


@pytest.mark.parametrize("status", ["WARNING", "STALE", "STALLED", "LINEAGE_FAILURE"])
def test_nonhealthy_dashboard_rejects(status: str) -> None:
    dashboard = _dashboard(staleness=status)
    candidate = build_release_candidate(
        dashboard=dashboard,
        replay=compare_dashboard_replays([dashboard], [dashboard]),
    )
    assert candidate.decision == "REJECT"
    assert candidate.reasons == (f"DASHBOARD_{status}",)


def test_replay_divergence_rejects() -> None:
    dashboard = _dashboard()
    warning = _dashboard(staleness="WARNING")
    candidate = build_release_candidate(
        dashboard=dashboard,
        replay=compare_dashboard_replays([dashboard], [warning]),
    )
    assert candidate.decision == "REJECT"
    assert candidate.reasons == ("REPLAY_DIVERGENCE",)


def test_missing_malformed_and_tampered_upstream_fail_closed() -> None:
    dashboard = _dashboard()
    replay = compare_dashboard_replays([dashboard], [dashboard])
    with pytest.raises(ReadModelReleaseCandidateError, match="UPSTREAM_EVIDENCE_INVALID"):
        build_release_candidate(dashboard={}, replay=replay)
    tampered = dict(dashboard)
    tampered["status"] = "HEALTHY"
    tampered["dashboard_hash"] = "0" * 64
    with pytest.raises(ReadModelReleaseCandidateError, match="UPSTREAM_EVIDENCE_INVALID"):
        build_release_candidate(dashboard=tampered, replay=replay)


def test_unavailable_dashboard_fails_closed() -> None:
    dashboard = build_provenance_dashboard(
        view=None, compatibility=None, chain=None, staleness=None
    )
    replay = compare_dashboard_replays([dashboard], [dashboard])
    with pytest.raises(ReadModelReleaseCandidateError, match="PROVENANCE_UNAVAILABLE"):
        build_release_candidate(dashboard=dashboard, replay=replay)


def test_release_tampering_and_safety_boundary_fail_closed() -> None:
    dashboard = _dashboard()
    candidate = build_release_candidate(
        dashboard=dashboard,
        replay=compare_dashboard_replays([dashboard], [dashboard]),
    )
    with pytest.raises(ReadModelReleaseCandidateError, match="RELEASE_HASH_MISMATCH"):
        validate_release_candidate(replace(candidate, release_hash="0" * 64))
    with pytest.raises(ReadModelReleaseCandidateError, match="RELEASE_SAFETY_BOUNDARY_INVALID"):
        validate_release_candidate(replace(candidate, execution_authorized=True))


def test_builder_has_no_mutation_surface() -> None:
    assert "execute" not in dir(build_release_candidate)
    assert "commit" not in dir(build_release_candidate)


def _dashboard(*, staleness: str = "FRESH"):
    view = ReadModelView(
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
    compatibility = CompatibilityResult(
        producer_schema=view.schema_version,
        consumer_schema=view.schema_version,
        decision="COMPATIBLE",
        reason="test",
        matrix_hash="e" * 64,
        age_seconds=1,
    )
    chain = ReadModelChainResult(
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
    stale = StalenessEscalation(
        status=staleness,  # type: ignore[arg-type]
        action="NO_ACTION" if staleness == "FRESH" else "ESCALATE",
        snapshot_age_seconds=1,
        progress_age_seconds=1,
        reason="test",
        evidence_hash="f" * 64,
    )
    return build_provenance_dashboard(
        view=view,
        compatibility=compatibility,
        chain=chain,
        staleness=stale,
    )
