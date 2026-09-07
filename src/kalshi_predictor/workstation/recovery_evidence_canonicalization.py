from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .authoritative_scheduler_health import (
    SchedulerHealthEvidence,
    validate_scheduler_health_evidence,
)
from .keepalive_gap_classifier import (
    KeepaliveGapClassification,
    validate_keepalive_gap_classification,
)
from .protected_invariant_recovery_gate import (
    ProtectedInvariantGateResult,
    validate_protected_invariant_gate_result,
)
from .user_systemd_reachability import (
    UserSystemdReachabilityEvidence,
    validate_user_systemd_reachability_evidence,
)
from .writer_exclusivity_recovery_gate import (
    WriterExclusivityGateResult,
    validate_writer_exclusivity_gate_result,
)
from .wsl_boot_identity_monitor import (
    WslBootIdentityMonitorResult,
    validate_wsl_boot_identity_monitor_result,
)
from .wsl_liveness_evidence import WslLivenessEvidence, validate_wsl_liveness_evidence

BUNDLE_SCHEMA_VERSION = "phase4hi-recovery-evidence-canonicalization-v1"
BundleStatus = Literal["READY", "DENIED", "STALE", "INCOMPLETE"]
PHASE_ORDER = ("4HB", "4HC", "4HD", "4HE", "4HF", "4HG", "4HH")


class RecoveryEvidenceCanonicalizationError(ValueError):
    """Stable fail-closed recovery evidence canonicalization error."""


@dataclass(frozen=True)
class CanonicalEvidenceEntry:
    phase: str
    status: str
    evidence_hash: str
    evidence_age_seconds: int


@dataclass(frozen=True)
class CanonicalRecoveryEvidenceBundle:
    status: BundleStatus
    reasons: tuple[str, ...]
    entries: tuple[CanonicalEvidenceEntry, ...]
    phase_order: tuple[str, ...]
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    source_set_hash: str
    entries_hash: str
    bundle_hash: str
    read_only: bool = True
    canonicalization_complete: bool = False
    prerequisites_ready: bool = False
    alert_required: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def canonicalize_recovery_evidence(
    *,
    boot_identity: Any,
    wsl_liveness: Any,
    keepalive_gap: Any,
    user_systemd: Any,
    scheduler_health: Any,
    writer_gate: Any,
    invariant_gate: Any,
    max_evidence_age_seconds: int = 120,
) -> CanonicalRecoveryEvidenceBundle:
    if (
        isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise RecoveryEvidenceCanonicalizationError("BUNDLE_BOUND_INVALID")
    values = (
        (
            "4HB",
            boot_identity,
            WslBootIdentityMonitorResult,
            validate_wsl_boot_identity_monitor_result,
        ),
        ("4HC", wsl_liveness, WslLivenessEvidence, validate_wsl_liveness_evidence),
        ("4HD", keepalive_gap, KeepaliveGapClassification, validate_keepalive_gap_classification),
        (
            "4HE",
            user_systemd,
            UserSystemdReachabilityEvidence,
            validate_user_systemd_reachability_evidence,
        ),
        ("4HF", scheduler_health, SchedulerHealthEvidence, validate_scheduler_health_evidence),
        ("4HG", writer_gate, WriterExclusivityGateResult, validate_writer_exclusivity_gate_result),
        (
            "4HH",
            invariant_gate,
            ProtectedInvariantGateResult,
            validate_protected_invariant_gate_result,
        ),
    )
    entries = []
    source_hashes = []
    for phase, value, expected_type, validator in values:
        if not isinstance(value, expected_type):
            raise RecoveryEvidenceCanonicalizationError(f"EVIDENCE_TYPE_INVALID:{phase}")
        try:
            validator(value)
        except ValueError as exc:
            raise RecoveryEvidenceCanonicalizationError(f"EVIDENCE_INVALID:{phase}") from exc
        entries.append(
            CanonicalEvidenceEntry(
                phase=phase,
                status=value.status,
                evidence_hash=_evidence_hash(value),
                evidence_age_seconds=_evidence_age(value),
            )
        )
        source_hashes.append(_source_hash(value))

    observed_age = max(entry.evidence_age_seconds for entry in entries)
    expected_statuses = {
        "4HB": "STABLE",
        "4HC": "AVAILABLE",
        "4HD": "HEALTHY",
        "4HE": "REACHABLE",
        "4HF": "HEALTHY",
        "4HG": "PASSED",
        "4HH": "PASSED",
    }
    incomplete_statuses = {"INCOMPLETE", "DEGRADED"}
    if observed_age > max_evidence_age_seconds or any(entry.status == "STALE" for entry in entries):
        status: BundleStatus = "STALE"
        reasons = ["RECOVERY_EVIDENCE_STALE"]
    elif any(entry.status in incomplete_statuses for entry in entries):
        status = "INCOMPLETE"
        reasons = [
            f"RECOVERY_EVIDENCE_INCOMPLETE:{entry.phase}"
            for entry in entries
            if entry.status in incomplete_statuses
        ]
    else:
        reasons = [
            f"RECOVERY_EVIDENCE_STATUS_DENIED:{entry.phase}:{entry.status}"
            for entry in entries
            if entry.status != expected_statuses[entry.phase]
        ]
        status = "DENIED" if reasons else "READY"

    ready = status == "READY"
    entries_payload = [asdict(entry) for entry in entries]
    entries_hash = _hash(entries_payload)
    source_set_hash = _hash(source_hashes)
    unsigned = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "entries": entries_payload,
        "phase_order": list(PHASE_ORDER),
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "source_set_hash": source_set_hash,
        "entries_hash": entries_hash,
        "read_only": True,
        "canonicalization_complete": True,
        "prerequisites_ready": ready,
        "alert_required": not ready,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return CanonicalRecoveryEvidenceBundle(
        status=status,
        reasons=tuple(reasons),
        entries=tuple(entries),
        phase_order=PHASE_ORDER,
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        source_set_hash=source_set_hash,
        entries_hash=entries_hash,
        bundle_hash=_hash(unsigned),
        canonicalization_complete=True,
        prerequisites_ready=ready,
        alert_required=not ready,
    )


def validate_canonical_recovery_evidence_bundle(bundle: Any) -> None:
    if not isinstance(bundle, CanonicalRecoveryEvidenceBundle):
        raise RecoveryEvidenceCanonicalizationError("BUNDLE_TYPE_INVALID")
    if bundle.read_only is not True or any(
        (
            bundle.recovery_authorized,
            bundle.service_control_authorized,
            bundle.host_restart_authorized,
            bundle.execution_authorized,
        )
    ):
        raise RecoveryEvidenceCanonicalizationError("BUNDLE_SAFETY_BOUNDARY_INVALID")
    entry_order = tuple(entry.phase for entry in bundle.entries)
    if entry_order != PHASE_ORDER or bundle.phase_order != PHASE_ORDER:
        raise RecoveryEvidenceCanonicalizationError("BUNDLE_PHASE_ORDER_INVALID")
    ready = (
        not bundle.reasons
        and not bundle.alert_required
        and bundle.canonicalization_complete
        and bundle.prerequisites_ready
    )
    if (bundle.status == "READY") != ready:
        raise RecoveryEvidenceCanonicalizationError("BUNDLE_STATUS_INVALID")
    if bundle.entries_hash != _hash([asdict(entry) for entry in bundle.entries]):
        raise RecoveryEvidenceCanonicalizationError("BUNDLE_ENTRIES_HASH_MISMATCH")
    unsigned = asdict(bundle)
    unsigned.pop("bundle_hash")
    unsigned["schema_version"] = BUNDLE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["entries"] = [asdict(entry) for entry in bundle.entries]
    unsigned["phase_order"] = list(bundle.phase_order)
    if bundle.bundle_hash != _hash(unsigned):
        raise RecoveryEvidenceCanonicalizationError("BUNDLE_HASH_MISMATCH")


def _evidence_hash(value: Any) -> str:
    for name in ("result_hash", "evidence_hash", "classification_hash", "gate_hash"):
        candidate = getattr(value, name, None)
        if isinstance(candidate, str):
            return candidate
    raise RecoveryEvidenceCanonicalizationError("EVIDENCE_HASH_MISSING")


def _evidence_age(value: Any) -> int:
    for name in ("evidence_age_seconds", "observed_max_age_seconds"):
        candidate = getattr(value, name, None)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
    raise RecoveryEvidenceCanonicalizationError("EVIDENCE_AGE_MISSING")


def _source_hash(value: Any) -> str:
    candidate = getattr(value, "source_identity_hash", None)
    if isinstance(candidate, str) and candidate:
        return candidate
    if isinstance(value, ProtectedInvariantGateResult):
        return value.source_identity_hash
    raise RecoveryEvidenceCanonicalizationError("EVIDENCE_SOURCE_MISSING")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
