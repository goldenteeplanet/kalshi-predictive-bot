from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .database_readability_classifier import (
    DatabaseReadabilityDecision,
    validate_database_readability_decision,
)

VERIFICATION_SCHEMA_VERSION = "phase4kf-post-boot-database-readability-v1"
VerificationStatus = Literal["PASS", "FAIL", "INCOMPLETE", "TAMPERED"]


class PostBootDatabaseReadabilityError(ValueError):
    """Stable fail-closed post-boot database-readability error."""


@dataclass(frozen=True)
class PostBootDatabaseReadabilityDecision:
    status: VerificationStatus
    reasons: tuple[str, ...]
    restart_intent_hash: str
    scheduler_decision_hash: str
    database_decision_hash: str
    decision_hash: str
    read_only: bool = True
    post_boot_database_verified: bool = False
    post_boot_chain_may_continue: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    database_write_authorized: bool = False
    execution_authorized: bool = False


def evaluate_post_boot_database_readability(
    *,
    restart_intent_hash: str,
    scheduler_decision_hash: str,
    scheduler_verified: bool,
    database_decision: Any,
) -> PostBootDatabaseReadabilityDecision:
    _require_hash(restart_intent_hash)
    _require_hash(scheduler_decision_hash)
    if not isinstance(scheduler_verified, bool):
        raise PostBootDatabaseReadabilityError("POST_BOOT_DATABASE_FIELD_INVALID")
    if not isinstance(database_decision, DatabaseReadabilityDecision):
        raise PostBootDatabaseReadabilityError("POST_BOOT_DATABASE_EVIDENCE_TYPE_INVALID")
    try:
        validate_database_readability_decision(database_decision)
    except ValueError as exc:
        raise PostBootDatabaseReadabilityError("POST_BOOT_DATABASE_EVIDENCE_INVALID") from exc

    if not scheduler_verified:
        status: VerificationStatus = "INCOMPLETE"
        reasons = ["POST_BOOT_SCHEDULER_PREREQUISITE_NOT_VERIFIED"]
    elif database_decision.status == "TAMPERED":
        status = "TAMPERED"
        reasons = ["POST_BOOT_DATABASE_EVIDENCE_TAMPERED"]
    elif database_decision.status in {"UNKNOWN", "INCOMPLETE"}:
        status = "INCOMPLETE"
        reasons = [f"POST_BOOT_{reason}" for reason in database_decision.reasons]
    elif database_decision.status == "UNREADABLE":
        status = "FAIL"
        reasons = [f"POST_BOOT_{reason}" for reason in database_decision.reasons]
    elif database_decision.status == "READABLE":
        status = "PASS"
        reasons = []
    else:
        status = "INCOMPLETE"
        reasons = ["POST_BOOT_DATABASE_STATUS_UNKNOWN"]

    passed = status == "PASS"
    unsigned = {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "restart_intent_hash": restart_intent_hash,
        "scheduler_decision_hash": scheduler_decision_hash,
        "database_decision_hash": database_decision.decision_hash,
        "read_only": True,
        "post_boot_database_verified": passed,
        "post_boot_chain_may_continue": passed,
        "recovery_authorized": False,
        "restart_authorized": False,
        "database_write_authorized": False,
        "execution_authorized": False,
    }
    return PostBootDatabaseReadabilityDecision(
        status=status,
        reasons=tuple(reasons),
        restart_intent_hash=restart_intent_hash,
        scheduler_decision_hash=scheduler_decision_hash,
        database_decision_hash=database_decision.decision_hash,
        decision_hash=_hash(unsigned),
        post_boot_database_verified=passed,
        post_boot_chain_may_continue=passed,
    )


def validate_post_boot_database_readability_decision(value: Any) -> None:
    if not isinstance(value, PostBootDatabaseReadabilityDecision):
        raise PostBootDatabaseReadabilityError("POST_BOOT_DATABASE_DECISION_TYPE_INVALID")
    passed = value.status == "PASS"
    if (
        value.read_only is not True
        or value.post_boot_database_verified != passed
        or value.post_boot_chain_may_continue != passed
        or any(
            (
                value.recovery_authorized,
                value.restart_authorized,
                value.database_write_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise PostBootDatabaseReadabilityError("POST_BOOT_DATABASE_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = VERIFICATION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise PostBootDatabaseReadabilityError("POST_BOOT_DATABASE_DECISION_HASH_MISMATCH")


def _require_hash(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PostBootDatabaseReadabilityError("POST_BOOT_DATABASE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
