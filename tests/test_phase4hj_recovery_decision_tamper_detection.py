from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace

import pytest

from kalshi_predictor.workstation.recovery_decision_tamper_detection import (
    RecoveryDecisionTamperDetectionError,
    detect_recovery_decision_tampering,
    make_recovery_decision_record,
    validate_recovery_decision_tamper_report,
)
from kalshi_predictor.workstation.recovery_evidence_canonicalization import (
    BUNDLE_SCHEMA_VERSION,
    PHASE_ORDER,
    CanonicalEvidenceEntry,
    CanonicalRecoveryEvidenceBundle,
)


def test_bound_decision_is_verified_but_never_authorizes_recovery() -> None:
    bundle = _bundle()
    decision = _decision(bundle)
    first = detect_recovery_decision_tampering(
        bundle, decision, evaluated_at_epoch_seconds=200
    )
    second = detect_recovery_decision_tampering(
        bundle, decision, evaluated_at_epoch_seconds=200
    )
    validate_recovery_decision_tamper_report(first)
    assert first.status == "VERIFIED"
    assert first.report_hash == second.report_hash
    assert first.decision_id_hash != "decision-1"
    assert first.recovery_authorized is False


def test_bundle_substitution_and_readiness_claim_mismatch_are_tampered() -> None:
    bundle = _bundle()
    substituted = _decision(bundle, bundle_hash="f" * 64)
    report = detect_recovery_decision_tampering(
        bundle, substituted, evaluated_at_epoch_seconds=200
    )
    assert report.status == "TAMPERED"
    assert "RECOVERY_DECISION_BUNDLE_MISMATCH" in report.reasons
    claim = _decision(bundle, ready=False)
    report = detect_recovery_decision_tampering(bundle, claim, evaluated_at_epoch_seconds=200)
    assert "RECOVERY_DECISION_READINESS_CLAIM_MISMATCH" in report.reasons


def test_exact_expiry_and_ttl_boundaries_pass_then_expire() -> None:
    bundle = _bundle()
    exact = _decision(bundle, created=100, expires=400)
    assert detect_recovery_decision_tampering(
        bundle, exact, evaluated_at_epoch_seconds=400
    ).status == "VERIFIED"
    assert detect_recovery_decision_tampering(
        bundle, exact, evaluated_at_epoch_seconds=401
    ).status == "STALE"
    too_long = _decision(bundle, created=100, expires=401)
    assert detect_recovery_decision_tampering(
        bundle, too_long, evaluated_at_epoch_seconds=200
    ).status == "DENIED"


def test_incomplete_future_and_wrong_action_fail_closed() -> None:
    bundle = _bundle()
    assert detect_recovery_decision_tampering(
        bundle, _decision(bundle, complete=False), evaluated_at_epoch_seconds=200
    ).status == "INCOMPLETE"
    assert detect_recovery_decision_tampering(
        bundle, _decision(bundle, created=201, expires=300), evaluated_at_epoch_seconds=200
    ).status == "DENIED"
    wrong = _decision(bundle, action="DENY_RECOVERY")
    assert detect_recovery_decision_tampering(
        bundle, wrong, evaluated_at_epoch_seconds=200
    ).status == "DENIED"


def test_malformed_bound_decision_and_bundle_tampering_fail_closed() -> None:
    bundle = _bundle()
    with pytest.raises(RecoveryDecisionTamperDetectionError, match="DECISION_FIELD_INVALID"):
        _decision(bundle, action="RESTART_NOW")
    with pytest.raises(RecoveryDecisionTamperDetectionError, match="DETECTOR_BOUND_INVALID"):
        detect_recovery_decision_tampering(
            bundle, _decision(bundle), evaluated_at_epoch_seconds=True
        )
    decision = _decision(bundle)
    with pytest.raises(RecoveryDecisionTamperDetectionError, match="DECISION_HASH_MISMATCH"):
        detect_recovery_decision_tampering(
            bundle, replace(decision, target="other"), evaluated_at_epoch_seconds=200
        )
    with pytest.raises(RecoveryDecisionTamperDetectionError, match="BUNDLE_INVALID"):
        detect_recovery_decision_tampering(
            replace(bundle, bundle_hash="0" * 64), decision, evaluated_at_epoch_seconds=200
        )


def test_report_hash_and_safety_tampering_fail_closed() -> None:
    bundle = _bundle()
    report = detect_recovery_decision_tampering(
        bundle, _decision(bundle), evaluated_at_epoch_seconds=200
    )
    with pytest.raises(RecoveryDecisionTamperDetectionError, match="REPORT_HASH_MISMATCH"):
        validate_recovery_decision_tamper_report(replace(report, report_hash="0" * 64))
    with pytest.raises(
        RecoveryDecisionTamperDetectionError, match="REPORT_SAFETY_BOUNDARY_INVALID"
    ):
        validate_recovery_decision_tamper_report(replace(report, service_control_authorized=True))


def test_detector_has_no_query_control_notification_or_mutation_surface() -> None:
    names = set(detect_recovery_decision_tampering.__code__.co_names)
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


def _decision(
    bundle,
    *,
    bundle_hash=None,
    ready=True,
    complete=True,
    created=100,
    expires=300,
    action="AWAIT_RECOVERY_POLICY",
):
    return make_recovery_decision_record(
        decision_id="decision-1",
        created_at_epoch_seconds=created,
        expires_at_epoch_seconds=expires,
        action=action,
        target="kalshi-fixed-rate-refresh.service",
        bundle_hash=bundle_hash or bundle.bundle_hash,
        prerequisites_ready_claim=ready,
        complete=complete,
        source_identity_hash="b" * 64,
    )


def _bundle():
    statuses = ("STABLE", "AVAILABLE", "HEALTHY", "REACHABLE", "HEALTHY", "PASSED", "PASSED")
    entries = tuple(
        CanonicalEvidenceEntry(
            phase=phase,
            status=status,
            evidence_hash=_hash(f"{phase}-evidence"),
            evidence_age_seconds=1,
        )
        for phase, status in zip(PHASE_ORDER, statuses, strict=True)
    )
    entries_payload = [asdict(entry) for entry in entries]
    unsigned = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "status": "READY",
        "reasons": [],
        "entries": entries_payload,
        "phase_order": list(PHASE_ORDER),
        "observed_max_age_seconds": 1,
        "max_evidence_age_seconds": 120,
        "source_set_hash": _hash(["a" * 64] * 7),
        "entries_hash": _hash(entries_payload),
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
        observed_max_age_seconds=1,
        max_evidence_age_seconds=120,
        source_set_hash=unsigned["source_set_hash"],
        entries_hash=unsigned["entries_hash"],
        bundle_hash=_hash(unsigned),
        canonicalization_complete=True,
        prerequisites_ready=True,
    )


def _hash(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
