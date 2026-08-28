from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

REFUSAL_SCHEMA_VERSION = "phase4jo-corrupt-cooldown-state-refusal-v1"
RefusalStatus = Literal["ACCEPTED", "REFUSED", "INCOMPLETE", "TAMPERED"]


class CorruptCooldownStateRefusalError(ValueError):
    """Stable fail-closed corrupt cooldown-state refusal error."""


@dataclass(frozen=True)
class CooldownArtifactEvidence:
    artifact_path_hash: str
    observed_file_hash: str
    expected_chain_head_hash: str
    file_present: bool
    trailing_record_complete: bool
    json_valid: bool
    schema_valid: bool
    record_hashes_valid: bool
    chain_valid: bool
    durability_proven: bool
    evidence_complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class CooldownArtifactDecision:
    status: RefusalStatus
    reasons: tuple[str, ...]
    artifact_path_hash: str
    evidence_hash: str
    decision_hash: str
    read_only: bool = True
    cooldown_state_accepted: bool = False
    repair_permitted: bool = False
    empty_history_assumption_permitted: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_cooldown_artifact_evidence(**fields: Any) -> CooldownArtifactEvidence:
    _validate_fields(fields)
    return CooldownArtifactEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_corrupt_cooldown_state_refusal(evidence: Any) -> CooldownArtifactDecision:
    item = _validated_evidence(evidence)
    if not item.evidence_complete:
        status: RefusalStatus = "INCOMPLETE"
        reasons = ["COOLDOWN_ARTIFACT_EVIDENCE_INCOMPLETE"]
    else:
        reasons = []
        checks = (
            (item.file_present, "COOLDOWN_ARTIFACT_MISSING"),
            (item.trailing_record_complete, "COOLDOWN_ARTIFACT_TRAILING_RECORD_INCOMPLETE"),
            (item.json_valid, "COOLDOWN_ARTIFACT_JSON_INVALID"),
            (item.schema_valid, "COOLDOWN_ARTIFACT_SCHEMA_INVALID"),
            (item.record_hashes_valid, "COOLDOWN_ARTIFACT_RECORD_HASH_INVALID"),
            (item.chain_valid, "COOLDOWN_ARTIFACT_CHAIN_INVALID"),
            (item.durability_proven, "COOLDOWN_ARTIFACT_DURABILITY_UNPROVEN"),
        )
        reasons.extend(reason for passed, reason in checks if not passed)
        status = "REFUSED" if reasons else "ACCEPTED"
    accepted = status == "ACCEPTED"
    unsigned = {
        "schema_version": REFUSAL_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "artifact_path_hash": item.artifact_path_hash,
        "evidence_hash": item.evidence_hash,
        "read_only": True,
        "cooldown_state_accepted": accepted,
        "repair_permitted": False,
        "empty_history_assumption_permitted": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return CooldownArtifactDecision(
        status=status,
        reasons=tuple(reasons),
        artifact_path_hash=item.artifact_path_hash,
        evidence_hash=item.evidence_hash,
        decision_hash=_hash(unsigned),
        cooldown_state_accepted=accepted,
    )


def validate_cooldown_artifact_decision(value: Any) -> None:
    if not isinstance(value, CooldownArtifactDecision):
        raise CorruptCooldownStateRefusalError("COOLDOWN_ARTIFACT_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.cooldown_state_accepted != (value.status == "ACCEPTED")
        or value.repair_permitted is not False
        or value.empty_history_assumption_permitted is not False
        or any(
            (value.restart_authorized, value.service_control_authorized, value.execution_authorized)
        )
    ):
        raise CorruptCooldownStateRefusalError("COOLDOWN_ARTIFACT_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = REFUSAL_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise CorruptCooldownStateRefusalError("COOLDOWN_ARTIFACT_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> CooldownArtifactEvidence:
    if not isinstance(value, CooldownArtifactEvidence):
        raise CorruptCooldownStateRefusalError("COOLDOWN_ARTIFACT_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise CorruptCooldownStateRefusalError("COOLDOWN_ARTIFACT_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "artifact_path_hash",
        "observed_file_hash",
        "expected_chain_head_hash",
        "file_present",
        "trailing_record_complete",
        "json_valid",
        "schema_valid",
        "record_hashes_valid",
        "chain_valid",
        "durability_proven",
        "evidence_complete",
    }
    if set(fields) != required:
        raise CorruptCooldownStateRefusalError("COOLDOWN_ARTIFACT_EVIDENCE_FIELD_INVALID")
    for key in ("artifact_path_hash", "observed_file_hash", "expected_chain_head_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise CorruptCooldownStateRefusalError("COOLDOWN_ARTIFACT_EVIDENCE_FIELD_INVALID")
    for key in (
        "file_present",
        "trailing_record_complete",
        "json_valid",
        "schema_valid",
        "record_hashes_valid",
        "chain_valid",
        "durability_proven",
        "evidence_complete",
    ):
        if not isinstance(fields[key], bool):
            raise CorruptCooldownStateRefusalError("COOLDOWN_ARTIFACT_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
