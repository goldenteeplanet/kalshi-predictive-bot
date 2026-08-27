from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.phase4cd.settled_count_contention_audit import (
    SettledCountContentionAudit,
    validate_contention_audit,
)

BUDGET_SCHEMA_VERSION = "phase4gb-sqlite-read-transaction-budget-v1"
BudgetDecision = Literal["GRANT", "DENY"]


class SQLiteReadTransactionBudgetError(ValueError):
    """Stable fail-closed read-budget error."""


@dataclass(frozen=True)
class SQLiteReadTransactionBudget:
    decision: BudgetDecision
    reasons: tuple[str, ...]
    audit_hash: str
    query_fingerprint: str
    source_identity_hash: str
    source_watermark: str
    requested_rows: int
    requested_duration_ms: int
    max_rows: int
    max_duration_ms: int
    max_evidence_age_seconds: int
    query_only: bool
    immutable_source: bool
    busy_timeout_ms: int
    budget_hash: str
    execution_authorized: bool = False


def build_sqlite_read_transaction_budget(
    *,
    audit: Any,
    requested_rows: int,
    requested_duration_ms: int,
    max_rows: int = 1,
    max_duration_ms: int = 100,
    max_evidence_age_seconds: int = 300,
) -> SQLiteReadTransactionBudget:
    for value in (
        requested_rows,
        requested_duration_ms,
        max_rows,
        max_duration_ms,
        max_evidence_age_seconds,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SQLiteReadTransactionBudgetError("BUDGET_FIELD_INVALID")
    if requested_rows == 0 or max_rows == 0:
        raise SQLiteReadTransactionBudgetError("ROW_BUDGET_EMPTY")
    try:
        validate_contention_audit(audit)
    except (TypeError, ValueError) as exc:
        raise SQLiteReadTransactionBudgetError("AUDIT_INPUT_INVALID") from exc
    if not isinstance(audit, SettledCountContentionAudit):
        raise SQLiteReadTransactionBudgetError("AUDIT_INPUT_INVALID")

    reasons: list[str] = []
    if audit.status == "STALE" or audit.observed_max_age_seconds > max_evidence_age_seconds:
        reasons.append("AUDIT_EVIDENCE_STALE")
    elif audit.status != "CLEAR":
        reasons.append("AUDIT_NOT_CLEAR")
    if requested_rows > max_rows:
        reasons.append("ROW_BUDGET_EXCEEDED")
    if requested_duration_ms > max_duration_ms:
        reasons.append("DURATION_BUDGET_EXCEEDED")
    decision: BudgetDecision = "GRANT" if not reasons else "DENY"
    unsigned = {
        "schema_version": BUDGET_SCHEMA_VERSION,
        "decision": decision,
        "reasons": sorted(reasons),
        "audit_hash": audit.audit_hash,
        "query_fingerprint": audit.query_fingerprint,
        "source_identity_hash": audit.source_identity_hash,
        "source_watermark": audit.source_watermark,
        "requested_rows": requested_rows,
        "requested_duration_ms": requested_duration_ms,
        "max_rows": max_rows,
        "max_duration_ms": max_duration_ms,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "query_only": True,
        "immutable_source": True,
        "busy_timeout_ms": 0,
        "execution_authorized": False,
    }
    return SQLiteReadTransactionBudget(
        decision=decision,
        reasons=tuple(unsigned["reasons"]),
        audit_hash=audit.audit_hash,
        query_fingerprint=audit.query_fingerprint,
        source_identity_hash=audit.source_identity_hash,
        source_watermark=audit.source_watermark,
        requested_rows=requested_rows,
        requested_duration_ms=requested_duration_ms,
        max_rows=max_rows,
        max_duration_ms=max_duration_ms,
        max_evidence_age_seconds=max_evidence_age_seconds,
        query_only=True,
        immutable_source=True,
        busy_timeout_ms=0,
        budget_hash=_hash(unsigned),
    )


def validate_sqlite_read_transaction_budget(budget: Any) -> None:
    if not isinstance(budget, SQLiteReadTransactionBudget):
        raise SQLiteReadTransactionBudgetError("BUDGET_RESULT_TYPE_INVALID")
    if budget.execution_authorized is not False:
        raise SQLiteReadTransactionBudgetError("BUDGET_SAFETY_BOUNDARY_INVALID")
    if not budget.query_only or not budget.immutable_source or budget.busy_timeout_ms != 0:
        raise SQLiteReadTransactionBudgetError("READ_ONLY_CONTRACT_INVALID")
    if budget.decision == "GRANT" and budget.reasons:
        raise SQLiteReadTransactionBudgetError("GRANT_REASONS_INVALID")
    if budget.decision == "DENY" and not budget.reasons:
        raise SQLiteReadTransactionBudgetError("DENY_REASONS_MISSING")
    unsigned = asdict(budget)
    unsigned.pop("budget_hash")
    unsigned["schema_version"] = BUDGET_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    if budget.budget_hash != _hash(unsigned):
        raise SQLiteReadTransactionBudgetError("BUDGET_HASH_MISMATCH")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
