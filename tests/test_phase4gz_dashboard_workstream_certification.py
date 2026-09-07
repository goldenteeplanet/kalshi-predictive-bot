from __future__ import annotations

from dataclasses import replace

import pytest
from kalshi_predictor.ui.dashboard_workstream_certification import (
    REQUIRED_PHASES,
    DashboardWorkstreamCertificationError,
    certify_dashboard_workstream,
    make_dashboard_phase_evidence,
    validate_dashboard_workstream_certification,
)


def test_complete_workstream_certifies_deterministically_and_read_only() -> None:
    first = certify_dashboard_workstream(list(reversed(_evidence())))
    second = certify_dashboard_workstream(_evidence())
    validate_dashboard_workstream_certification(first)
    assert first.status == "CERTIFIED"
    assert first.certification_hash == second.certification_hash
    assert first.evidence_count == len(REQUIRED_PHASES)
    assert first.execution_authorized is False


def test_empty_bound_and_missing_phase_fail_closed() -> None:
    with pytest.raises(DashboardWorkstreamCertificationError, match="EVIDENCE_EMPTY"):
        certify_dashboard_workstream([])
    with pytest.raises(DashboardWorkstreamCertificationError, match="EVIDENCE_BOUND_EXCEEDED"):
        certify_dashboard_workstream(_evidence(), max_evidence_items=11)
    with pytest.raises(DashboardWorkstreamCertificationError, match="REQUIRED_PHASE_SET_MISMATCH"):
        certify_dashboard_workstream(_evidence()[:-1])


def test_exact_freshness_boundary_certifies_and_one_second_stale() -> None:
    exact = _evidence(age=300)
    assert certify_dashboard_workstream(exact).status == "CERTIFIED"
    stale = _evidence(age=301)
    assert certify_dashboard_workstream(stale).status == "STALE"


def test_failed_and_partial_phase_block_certification() -> None:
    evidence = _evidence()
    evidence[0] = _item(REQUIRED_PHASES[0], passed=False)
    evidence[1] = _item(REQUIRED_PHASES[1], complete=False)
    result = certify_dashboard_workstream(evidence)
    assert result.status == "BLOCKED"
    assert result.failed_phase_count == 2
    assert f"PHASE_NOT_CERTIFIED:{REQUIRED_PHASES[0]}" in result.reasons


def test_malformed_duplicate_lineage_and_input_tampering_fail_closed() -> None:
    with pytest.raises(DashboardWorkstreamCertificationError, match="ARTIFACT_HASH_INVALID"):
        _item("4GN", artifact_hash="bad")
    duplicate = _evidence()
    duplicate[-1] = _item(REQUIRED_PHASES[0])
    with pytest.raises(DashboardWorkstreamCertificationError, match="PHASE_DUPLICATE"):
        certify_dashboard_workstream(duplicate)
    mixed = _evidence()
    mixed[-1] = _item(REQUIRED_PHASES[-1], identity="b" * 64)
    with pytest.raises(DashboardWorkstreamCertificationError, match="EVIDENCE_LINEAGE_MIXED"):
        certify_dashboard_workstream(mixed)
    item = _item("4GN")
    with pytest.raises(DashboardWorkstreamCertificationError, match="EVIDENCE_HASH_MISMATCH"):
        certify_dashboard_workstream([replace(item, passed=False), *_evidence()[1:]])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    result = certify_dashboard_workstream(_evidence())
    with pytest.raises(DashboardWorkstreamCertificationError, match="CERTIFICATION_HASH_MISMATCH"):
        validate_dashboard_workstream_certification(replace(result, certification_hash="0" * 64))
    with pytest.raises(
        DashboardWorkstreamCertificationError,
        match="CERTIFICATION_SAFETY_BOUNDARY_INVALID",
    ):
        validate_dashboard_workstream_certification(replace(result, execution_authorized=True))


def test_certifier_has_no_query_publication_or_mutation_surface() -> None:
    names = set(certify_dashboard_workstream.__code__.co_names)
    assert names.isdisjoint({"commit", "connect", "execute", "open", "publish", "unlink", "write"})


def _item(
    phase,
    *,
    artifact_hash="a" * 64,
    passed=True,
    complete=True,
    identity="c" * 64,
    age=1,
):
    return make_dashboard_phase_evidence(
        phase=phase,
        artifact_schema_version=f"phase{phase.lower()}-v1",
        artifact_hash=artifact_hash,
        passed=passed,
        complete=complete,
        source_identity_hash=identity,
        source_watermark="dashboard-cycle-1",
        evidence_age_seconds=age,
    )


def _evidence(*, age=1):
    return [
        _item(phase, artifact_hash=f"{index + 1:064x}", age=age)
        for index, phase in enumerate(REQUIRED_PHASES)
    ]
