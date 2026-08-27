from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

CLASSIFIER_SCHEMA_VERSION = "phase4hd-keepalive-gap-classifier-v1"
GapStatus = Literal["HEALTHY", "GAP_DETECTED", "STALE", "INCOMPLETE"]


class KeepaliveGapClassifierError(ValueError):
    """Stable fail-closed keepalive gap classification error."""


@dataclass(frozen=True)
class KeepaliveSample:
    sequence: int
    observed_at_epoch_seconds: int
    present: bool
    complete: bool
    evidence_age_seconds: int
    source_identity_hash: str
    sample_hash: str


@dataclass(frozen=True)
class KeepaliveGapClassification:
    status: GapStatus
    reasons: tuple[str, ...]
    source_identity_hash: str
    sample_count: int
    gap_count: int
    missing_sample_count: int
    observed_max_gap_seconds: int
    max_gap_seconds: int
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    samples_hash: str
    classification_hash: str
    read_only: bool = True
    alert_required: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_keepalive_sample(
    *,
    sequence: int,
    observed_at_epoch_seconds: int,
    present: bool,
    complete: bool,
    evidence_age_seconds: int,
    source_identity_hash: str,
) -> KeepaliveSample:
    unsigned = {
        "sequence": sequence,
        "observed_at_epoch_seconds": observed_at_epoch_seconds,
        "present": present,
        "complete": complete,
        "evidence_age_seconds": evidence_age_seconds,
        "source_identity_hash": source_identity_hash,
    }
    _validate_sample_fields(unsigned)
    return KeepaliveSample(**unsigned, sample_hash=_hash(unsigned))


def classify_keepalive_gaps(
    samples: Sequence[Any],
    *,
    max_samples: int = 128,
    max_gap_seconds: int = 60,
    max_evidence_age_seconds: int = 120,
) -> KeepaliveGapClassification:
    for bound in (max_samples, max_gap_seconds):
        if isinstance(bound, bool) or not isinstance(bound, int) or bound <= 0:
            raise KeepaliveGapClassifierError("CLASSIFIER_BOUND_INVALID")
    if (
        isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise KeepaliveGapClassifierError("CLASSIFIER_BOUND_INVALID")
    if not samples:
        raise KeepaliveGapClassifierError("SAMPLES_EMPTY")
    if len(samples) > max_samples:
        raise KeepaliveGapClassifierError("SAMPLE_BOUND_EXCEEDED")

    validated = [_validated_sample(item) for item in samples]
    if len({item.sequence for item in validated}) != len(validated):
        raise KeepaliveGapClassifierError("SAMPLE_SEQUENCE_DUPLICATE")
    if len({item.source_identity_hash for item in validated}) != 1:
        raise KeepaliveGapClassifierError("SAMPLE_LINEAGE_MIXED")
    ordered = sorted(validated, key=lambda item: item.sequence)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.observed_at_epoch_seconds <= previous.observed_at_epoch_seconds:
            raise KeepaliveGapClassifierError("SAMPLE_TIME_NOT_MONOTONIC")

    gaps = [
        current.observed_at_epoch_seconds - previous.observed_at_epoch_seconds
        for previous, current in zip(ordered, ordered[1:], strict=False)
    ]
    missing_sample_count = sum(
        max(0, current.sequence - previous.sequence - 1)
        for previous, current in zip(ordered, ordered[1:], strict=False)
    )
    observed_gap = max(gaps, default=0)
    gap_count = sum(gap > max_gap_seconds for gap in gaps)
    observed_age = max(item.evidence_age_seconds for item in ordered)
    incomplete_reasons = [
        f"SAMPLE_INCOMPLETE:{item.sequence}" for item in ordered if not item.complete
    ]
    incomplete_reasons.extend(
        f"KEEPALIVE_ABSENT:{item.sequence}" for item in ordered if not item.present
    )
    if len(ordered) < 2:
        incomplete_reasons.append("SAMPLE_PAIR_REQUIRED")
    if missing_sample_count:
        incomplete_reasons.append("SAMPLE_SEQUENCE_GAP")

    if observed_age > max_evidence_age_seconds:
        status: GapStatus = "STALE"
        reasons = ["KEEPALIVE_EVIDENCE_STALE"]
    elif incomplete_reasons:
        status = "INCOMPLETE"
        reasons = sorted(incomplete_reasons)
    elif gap_count:
        status = "GAP_DETECTED"
        reasons = ["KEEPALIVE_GAP_EXCEEDED"]
    else:
        status = "HEALTHY"
        reasons = []

    samples_hash = _hash([asdict(item) for item in ordered])
    unsigned = {
        "schema_version": CLASSIFIER_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "source_identity_hash": ordered[0].source_identity_hash,
        "sample_count": len(ordered),
        "gap_count": gap_count,
        "missing_sample_count": missing_sample_count,
        "observed_max_gap_seconds": observed_gap,
        "max_gap_seconds": max_gap_seconds,
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "samples_hash": samples_hash,
        "read_only": True,
        "alert_required": status != "HEALTHY",
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return KeepaliveGapClassification(
        status=status,
        reasons=tuple(reasons),
        source_identity_hash=ordered[0].source_identity_hash,
        sample_count=len(ordered),
        gap_count=gap_count,
        missing_sample_count=missing_sample_count,
        observed_max_gap_seconds=observed_gap,
        max_gap_seconds=max_gap_seconds,
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        samples_hash=samples_hash,
        classification_hash=_hash(unsigned),
        alert_required=unsigned["alert_required"],
    )


def validate_keepalive_gap_classification(classification: Any) -> None:
    if not isinstance(classification, KeepaliveGapClassification):
        raise KeepaliveGapClassifierError("CLASSIFICATION_TYPE_INVALID")
    if classification.read_only is not True or any(
        (
            classification.recovery_authorized,
            classification.service_control_authorized,
            classification.host_restart_authorized,
            classification.execution_authorized,
        )
    ):
        raise KeepaliveGapClassifierError("CLASSIFICATION_SAFETY_BOUNDARY_INVALID")
    healthy = not classification.reasons and not classification.alert_required
    if (classification.status == "HEALTHY") != healthy:
        raise KeepaliveGapClassifierError("CLASSIFICATION_STATUS_INVALID")
    unsigned = asdict(classification)
    unsigned.pop("classification_hash")
    unsigned["schema_version"] = CLASSIFIER_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if classification.classification_hash != _hash(unsigned):
        raise KeepaliveGapClassifierError("CLASSIFICATION_HASH_MISMATCH")


def _validated_sample(value: Any) -> KeepaliveSample:
    if not isinstance(value, KeepaliveSample):
        raise KeepaliveGapClassifierError("SAMPLE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("sample_hash")
    _validate_sample_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise KeepaliveGapClassifierError("SAMPLE_HASH_MISMATCH")
    return value


def _validate_sample_fields(payload: dict[str, Any]) -> None:
    for key in ("present", "complete"):
        if not isinstance(payload[key], bool):
            raise KeepaliveGapClassifierError("SAMPLE_FIELD_INVALID")
    if not isinstance(payload["source_identity_hash"], str) or not payload[
        "source_identity_hash"
    ].strip():
        raise KeepaliveGapClassifierError("SAMPLE_FIELD_INVALID")
    for key in ("sequence", "observed_at_epoch_seconds", "evidence_age_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise KeepaliveGapClassifierError("SAMPLE_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
