from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

GATE_SCHEMA_VERSION = "phase4gm-evidence-latency-workstream-gate-v1"
GateStatus = Literal["READY", "BLOCKED", "STALE"]

REQUIRED_OUTCOMES = {
    "memory_bounds": "WITHIN_BOUNDS",
    "provenance_integrity": "INTACT",
    "cold_start_seed": "PROPOSE",
    "regression_corpus": "READY",
    "plan_drift": "STABLE",
}


class EvidenceLatencyWorkstreamGateError(ValueError):
    """Stable fail-closed evidence-latency gate error."""


@dataclass(frozen=True)
class EvidenceLatencyGateArtifact:
    stage: str
    upstream_schema_version: str
    artifact_hash: str
    source_identity_hash: str
    source_watermark: str
    outcome: str
    evidence_age_seconds: int
    complete: bool
    envelope_hash: str


@dataclass(frozen=True)
class EvidenceLatencyWorkstreamGate:
    status: GateStatus
    reasons: tuple[str, ...]
    source_identity_hash: str
    source_watermark: str
    artifact_count: int
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    artifact_set_hash: str
    gate_hash: str
    execution_authorized: bool = False


def make_evidence_latency_gate_artifact(
    *,
    stage: str,
    upstream_schema_version: str,
    artifact_hash: str,
    source_identity_hash: str,
    source_watermark: str,
    outcome: str,
    evidence_age_seconds: int,
    complete: bool = True,
) -> EvidenceLatencyGateArtifact:
    unsigned = {
        "stage": stage,
        "upstream_schema_version": upstream_schema_version,
        "artifact_hash": artifact_hash,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "outcome": outcome,
        "evidence_age_seconds": evidence_age_seconds,
        "complete": complete,
    }
    _validate_artifact_fields(unsigned)
    return EvidenceLatencyGateArtifact(**unsigned, envelope_hash=_hash(unsigned))


def evaluate_evidence_latency_workstream_gate(
    artifacts: Sequence[Any],
    *,
    max_artifacts: int = 5,
    max_evidence_age_seconds: int = 300,
) -> EvidenceLatencyWorkstreamGate:
    if (
        isinstance(max_artifacts, bool)
        or not isinstance(max_artifacts, int)
        or max_artifacts <= 0
        or isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise EvidenceLatencyWorkstreamGateError("GATE_BOUND_INVALID")
    if not artifacts:
        raise EvidenceLatencyWorkstreamGateError("ARTIFACTS_EMPTY")
    if len(artifacts) > max_artifacts:
        raise EvidenceLatencyWorkstreamGateError("ARTIFACT_BOUND_EXCEEDED")

    validated = [_validated_artifact(item) for item in artifacts]
    stages = [item.stage for item in validated]
    if len(set(stages)) != len(stages):
        raise EvidenceLatencyWorkstreamGateError("ARTIFACT_STAGE_DUPLICATE")
    missing = sorted(set(REQUIRED_OUTCOMES) - set(stages))
    extra = sorted(set(stages) - set(REQUIRED_OUTCOMES))
    if missing or extra:
        raise EvidenceLatencyWorkstreamGateError("ARTIFACT_STAGE_SET_INVALID")
    identities = {item.source_identity_hash for item in validated}
    watermarks = {item.source_watermark for item in validated}
    if len(identities) != 1 or len(watermarks) != 1:
        raise EvidenceLatencyWorkstreamGateError("SOURCE_LINEAGE_MIXED")

    ordered = sorted(validated, key=lambda item: item.stage)
    observed_age = max(item.evidence_age_seconds for item in ordered)
    reasons: list[str] = []
    if observed_age > max_evidence_age_seconds:
        status: GateStatus = "STALE"
        reasons.append("WORKSTREAM_EVIDENCE_STALE")
    else:
        for item in ordered:
            if not item.complete:
                reasons.append(f"{item.stage.upper()}_INCOMPLETE")
            elif item.outcome != REQUIRED_OUTCOMES[item.stage]:
                reasons.append(f"{item.stage.upper()}_NOT_READY")
        status = "BLOCKED" if reasons else "READY"

    artifact_set_hash = _hash([asdict(item) for item in ordered])
    identity = ordered[0].source_identity_hash
    watermark = ordered[0].source_watermark
    unsigned = {
        "schema_version": GATE_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "source_identity_hash": identity,
        "source_watermark": watermark,
        "artifact_count": len(ordered),
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "artifact_set_hash": artifact_set_hash,
        "execution_authorized": False,
    }
    return EvidenceLatencyWorkstreamGate(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        source_identity_hash=identity,
        source_watermark=watermark,
        artifact_count=len(ordered),
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        artifact_set_hash=artifact_set_hash,
        gate_hash=_hash(unsigned),
    )


def validate_evidence_latency_workstream_gate(gate: Any) -> None:
    if not isinstance(gate, EvidenceLatencyWorkstreamGate):
        raise EvidenceLatencyWorkstreamGateError("GATE_RESULT_TYPE_INVALID")
    if gate.execution_authorized is not False:
        raise EvidenceLatencyWorkstreamGateError("GATE_SAFETY_BOUNDARY_INVALID")
    if gate.status == "READY" and gate.reasons:
        raise EvidenceLatencyWorkstreamGateError("READY_STATE_INVALID")
    if gate.status in {"BLOCKED", "STALE"} and not gate.reasons:
        raise EvidenceLatencyWorkstreamGateError("NON_READY_REASONS_MISSING")
    unsigned = asdict(gate)
    unsigned.pop("gate_hash")
    unsigned["schema_version"] = GATE_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    if gate.gate_hash != _hash(unsigned):
        raise EvidenceLatencyWorkstreamGateError("GATE_HASH_MISMATCH")


def _validated_artifact(value: Any) -> EvidenceLatencyGateArtifact:
    if not isinstance(value, EvidenceLatencyGateArtifact):
        raise EvidenceLatencyWorkstreamGateError("ARTIFACT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("envelope_hash")
    _validate_artifact_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise EvidenceLatencyWorkstreamGateError("ARTIFACT_HASH_MISMATCH")
    return value


def _validate_artifact_fields(payload: dict[str, Any]) -> None:
    for key in (
        "stage",
        "upstream_schema_version",
        "artifact_hash",
        "source_identity_hash",
        "source_watermark",
        "outcome",
    ):
        if not isinstance(payload[key], str) or not payload[key]:
            raise EvidenceLatencyWorkstreamGateError("ARTIFACT_FIELD_INVALID")
    if payload["stage"] not in REQUIRED_OUTCOMES:
        raise EvidenceLatencyWorkstreamGateError("ARTIFACT_STAGE_INVALID")
    age = payload["evidence_age_seconds"]
    if isinstance(age, bool) or not isinstance(age, int) or age < 0:
        raise EvidenceLatencyWorkstreamGateError("ARTIFACT_FIELD_INVALID")
    if not isinstance(payload["complete"], bool):
        raise EvidenceLatencyWorkstreamGateError("ARTIFACT_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
