from __future__ import annotations

from dataclasses import replace

import pytest
from kalshi_predictor.workstation.wsl_keepalive_reliability_audit import (
    WslKeepaliveReliabilityAuditError,
    audit_wsl_keepalive_reliability,
    make_wsl_keepalive_observation,
    validate_wsl_keepalive_reliability_audit,
)


def test_healthy_observations_are_deterministic_and_read_only() -> None:
    first = audit_wsl_keepalive_reliability(list(reversed(_observations())))
    second = audit_wsl_keepalive_reliability(_observations())
    validate_wsl_keepalive_reliability_audit(first)
    assert first.status == "HEALTHY"
    assert first.audit_hash == second.audit_hash
    assert first.service_control_authorized is False


def test_empty_and_observation_bound_fail_closed() -> None:
    with pytest.raises(WslKeepaliveReliabilityAuditError, match="OBSERVATIONS_EMPTY"):
        audit_wsl_keepalive_reliability([])
    with pytest.raises(WslKeepaliveReliabilityAuditError, match="OBSERVATION_BOUND_EXCEEDED"):
        audit_wsl_keepalive_reliability(_observations(), max_observations=1)


def test_exact_gap_and_freshness_boundaries_pass() -> None:
    exact = [_observation(1, 100, age=120), _observation(2, 160, age=120)]
    assert audit_wsl_keepalive_reliability(exact).status == "HEALTHY"
    assert (
        audit_wsl_keepalive_reliability([_observation(1, 100), _observation(2, 161)]).status
        == "DEGRADED"
    )
    assert audit_wsl_keepalive_reliability([_observation(1, 100, age=121)]).status == "STALE"


def test_reliability_and_partial_failures_are_explicit() -> None:
    result = audit_wsl_keepalive_reliability(
        [
            _observation(
                1, 100, running=False, keepalive=False, systemd=False, service=False, complete=False
            )
        ]
    )
    assert result.status == "DEGRADED"
    assert result.failure_count == 5
    assert "KEEPALIVE_MISSING:1" in result.reasons


def test_malformed_sequence_lineage_and_tampering_fail_closed() -> None:
    with pytest.raises(WslKeepaliveReliabilityAuditError, match="OBSERVATION_FIELD_INVALID"):
        _observation(-1, 100)
    with pytest.raises(WslKeepaliveReliabilityAuditError, match="OBSERVATION_SEQUENCE_GAP"):
        audit_wsl_keepalive_reliability([_observation(1, 100), _observation(3, 120)])
    with pytest.raises(WslKeepaliveReliabilityAuditError, match="OBSERVATION_LINEAGE_MIXED"):
        audit_wsl_keepalive_reliability(
            [_observation(1, 100), _observation(2, 120, identity="b" * 64)]
        )
    item = _observation(1, 100)
    with pytest.raises(WslKeepaliveReliabilityAuditError, match="OBSERVATION_HASH_MISMATCH"):
        audit_wsl_keepalive_reliability([replace(item, keepalive_present=False)])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    result = audit_wsl_keepalive_reliability(_observations())
    with pytest.raises(WslKeepaliveReliabilityAuditError, match="AUDIT_HASH_MISMATCH"):
        validate_wsl_keepalive_reliability_audit(replace(result, audit_hash="0" * 64))
    with pytest.raises(WslKeepaliveReliabilityAuditError, match="AUDIT_SAFETY_BOUNDARY_INVALID"):
        validate_wsl_keepalive_reliability_audit(replace(result, recovery_authorized=True))


def test_audit_has_no_process_service_query_or_mutation_surface() -> None:
    names = set(audit_wsl_keepalive_reliability.__code__.co_names)
    assert names.isdisjoint(
        {
            "Popen",
            "commit",
            "connect",
            "execute",
            "kill",
            "open",
            "restart",
            "start",
            "stop",
            "write",
        }
    )


def _observation(
    sequence,
    observed_at,
    *,
    running=True,
    keepalive=True,
    systemd=True,
    service=True,
    complete=True,
    identity="a" * 64,
    age=1,
):
    return make_wsl_keepalive_observation(
        sequence=sequence,
        observed_at_epoch_seconds=observed_at,
        wsl_running=running,
        keepalive_present=keepalive,
        user_systemd_reachable=systemd,
        authoritative_service_active=service,
        complete=complete,
        source_identity_hash=identity,
        evidence_age_seconds=age,
    )


def _observations():
    return [_observation(1, 100), _observation(2, 130)]
