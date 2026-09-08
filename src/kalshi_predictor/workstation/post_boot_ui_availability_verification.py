from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

VERIFICATION_SCHEMA_VERSION = "phase4ki-post-boot-ui-availability-v1"
VerificationStatus = Literal["PASS", "FAIL", "INCOMPLETE", "TAMPERED"]


class PostBootUiAvailabilityError(ValueError):
    """Stable fail-closed post-boot UI-availability error."""


@dataclass(frozen=True)
class PostBootUiEvidence:
    probe_identity_hash: str
    endpoint_identity_hash: str
    observed_at_epoch_seconds: int
    evidence_age_seconds: int
    duration_milliseconds: int
    http_status_code: int
    response_identity_verified: bool
    probe_integrity_verified: bool
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class PostBootUiDecision:
    status: VerificationStatus
    reasons: tuple[str, ...]
    restart_intent_hash: str
    writer_decision_hash: str
    ui_evidence_hash: str
    decision_hash: str
    read_only: bool = True
    post_boot_ui_verified: bool = False
    post_boot_chain_may_continue: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_post_boot_ui_evidence(**fields: Any) -> PostBootUiEvidence:
    _validate_evidence_fields(fields)
    return PostBootUiEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_post_boot_ui_availability(
    *,
    restart_intent_hash: str,
    writer_decision_hash: str,
    writer_exclusivity_verified: bool,
    evidence: Any,
    maximum_age_seconds: int = 120,
    maximum_duration_milliseconds: int = 5_000,
) -> PostBootUiDecision:
    _require_hash(restart_intent_hash)
    _require_hash(writer_decision_hash)
    if not isinstance(writer_exclusivity_verified, bool):
        raise PostBootUiAvailabilityError("POST_BOOT_UI_FIELD_INVALID")
    for bound in (maximum_age_seconds, maximum_duration_milliseconds):
        if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
            raise PostBootUiAvailabilityError("POST_BOOT_UI_BOUND_INVALID")
    item = _validated_evidence(evidence)

    if not writer_exclusivity_verified:
        status: VerificationStatus = "INCOMPLETE"
        reasons = ["POST_BOOT_WRITER_PREREQUISITE_NOT_VERIFIED"]
    elif not item.complete or not item.probe_integrity_verified:
        status = "INCOMPLETE"
        reasons = ["POST_BOOT_UI_EVIDENCE_UNTRUSTED"]
    elif item.evidence_age_seconds > maximum_age_seconds:
        status = "INCOMPLETE"
        reasons = ["POST_BOOT_UI_EVIDENCE_STALE"]
    elif not item.response_identity_verified:
        status = "TAMPERED"
        reasons = ["POST_BOOT_UI_RESPONSE_IDENTITY_MISMATCH"]
    else:
        reasons = []
        if item.duration_milliseconds > maximum_duration_milliseconds:
            reasons.append("POST_BOOT_UI_PROBE_TIMEOUT")
        if item.http_status_code != 200:
            reasons.append("POST_BOOT_UI_HTTP_UNAVAILABLE")
        status = "FAIL" if reasons else "PASS"

    passed = status == "PASS"
    unsigned = {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "restart_intent_hash": restart_intent_hash,
        "writer_decision_hash": writer_decision_hash,
        "ui_evidence_hash": item.evidence_hash,
        "read_only": True,
        "post_boot_ui_verified": passed,
        "post_boot_chain_may_continue": passed,
        "recovery_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return PostBootUiDecision(
        status=status,
        reasons=tuple(reasons),
        restart_intent_hash=restart_intent_hash,
        writer_decision_hash=writer_decision_hash,
        ui_evidence_hash=item.evidence_hash,
        decision_hash=_hash(unsigned),
        post_boot_ui_verified=passed,
        post_boot_chain_may_continue=passed,
    )


def validate_post_boot_ui_decision(value: Any) -> None:
    if not isinstance(value, PostBootUiDecision):
        raise PostBootUiAvailabilityError("POST_BOOT_UI_DECISION_TYPE_INVALID")
    passed = value.status == "PASS"
    if (
        value.read_only is not True
        or value.post_boot_ui_verified != passed
        or value.post_boot_chain_may_continue != passed
        or any(
            (
                value.recovery_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise PostBootUiAvailabilityError("POST_BOOT_UI_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = VERIFICATION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise PostBootUiAvailabilityError("POST_BOOT_UI_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> PostBootUiEvidence:
    if not isinstance(value, PostBootUiEvidence):
        raise PostBootUiAvailabilityError("POST_BOOT_UI_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_evidence_fields(unsigned)
    if supplied != _hash(unsigned):
        raise PostBootUiAvailabilityError("POST_BOOT_UI_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_evidence_fields(fields: dict[str, Any]) -> None:
    required = {
        "probe_identity_hash",
        "endpoint_identity_hash",
        "observed_at_epoch_seconds",
        "evidence_age_seconds",
        "duration_milliseconds",
        "http_status_code",
        "response_identity_verified",
        "probe_integrity_verified",
        "complete",
    }
    if set(fields) != required:
        raise PostBootUiAvailabilityError("POST_BOOT_UI_EVIDENCE_FIELD_INVALID")
    for key in ("probe_identity_hash", "endpoint_identity_hash"):
        _require_hash(fields[key])
    for key in ("observed_at_epoch_seconds", "evidence_age_seconds", "duration_milliseconds"):
        value = fields[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PostBootUiAvailabilityError("POST_BOOT_UI_EVIDENCE_FIELD_INVALID")
    code = fields["http_status_code"]
    if isinstance(code, bool) or not isinstance(code, int) or not 100 <= code <= 599:
        raise PostBootUiAvailabilityError("POST_BOOT_UI_EVIDENCE_FIELD_INVALID")
    for key in ("response_identity_verified", "probe_integrity_verified", "complete"):
        if not isinstance(fields[key], bool):
            raise PostBootUiAvailabilityError("POST_BOOT_UI_EVIDENCE_FIELD_INVALID")


def _require_hash(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PostBootUiAvailabilityError("POST_BOOT_UI_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
