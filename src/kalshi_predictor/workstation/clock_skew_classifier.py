from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

CLASSIFIER_SCHEMA_VERSION = "phase4ic-clock-skew-classifier-v1"
ClockStatus = Literal["SYNCHRONIZED", "SKEWED", "UNKNOWN", "INCOMPLETE", "TAMPERED"]
TRUSTED_REFERENCE_SOURCES = frozenset({"NTP_SYNCED", "WINDOWS_HOST_CLOCK", "SIGNED_TIME_SOURCE"})


class ClockSkewClassifierError(ValueError):
    """Stable fail-closed clock skew classifier error."""


@dataclass(frozen=True)
class ClockComparisonEvidence:
    probe_id_hash: str
    observed_at_epoch_seconds: int
    local_epoch_seconds: int
    reference_epoch_seconds: int
    reference_source: str
    uncertainty_seconds: int
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class ClockSkewDecision:
    status: ClockStatus
    reasons: tuple[str, ...]
    evidence_hash: str
    reference_source: str
    evaluated_at_epoch_seconds: int
    maximum_skew_seconds: int
    maximum_uncertainty_seconds: int
    absolute_skew_seconds: int
    decision_hash: str
    read_only: bool = True
    clock_synchronization_proven: bool = False
    operator_alert_required: bool = True
    restart_eligible: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_clock_comparison_evidence(**fields: Any) -> ClockComparisonEvidence:
    _validate_fields(fields)
    return ClockComparisonEvidence(**fields, evidence_hash=_hash(fields))


def classify_clock_skew(
    evidence: Any,
    *,
    evaluated_at_epoch_seconds: int,
    maximum_skew_seconds: int = 5,
    maximum_uncertainty_seconds: int = 2,
    maximum_age_seconds: int = 120,
) -> ClockSkewDecision:
    for value in (
        evaluated_at_epoch_seconds,
        maximum_skew_seconds,
        maximum_uncertainty_seconds,
        maximum_age_seconds,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ClockSkewClassifierError("CLOCK_CLASSIFIER_BOUND_INVALID")
    item = _validated_evidence(evidence)
    skew = abs(item.local_epoch_seconds - item.reference_epoch_seconds)
    if item.observed_at_epoch_seconds > evaluated_at_epoch_seconds:
        status: ClockStatus = "TAMPERED"
        reasons = ["CLOCK_EVIDENCE_FROM_FUTURE"]
    elif evaluated_at_epoch_seconds - item.observed_at_epoch_seconds > maximum_age_seconds:
        status = "UNKNOWN"
        reasons = ["CLOCK_EVIDENCE_STALE"]
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["CLOCK_EVIDENCE_INCOMPLETE"]
    elif item.reference_source not in TRUSTED_REFERENCE_SOURCES:
        status = "UNKNOWN"
        reasons = [f"CLOCK_REFERENCE_UNTRUSTED:{item.reference_source}"]
    elif item.uncertainty_seconds > maximum_uncertainty_seconds:
        status = "UNKNOWN"
        reasons = ["CLOCK_REFERENCE_UNCERTAINTY_EXCEEDED"]
    elif skew > maximum_skew_seconds:
        status = "SKEWED"
        reasons = ["CLOCK_SKEW_THRESHOLD_EXCEEDED"]
    else:
        status = "SYNCHRONIZED"
        reasons = []
    synchronized = status == "SYNCHRONIZED"
    unsigned = {
        "schema_version": CLASSIFIER_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "evidence_hash": item.evidence_hash,
        "reference_source": item.reference_source,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "maximum_skew_seconds": maximum_skew_seconds,
        "maximum_uncertainty_seconds": maximum_uncertainty_seconds,
        "absolute_skew_seconds": skew,
        "read_only": True,
        "clock_synchronization_proven": synchronized,
        "operator_alert_required": not synchronized,
        "restart_eligible": False,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return ClockSkewDecision(
        status=status,
        reasons=tuple(reasons),
        evidence_hash=item.evidence_hash,
        reference_source=item.reference_source,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        maximum_skew_seconds=maximum_skew_seconds,
        maximum_uncertainty_seconds=maximum_uncertainty_seconds,
        absolute_skew_seconds=skew,
        decision_hash=_hash(unsigned),
        clock_synchronization_proven=synchronized,
        operator_alert_required=not synchronized,
    )


def validate_clock_skew_decision(value: Any) -> None:
    if not isinstance(value, ClockSkewDecision):
        raise ClockSkewClassifierError("CLOCK_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.restart_eligible is not False
        or any(
            (
                value.recovery_authorized,
                value.service_control_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise ClockSkewClassifierError("CLOCK_SAFETY_BOUNDARY_INVALID")
    if value.clock_synchronization_proven != (value.status == "SYNCHRONIZED"):
        raise ClockSkewClassifierError("CLOCK_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = CLASSIFIER_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise ClockSkewClassifierError("CLOCK_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> ClockComparisonEvidence:
    if not isinstance(value, ClockComparisonEvidence):
        raise ClockSkewClassifierError("CLOCK_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise ClockSkewClassifierError("CLOCK_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "probe_id_hash",
        "observed_at_epoch_seconds",
        "local_epoch_seconds",
        "reference_epoch_seconds",
        "reference_source",
        "uncertainty_seconds",
        "complete",
    }
    if (
        set(fields) != required
        or not isinstance(fields["probe_id_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["probe_id_hash"]) is None
    ):
        raise ClockSkewClassifierError("CLOCK_EVIDENCE_FIELD_INVALID")
    for key in (
        "observed_at_epoch_seconds",
        "local_epoch_seconds",
        "reference_epoch_seconds",
        "uncertainty_seconds",
    ):
        value = fields[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ClockSkewClassifierError("CLOCK_EVIDENCE_FIELD_INVALID")
    if (
        not isinstance(fields["reference_source"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["reference_source"]) is None
        or not isinstance(fields["complete"], bool)
    ):
        raise ClockSkewClassifierError("CLOCK_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
