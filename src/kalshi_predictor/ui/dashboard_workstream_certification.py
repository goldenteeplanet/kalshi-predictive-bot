from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

CERTIFICATION_SCHEMA_VERSION = "phase4gz-dashboard-workstream-certification-v1"
REQUIRED_PHASES = (
    "4GN",
    "4GO",
    "4GP",
    "4GQ",
    "4GR",
    "4GS",
    "4GT",
    "4GU",
    "4GV",
    "4GW",
    "4GX",
    "4GY",
)
CertificationStatus = Literal["CERTIFIED", "BLOCKED", "STALE"]


class DashboardWorkstreamCertificationError(ValueError):
    """Stable fail-closed dashboard-workstream certification error."""


@dataclass(frozen=True)
class DashboardPhaseEvidence:
    phase: str
    artifact_schema_version: str
    artifact_hash: str
    passed: bool
    complete: bool
    source_identity_hash: str
    source_watermark: str
    evidence_age_seconds: int
    evidence_hash: str


@dataclass(frozen=True)
class DashboardWorkstreamCertification:
    status: CertificationStatus
    reasons: tuple[str, ...]
    source_identity_hash: str
    source_watermark: str
    required_phases: tuple[str, ...]
    evidence_count: int
    failed_phase_count: int
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    evidence_manifest_hash: str
    certification_hash: str
    read_only: bool = True
    execution_authorized: bool = False


def make_dashboard_phase_evidence(
    *,
    phase: str,
    artifact_schema_version: str,
    artifact_hash: str,
    passed: bool,
    complete: bool,
    source_identity_hash: str,
    source_watermark: str,
    evidence_age_seconds: int,
) -> DashboardPhaseEvidence:
    unsigned = {
        "phase": phase,
        "artifact_schema_version": artifact_schema_version,
        "artifact_hash": artifact_hash,
        "passed": passed,
        "complete": complete,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "evidence_age_seconds": evidence_age_seconds,
    }
    _validate_evidence_fields(unsigned)
    return DashboardPhaseEvidence(**unsigned, evidence_hash=_hash(unsigned))


def certify_dashboard_workstream(
    evidence: Sequence[Any],
    *,
    max_evidence_items: int = len(REQUIRED_PHASES),
    max_evidence_age_seconds: int = 300,
) -> DashboardWorkstreamCertification:
    if (
        isinstance(max_evidence_items, bool)
        or not isinstance(max_evidence_items, int)
        or max_evidence_items <= 0
    ):
        raise DashboardWorkstreamCertificationError("CERTIFICATION_BOUND_INVALID")
    if (
        isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise DashboardWorkstreamCertificationError("CERTIFICATION_BOUND_INVALID")
    if not evidence:
        raise DashboardWorkstreamCertificationError("EVIDENCE_EMPTY")
    if len(evidence) > max_evidence_items:
        raise DashboardWorkstreamCertificationError("EVIDENCE_BOUND_EXCEEDED")

    validated = [_validated_evidence(item) for item in evidence]
    phases = [item.phase for item in validated]
    if len(set(phases)) != len(phases):
        raise DashboardWorkstreamCertificationError("PHASE_DUPLICATE")
    if set(phases) != set(REQUIRED_PHASES):
        raise DashboardWorkstreamCertificationError("REQUIRED_PHASE_SET_MISMATCH")
    identities = {item.source_identity_hash for item in validated}
    watermarks = {item.source_watermark for item in validated}
    if len(identities) != 1 or len(watermarks) != 1:
        raise DashboardWorkstreamCertificationError("EVIDENCE_LINEAGE_MIXED")

    ordered = sorted(validated, key=lambda item: item.phase)
    observed_age = max(item.evidence_age_seconds for item in ordered)
    failed = [item.phase for item in ordered if not item.passed or not item.complete]
    if observed_age > max_evidence_age_seconds:
        status: CertificationStatus = "STALE"
        reasons = ["DASHBOARD_WORKSTREAM_EVIDENCE_STALE"]
    elif failed:
        status = "BLOCKED"
        reasons = [f"PHASE_NOT_CERTIFIED:{phase}" for phase in failed]
    else:
        status = "CERTIFIED"
        reasons = []

    manifest_hash = _hash([asdict(item) for item in ordered])
    unsigned = {
        "schema_version": CERTIFICATION_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "source_identity_hash": ordered[0].source_identity_hash,
        "source_watermark": ordered[0].source_watermark,
        "required_phases": list(REQUIRED_PHASES),
        "evidence_count": len(ordered),
        "failed_phase_count": len(failed),
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "evidence_manifest_hash": manifest_hash,
        "read_only": True,
        "execution_authorized": False,
    }
    return DashboardWorkstreamCertification(
        status=status,
        reasons=tuple(reasons),
        source_identity_hash=ordered[0].source_identity_hash,
        source_watermark=ordered[0].source_watermark,
        required_phases=REQUIRED_PHASES,
        evidence_count=len(ordered),
        failed_phase_count=len(failed),
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        evidence_manifest_hash=manifest_hash,
        certification_hash=_hash(unsigned),
    )


def validate_dashboard_workstream_certification(certification: Any) -> None:
    if not isinstance(certification, DashboardWorkstreamCertification):
        raise DashboardWorkstreamCertificationError("CERTIFICATION_RESULT_TYPE_INVALID")
    if certification.read_only is not True or certification.execution_authorized is not False:
        raise DashboardWorkstreamCertificationError("CERTIFICATION_SAFETY_BOUNDARY_INVALID")
    if certification.status == "CERTIFIED" and (
        certification.reasons or certification.failed_phase_count
    ):
        raise DashboardWorkstreamCertificationError("CERTIFIED_STATE_INVALID")
    if certification.status == "BLOCKED" and not certification.reasons:
        raise DashboardWorkstreamCertificationError("BLOCKED_REASONS_MISSING")
    if certification.status == "STALE" and not certification.reasons:
        raise DashboardWorkstreamCertificationError("STALE_REASONS_MISSING")
    unsigned = asdict(certification)
    unsigned.pop("certification_hash")
    unsigned["schema_version"] = CERTIFICATION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["required_phases"] = list(unsigned["required_phases"])
    if certification.certification_hash != _hash(unsigned):
        raise DashboardWorkstreamCertificationError("CERTIFICATION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> DashboardPhaseEvidence:
    if not isinstance(value, DashboardPhaseEvidence):
        raise DashboardWorkstreamCertificationError("EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("evidence_hash")
    _validate_evidence_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise DashboardWorkstreamCertificationError("EVIDENCE_HASH_MISMATCH")
    return value


def _validate_evidence_fields(payload: dict[str, Any]) -> None:
    for key in (
        "phase",
        "artifact_schema_version",
        "artifact_hash",
        "source_identity_hash",
        "source_watermark",
    ):
        if not isinstance(payload[key], str) or not payload[key]:
            raise DashboardWorkstreamCertificationError("EVIDENCE_FIELD_INVALID")
    if len(payload["artifact_hash"]) != 64:
        raise DashboardWorkstreamCertificationError("ARTIFACT_HASH_INVALID")
    for key in ("passed", "complete"):
        if not isinstance(payload[key], bool):
            raise DashboardWorkstreamCertificationError("EVIDENCE_FIELD_INVALID")
    age = payload["evidence_age_seconds"]
    if isinstance(age, bool) or not isinstance(age, int) or age < 0:
        raise DashboardWorkstreamCertificationError("EVIDENCE_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
