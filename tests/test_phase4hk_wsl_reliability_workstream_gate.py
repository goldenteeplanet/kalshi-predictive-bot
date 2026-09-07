from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace

import pytest

from kalshi_predictor.workstation.recovery_decision_tamper_detection import (
    detect_recovery_decision_tampering,
    make_recovery_decision_record,
)
from kalshi_predictor.workstation.recovery_evidence_canonicalization import (
    BUNDLE_SCHEMA_VERSION,
    PHASE_ORDER,
    CanonicalEvidenceEntry,
    CanonicalRecoveryEvidenceBundle,
)
from kalshi_predictor.workstation.wsl_keepalive_reliability_audit import (
    audit_wsl_keepalive_reliability,
    make_wsl_keepalive_observation,
)
from kalshi_predictor.workstation.wsl_reliability_workstream_gate import (
    WslReliabilityWorkstreamGateError,
    evaluate_wsl_reliability_workstream_gate,
    validate_wsl_reliability_workstream_gate_result,
)


def test_complete_workstream_is_certified_but_control_remains_denied() -> None:
    audit, bundle, report = _inputs()
    first = evaluate_wsl_reliability_workstream_gate(
        audit, bundle, report, evaluated_at_epoch_seconds=200
    )
    second = evaluate_wsl_reliability_workstream_gate(
        audit, bundle, report, evaluated_at_epoch_seconds=200
    )
    validate_wsl_reliability_workstream_gate_result(first)
    assert first.status == "CERTIFIED"
    assert first.gate_hash == second.gate_hash
    assert first.workstream_certified is True
    assert first.recovery_authorized is False


def test_unhealthy_keepalive_and_unverified_decision_are_denied() -> None:
    audit, bundle, report = _inputs(keepalive_present=False)
    result = evaluate_wsl_reliability_workstream_gate(
        audit, bundle, report, evaluated_at_epoch_seconds=200
    )
    assert result.status == "DENIED"
    assert "KEEPALIVE_AUDIT_NOT_HEALTHY:DEGRADED" in result.reasons
    audit, bundle, report = _inputs(decision_action="DENY_RECOVERY")
    result = evaluate_wsl_reliability_workstream_gate(
        audit, bundle, report, evaluated_at_epoch_seconds=200
    )
    assert "DECISION_REPORT_NOT_VERIFIED:DENIED" in result.reasons


def test_exact_freshness_and_expiry_boundaries_pass() -> None:
    audit, bundle, report = _inputs(age=120, expires=200, decision_evaluated=200)
    assert (
        evaluate_wsl_reliability_workstream_gate(
            audit, bundle, report, evaluated_at_epoch_seconds=200
        ).status
        == "CERTIFIED"
    )
    audit, bundle, report = _inputs(age=121)
    assert (
        evaluate_wsl_reliability_workstream_gate(
            audit, bundle, report, evaluated_at_epoch_seconds=200
        ).status
        == "STALE"
    )
    audit, bundle, report = _inputs(expires=199, decision_evaluated=199)
    assert (
        evaluate_wsl_reliability_workstream_gate(
            audit, bundle, report, evaluated_at_epoch_seconds=200
        ).status
        == "DENIED"
    )


def test_incomplete_evidence_and_invalid_bounds_fail_closed() -> None:
    audit, bundle, report = _inputs(keepalive_complete=False)
    assert (
        evaluate_wsl_reliability_workstream_gate(
            audit, bundle, report, evaluated_at_epoch_seconds=200
        ).status
        == "INCOMPLETE"
    )
    with pytest.raises(WslReliabilityWorkstreamGateError, match="GATE_BOUND_INVALID"):
        evaluate_wsl_reliability_workstream_gate(
            audit, bundle, report, evaluated_at_epoch_seconds=True
        )


def test_upstream_and_bundle_binding_tampering_fail_closed() -> None:
    audit, bundle, report = _inputs()
    with pytest.raises(WslReliabilityWorkstreamGateError, match="KEEPALIVE_AUDIT_INVALID"):
        evaluate_wsl_reliability_workstream_gate(
            replace(audit, audit_hash="0" * 64),
            bundle,
            report,
            evaluated_at_epoch_seconds=200,
        )
    with pytest.raises(WslReliabilityWorkstreamGateError, match="CANONICAL_BUNDLE_INVALID"):
        evaluate_wsl_reliability_workstream_gate(
            audit,
            replace(bundle, bundle_hash="0" * 64),
            report,
            evaluated_at_epoch_seconds=200,
        )
    rebound = replace(report, bundle_hash="f" * 64)
    rebound = _rehash_report(rebound)
    result = evaluate_wsl_reliability_workstream_gate(
        audit, bundle, rebound, evaluated_at_epoch_seconds=200
    )
    assert "DECISION_REPORT_BUNDLE_BINDING_MISMATCH" in result.reasons


def test_gate_hash_and_safety_tampering_fail_closed() -> None:
    audit, bundle, report = _inputs()
    result = evaluate_wsl_reliability_workstream_gate(
        audit, bundle, report, evaluated_at_epoch_seconds=200
    )
    with pytest.raises(WslReliabilityWorkstreamGateError, match="GATE_HASH_MISMATCH"):
        validate_wsl_reliability_workstream_gate_result(replace(result, gate_hash="0" * 64))
    with pytest.raises(WslReliabilityWorkstreamGateError, match="GATE_SAFETY_BOUNDARY_INVALID"):
        validate_wsl_reliability_workstream_gate_result(
            replace(result, service_control_authorized=True)
        )


def test_gate_has_no_query_control_notification_or_mutation_surface() -> None:
    names = set(evaluate_wsl_reliability_workstream_gate.__code__.co_names)
    assert names.isdisjoint(
        {
            "Popen",
            "commit",
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


def _inputs(
    *,
    age=1,
    keepalive_present=True,
    keepalive_complete=True,
    expires=300,
    decision_evaluated=200,
    decision_action="AWAIT_RECOVERY_POLICY",
):
    audit = audit_wsl_keepalive_reliability(
        [
            make_wsl_keepalive_observation(
                sequence=index,
                observed_at_epoch_seconds=100 + index,
                wsl_running=True,
                keepalive_present=keepalive_present,
                user_systemd_reachable=True,
                authoritative_service_active=True,
                complete=keepalive_complete,
                source_identity_hash="a" * 64,
                evidence_age_seconds=age,
            )
            for index in (1, 2)
        ]
    )
    bundle = _bundle(age=age)
    decision = make_recovery_decision_record(
        decision_id="decision-1",
        created_at_epoch_seconds=100,
        expires_at_epoch_seconds=expires,
        action=decision_action,
        target="kalshi-fixed-rate-refresh.service",
        bundle_hash=bundle.bundle_hash,
        prerequisites_ready_claim=True,
        complete=True,
        source_identity_hash="b" * 64,
    )
    report = detect_recovery_decision_tampering(
        bundle, decision, evaluated_at_epoch_seconds=decision_evaluated
    )
    return audit, bundle, report


def _bundle(*, age=1):
    statuses = ("STABLE", "AVAILABLE", "HEALTHY", "REACHABLE", "HEALTHY", "PASSED", "PASSED")
    entries = tuple(
        CanonicalEvidenceEntry(phase, status, _hash(f"{phase}-evidence"), age)
        for phase, status in zip(PHASE_ORDER, statuses, strict=True)
    )
    entry_payload = [asdict(entry) for entry in entries]
    unsigned = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "status": "READY",
        "reasons": [],
        "entries": entry_payload,
        "phase_order": list(PHASE_ORDER),
        "observed_max_age_seconds": age,
        "max_evidence_age_seconds": 120,
        "source_set_hash": _hash(["a" * 64] * 7),
        "entries_hash": _hash(entry_payload),
        "read_only": True,
        "canonicalization_complete": True,
        "prerequisites_ready": True,
        "alert_required": False,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return CanonicalRecoveryEvidenceBundle(
        status="READY",
        reasons=(),
        entries=entries,
        phase_order=PHASE_ORDER,
        observed_max_age_seconds=age,
        max_evidence_age_seconds=120,
        source_set_hash=unsigned["source_set_hash"],
        entries_hash=unsigned["entries_hash"],
        bundle_hash=_hash(unsigned),
        canonicalization_complete=True,
        prerequisites_ready=True,
    )


def _rehash_report(report):
    unsigned = asdict(report)
    unsigned.pop("report_hash")
    unsigned["schema_version"] = "phase4hj-recovery-decision-tamper-detection-v1"
    unsigned["reasons"] = list(unsigned["reasons"])
    return replace(report, report_hash=_hash(unsigned))


def _hash(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
