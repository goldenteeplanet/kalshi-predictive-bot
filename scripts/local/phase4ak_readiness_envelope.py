"""Phase 4AK read-only, non-executable settlement readiness envelope."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import (
    binary_outcome,
    canonical_hash,
    settlement_lineage_hash,
)

SCHEMA = "phase4ak.settlement-mutation-readiness-envelope.v1"
MANIFEST_SCHEMA = "phase4ak.executor-design-handoff.v1"
AJ_SCHEMA = "phase4aj.independent-canonicalization-review.v1"
AJ_MANIFEST_SCHEMA = "phase4aj.advancement-manifest.v1"
AI_SCHEMA = "phase4ai.settlement-timestamp-canonicalization-proposal.v1"
AI_REVIEW_SCHEMA = "phase4ai.settlement-timestamp-review-manifest.v1"
APPROVAL_SCHEMA = "phase4aj.human-approval.v1"
AH_SCHEMA = "phase4ah.settlement-timestamp-readiness-gate.v1"
AF_SCHEMA = "phase4af.settlement-timestamp-audit.v1"
AG_SCHEMA = "phase4ag.settlement-timestamp-collection.v1"
EVIDENCE_SCHEMA = "phase4af.settlement-timestamp-evidence.v1"
AD_SCHEMA = "phase4ad.reconciliation-attribution.v1"
AE_SCHEMA = "phase4ae.reconciliation-plan.v1"

SAFEGUARDS = sorted(
    [
        "AUDIT_ARTIFACT_PUBLICATION",
        "AUTOMATIC_ROLLBACK_OR_HALT_ON_MISMATCH",
        "CONTINUED_PROHIBITION_ON_LIVE_EXCHANGE_EXECUTION_AND_ORDER_CREATION",
        "EXACT_AFFECTED_ROW_COUNT_VERIFICATION",
        "FRESH_READ_ONLY_REVALIDATION_IMMEDIATELY_BEFORE_FUTURE_MUTATION",
        "INDEPENDENTLY_IMPLEMENTED_EXECUTOR",
        "POST_MUTATION_RECONCILIATION_AND_LINEAGE_VERIFICATION",
        "PRE_MUTATION_RECOVERABLE_DATABASE_SNAPSHOT",
        "SEPARATE_OPERATOR_AUTHORIZATION_SCOPED_TO_ONE_EXECUTION_ATTEMPT",
        "SINGLE_WRITER_LOCK_ACQUISITION",
        "TRANSACTIONAL_COMPARE_AND_SWAP",
    ]
)


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _read(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"PHASE4AK_{label}_UNREADABLE") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"PHASE4AK_{label}_INVALID")
    return payload


def _rows(path: Path, schema: str, label: str) -> dict[str, Any]:
    payload = _read(path, label)
    if payload.get("schema") != schema:
        raise ValueError(f"PHASE4AK_{label}_SCHEMA_INVALID")
    if payload.get("artifact_hash") != _hash(payload):
        raise ValueError(f"PHASE4AK_{label}_HASH_MISMATCH")
    rows = payload.get("rows")
    if not isinstance(rows, list) or payload.get("rows_hash") != canonical_hash(rows):
        raise ValueError(f"PHASE4AK_{label}_ROWS_HASH_MISMATCH")
    return payload


def _manifest(path: Path, schema: str, label: str) -> dict[str, Any]:
    payload = _read(path, label)
    if payload.get("schema") != schema:
        raise ValueError(f"PHASE4AK_{label}_SCHEMA_INVALID")
    if payload.get("manifest_hash") != _hash(payload, "manifest_hash"):
        raise ValueError(f"PHASE4AK_{label}_HASH_MISMATCH")
    return payload


def _approval(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    payload = _read(path, "APPROVAL")
    if payload.get("schema") != APPROVAL_SCHEMA:
        raise ValueError("PHASE4AK_APPROVAL_SCHEMA_INVALID")
    if payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4AK_APPROVAL_HASH_MISMATCH")
    return payload


def _time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"PHASE4AK_{label}_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"PHASE4AK_{label}_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"PHASE4AK_{label}_TIMEZONE_MISSING")
    return parsed.astimezone(UTC)


def _history_hash(history_dir: Path) -> str:
    script = Path(__file__).with_name("phase4ac_artifact_history.py")
    spec = importlib.util.spec_from_file_location("phase4ac_for_4ak", script)
    if not spec or not spec.loader:
        raise ValueError("PHASE4AK_HISTORY_LOADER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return str(module.reconstruct_phase4ab_history(history_dir)["manifest_hash"])
    except Exception as exc:
        raise ValueError("PHASE4AK_HISTORY_INVALID") from exc


def _ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError("PHASE4AK_DATABASE_NOT_FILE")
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise ValueError("PHASE4AK_QUERY_ONLY_NOT_ENFORCED")
    return connection


def _validate_lineage(
    aj: dict[str, Any],
    aj_manifest: dict[str, Any],
    proposal: dict[str, Any],
    ai_review: dict[str, Any],
    approval: dict[str, Any] | None,
    gate: dict[str, Any],
    reaudit: dict[str, Any],
    baseline: dict[str, Any],
    status: dict[str, Any],
    evidence: dict[str, Any],
    ad: dict[str, Any],
    ae: dict[str, Any],
    history_hash: str,
) -> None:
    checks = {
        "AJ_PAIR": aj.get("publication_pair_id") == aj_manifest.get("publication_pair_id"),
        "AJ_ATTESTATION": aj_manifest.get("review_attestation_hash") == aj.get("artifact_hash"),
        "AJ_AI_PROPOSAL": aj.get("source_phase4ai_proposal_hash")
        == aj_manifest.get("phase4ai_proposal_hash")
        == proposal.get("artifact_hash"),
        "AJ_AI_REVIEW": aj.get("source_phase4ai_review_manifest_hash")
        == aj_manifest.get("phase4ai_review_manifest_hash")
        == ai_review.get("manifest_hash"),
        "AJ_APPROVAL": aj.get("human_approval_artifact_hash")
        == aj_manifest.get("human_approval_artifact_hash")
        == (None if approval is None else approval.get("artifact_hash")),
        "AJ_AH": aj.get("source_phase4ah_gate_hash") == gate.get("artifact_hash"),
        "AJ_AF": aj.get("source_phase4af_reaudit_hash") == reaudit.get("artifact_hash"),
        "AJ_AG_STATUS": aj.get("source_phase4ag_status_hash") == status.get("artifact_hash"),
        "AJ_AG_EVIDENCE": aj.get("source_phase4ag_evidence_hash") == evidence.get("artifact_hash"),
        "AJ_AD": aj.get("source_phase4ad_hash") == ad.get("artifact_hash"),
        "AJ_AE": aj.get("source_phase4ae_hash") == ae.get("artifact_hash"),
        "AJ_AC": aj.get("source_phase4ac_manifest_hash") == history_hash,
        "AI_REVIEW_PROPOSAL": ai_review.get("proposal_artifact_hash")
        == proposal.get("artifact_hash"),
        "AI_REVIEW_ROWS": ai_review.get("proposal_rows_hash") == proposal.get("rows_hash"),
        "AI_AH": proposal.get("source_phase4ah_gate_hash") == gate.get("artifact_hash"),
        "AI_AF": proposal.get("source_phase4af_reaudit_hash") == reaudit.get("artifact_hash"),
        "AI_BASELINE": proposal.get("source_baseline_phase4af_hash")
        == baseline.get("artifact_hash"),
        "AI_AG_STATUS": proposal.get("source_phase4ag_status_hash") == status.get("artifact_hash"),
        "AI_AG_EVIDENCE": proposal.get("source_phase4ag_evidence_hash")
        == evidence.get("artifact_hash"),
        "AI_AD": proposal.get("source_phase4ad_hash") == ad.get("artifact_hash"),
        "AI_AE": proposal.get("source_phase4ae_hash") == ae.get("artifact_hash"),
        "AI_AC": proposal.get("source_phase4ac_manifest_hash") == history_hash,
    }
    failed = [name for name, valid in checks.items() if not valid]
    if failed:
        raise ValueError(f"PHASE4AK_LINEAGE_FAILURE_{failed[0]}")


def build(
    source_db: Path,
    aj_path: Path,
    aj_manifest_path: Path,
    proposal_path: Path,
    ai_review_path: Path,
    approval_path: Path | None,
    gate_path: Path,
    reaudit_path: Path,
    baseline_path: Path,
    status_path: Path,
    evidence_path: Path,
    ad_path: Path,
    ae_path: Path,
    history_dir: Path,
    *,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AK_EVALUATION_TIMEZONE_MISSING")
    now = now.astimezone(UTC)
    aj = _rows(aj_path, AJ_SCHEMA, "PHASE4AJ_ATTESTATION")
    aj_manifest = _manifest(aj_manifest_path, AJ_MANIFEST_SCHEMA, "PHASE4AJ_MANIFEST")
    proposal = _rows(proposal_path, AI_SCHEMA, "PHASE4AI_PROPOSAL")
    ai_review = _manifest(ai_review_path, AI_REVIEW_SCHEMA, "PHASE4AI_REVIEW")
    approval = _approval(approval_path)
    gate = _rows(gate_path, AH_SCHEMA, "PHASE4AH_GATE")
    reaudit = _rows(reaudit_path, AF_SCHEMA, "PHASE4AF_REAUDIT")
    baseline = _rows(baseline_path, AF_SCHEMA, "BASELINE_PHASE4AF")
    status = _rows(status_path, AG_SCHEMA, "PHASE4AG_STATUS")
    evidence = _rows(evidence_path, EVIDENCE_SCHEMA, "PHASE4AG_EVIDENCE")
    ad = _rows(ad_path, AD_SCHEMA, "PHASE4AD")
    ae = _rows(ae_path, AE_SCHEMA, "PHASE4AE")
    history_hash = _history_hash(history_dir)
    _validate_lineage(
        aj,
        aj_manifest,
        proposal,
        ai_review,
        approval,
        gate,
        reaudit,
        baseline,
        status,
        evidence,
        ad,
        ae,
        history_hash,
    )
    if any(
        artifact.get("execution_authorized") is not False
        for artifact in (aj, aj_manifest, proposal, ai_review)
    ):
        raise ValueError("PHASE4AK_INPUT_EXECUTION_AUTHORIZATION_PRESENT")
    proposal_expires = _time(proposal.get("proposal_expires_at"), "PROPOSAL_EXPIRATION")
    review_expires = _time(aj_manifest.get("expires_at"), "REVIEW_EXPIRATION")
    approval_expires = (
        None
        if approval is None
        else _time(approval.get("approval_expires_at"), "APPROVAL_EXPIRATION")
    )
    approval_time = (
        None if approval is None else _time(approval.get("approval_timestamp"), "APPROVAL_TIME")
    )
    approval_valid = bool(
        approval
        and approval.get("proposal_artifact_hash") == proposal["artifact_hash"]
        and approval.get("phase4ai_review_manifest_hash") == ai_review["manifest_hash"]
        and approval.get("future_separate_execution_only") is True
        and approval.get("phase4aj_execution_authorized") is False
        and str(approval.get("reviewer_identifier", "")).strip()
        and approval_expires
        and approval_time
        and approval_time <= now < approval_expires
        and approval_expires > approval_time
    )
    proposed_index = {str(row.get("ticker")): row for row in proposal["rows"]}
    review_index = {str(row.get("ticker")): row for row in aj["rows"]}
    evidence_index = {str(row.get("ticker")): row for row in evidence["rows"]}
    reaudit_index = {str(row.get("ticker")): row for row in reaudit["rows"]}
    if any(
        len(index) != len(rows)
        for index, rows in (
            (proposed_index, proposal["rows"]),
            (review_index, aj["rows"]),
            (evidence_index, evidence["rows"]),
            (reaudit_index, reaudit["rows"]),
        )
    ):
        raise ValueError("PHASE4AK_DUPLICATE_CANDIDATE")
    manifest_hashes = aj_manifest.get("eligible_proposal_row_hashes")
    approved_hashes = None if approval is None else approval.get("approved_proposal_row_hashes")
    if not isinstance(manifest_hashes, list) or len(manifest_hashes) != len(set(manifest_hashes)):
        raise ValueError("PHASE4AK_ADVANCEMENT_ROWS_INVALID")
    if approval is not None and (
        not isinstance(approved_hashes, list)
        or len(approved_hashes) != len(set(approved_hashes))
        or sorted(approved_hashes) != sorted(manifest_hashes)
    ):
        approval_valid = False
    aj_eligible = sorted(
        row.get("proposal_row_hash") for row in aj["rows"] if row.get("review_result") == "ELIGIBLE"
    )
    if (
        aj.get("review_state") == "APPROVED_FOR_SEPARATELY_AUTHORIZED_EXECUTION"
        and sorted(manifest_hashes) != aj_eligible
    ):
        raise ValueError("PHASE4AK_LINEAGE_FAILURE_ADVANCEMENT_ROWS")
    connection = _ro(source_db)
    rows: list[dict[str, Any]] = []
    try:
        for row_hash in sorted(manifest_hashes):
            proposed = next(
                (row for row in proposal["rows"] if row.get("proposal_row_hash") == row_hash),
                None,
            )
            review = next(
                (row for row in aj["rows"] if row.get("proposal_row_hash") == row_hash), None
            )
            result, reasons = "READY", []
            if proposed is None or review is None:
                result, reasons = "LINEAGE_FAILURE", ["APPROVED_ROW_MISSING"]
                ticker = ""
            else:
                ticker = str(proposed["ticker"])
                preconditions = proposed.get("compare_and_swap_preconditions")
                required = {
                    "settled_at",
                    "result",
                    "settlement_lineage_hash",
                    "updated_at",
                    "no_linked_evaluation",
                    "proposal_not_expired_before",
                    "proposal_not_previously_applied",
                    "proposal_not_superseded",
                }
                current = connection.execute(
                    "SELECT * FROM settlements WHERE ticker=?", (ticker,)
                ).fetchone()
                audited = reaudit_index.get(ticker)
                source = evidence_index.get(ticker)
                if not isinstance(preconditions, dict) or set(preconditions) != required:
                    result, reasons = "INCOMPLETE", ["COMPARE_AND_SWAP_INCOMPLETE"]
                elif review.get("review_result") != "ELIGIBLE" or not review.get(
                    "compare_and_swap_verified"
                ):
                    result, reasons = "LINEAGE_FAILURE", ["PHASE4AJ_ROW_NOT_ELIGIBLE"]
                elif current is None:
                    result, reasons = "DRIFTED", ["SETTLEMENT_ROW_MISSING"]
                elif current["settled_at"] not in (None, ""):
                    result, reasons = "DRIFTED", ["CANONICAL_TIMESTAMP_APPEARED"]
                elif (
                    binary_outcome(current["result"]) is None
                    or current["result"] != preconditions["result"]
                ):
                    result, reasons = "DRIFTED", ["SETTLEMENT_RESULT_CHANGED"]
                elif (
                    settlement_lineage_hash(dict(current))
                    != preconditions["settlement_lineage_hash"]
                ):
                    result, reasons = "DRIFTED", ["SETTLEMENT_LINEAGE_CHANGED"]
                elif preconditions.get("no_linked_evaluation") is not True:
                    result, reasons = "DRIFTED", ["LINKED_EVALUATION_APPEARED"]
                elif (
                    preconditions.get("proposal_not_previously_applied") is not True
                    or preconditions.get("proposal_not_superseded") is not True
                ):
                    result, reasons = "DRIFTED", ["PROPOSAL_APPLIED_OR_SUPERSEDED"]
                elif audited is None or source is None:
                    result, reasons = "CONFLICT", ["AUTHORITATIVE_EVIDENCE_MISSING"]
                elif audited.get("classification") == "TIMESTAMP_CONFLICT":
                    result, reasons = "CONFLICT", ["TIMESTAMP_CONFLICT"]
                elif not audited.get("source_fresh") or not audited.get("timezone_unambiguous"):
                    result, reasons = "CONFLICT", ["EVIDENCE_STALE_OR_AMBIGUOUS"]
                elif source.get("settlement_timestamp") != proposed.get("proposed_settled_at"):
                    result, reasons = "CONFLICT", ["AUTHORITATIVE_TIMESTAMP_CHANGED"]
                elif audited.get("candidate_evidence_hash") != proposed.get("evidence_hash"):
                    result, reasons = "CONFLICT", ["AUTHORITATIVE_EVIDENCE_HASH_CHANGED"]
            preconditions = (
                {} if proposed is None else proposed.get("compare_and_swap_preconditions", {})
            )
            audited = reaudit_index.get(ticker)
            source = evidence_index.get(ticker)
            envelope_row: dict[str, Any] = {
                "ticker": ticker,
                "settlement_identity": {"ticker": ticker},
                "phase4ai_proposal_row_hash": row_hash,
                "phase4aj_review_row_hash": None
                if review is None
                else review.get("review_row_hash"),
                "human_approval_artifact_hash": None
                if approval is None
                else approval["artifact_hash"],
                "expected_result": preconditions.get("result"),
                "expected_settled_at": preconditions.get("settled_at"),
                "expected_settlement_lineage_hash": preconditions.get("settlement_lineage_hash"),
                "expected_updated_at": preconditions.get("updated_at"),
                "proposed_settled_at": None
                if proposed is None
                else proposed.get("proposed_settled_at"),
                "authoritative_evidence_identity": None
                if proposed is None
                else proposed.get("evidence_source_identity"),
                "authoritative_evidence_hash": None
                if proposed is None
                else proposed.get("evidence_hash"),
                "evidence_captured_at": None if source is None else source.get("captured_at"),
                "evidence_freshness_deadline": None
                if audited is None
                else audited.get("freshness_deadline"),
                "proposal_expires_at": proposal_expires.isoformat(),
                "approval_expires_at": None
                if approval_expires is None
                else approval_expires.isoformat(),
                "compare_and_swap_preconditions": preconditions,
                "semantic_operation": "SET_CANONICAL_SETTLED_AT_IF_UNCHANGED",
                "readiness_result": result,
                "reason_codes": reasons or ["ALL_READINESS_PRECONDITIONS_SATISFIED"],
                "executable": False,
                "sql_present": False,
                "execution_authorized": False,
            }
            envelope_row["readiness_row_hash"] = canonical_hash(envelope_row)
            rows.append(envelope_row)
    finally:
        connection.close()
    counts = Counter(row["readiness_result"] for row in rows)
    review_approved = aj.get("review_state") == "APPROVED_FOR_SEPARATELY_AUTHORIZED_EXECUTION"
    if counts["LINEAGE_FAILURE"]:
        state, reasons = "LINEAGE_FAILURE", ["ROW_LINEAGE_FAILURE"]
    elif not review_approved:
        state, reasons = "REVIEW_NOT_APPROVED", ["PHASE4AJ_REVIEW_NOT_APPROVED"]
    elif not approval_valid:
        state, reasons = "APPROVAL_EXPIRED_OR_INVALID", ["APPROVAL_MISSING_EXPIRED_OR_INVALID"]
    elif now >= proposal_expires or now >= review_expires:
        state, reasons = "PROPOSAL_EXPIRED", ["PROPOSAL_OR_REVIEW_EXPIRED"]
    elif counts["DRIFTED"]:
        state, reasons = "CURRENT_STATE_DRIFTED", ["CURRENT_SETTLEMENT_STATE_CHANGED"]
    elif counts["CONFLICT"]:
        state, reasons = "EVIDENCE_CONFLICT", ["AUTHORITATIVE_EVIDENCE_CONFLICT"]
    elif counts["INCOMPLETE"]:
        state, reasons = "PRECONDITION_INCOMPLETE", ["COMPARE_AND_SWAP_PRECONDITION_INCOMPLETE"]
    elif counts["READY"]:
        state, reasons = "READY_FOR_SEPARATE_EXECUTOR_DESIGN", ["READINESS_ENVELOPE_COMPLETE"]
    else:
        state, reasons = "NO_ELIGIBLE_ROWS", ["NO_APPROVED_ROWS"]
    stat = source_db.stat()
    input_hashes = dict(
        sorted(
            {
                "phase4ac_manifest": history_hash,
                "phase4ad": ad["artifact_hash"],
                "phase4ae": ae["artifact_hash"],
                "phase4af_baseline": baseline["artifact_hash"],
                "phase4af_reaudit": reaudit["artifact_hash"],
                "phase4ag_evidence": evidence["artifact_hash"],
                "phase4ag_status": status["artifact_hash"],
                "phase4ah": gate["artifact_hash"],
                "phase4ai_proposal": proposal["artifact_hash"],
                "phase4ai_review": ai_review["manifest_hash"],
                "phase4aj_attestation": aj["artifact_hash"],
                "phase4aj_manifest": aj_manifest["manifest_hash"],
                "human_approval": None if approval is None else approval["artifact_hash"],
            }.items()
        )
    )
    envelope_id = canonical_hash(
        {"evaluated_at": now.isoformat(), "inputs": input_hashes, "rows": rows}
    )
    envelope: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AK",
        "envelope_id": envelope_id,
        "evaluated_at": now.isoformat(),
        "readiness_state": state,
        "reason_codes": reasons,
        "input_hashes": input_hashes,
        "phase4ac_manifest_hash": history_hash,
        "production_database_identity": {
            "path": str(source_db),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        },
        "proposal_expires_at": proposal_expires.isoformat(),
        "review_expires_at": review_expires.isoformat(),
        "approval_expires_at": None if approval_expires is None else approval_expires.isoformat(),
        "proposal_expired": now >= proposal_expires,
        "review_expired": now >= review_expires,
        "approval_expired_or_invalid": not approval_valid,
        "eligible_count": counts["READY"],
        "excluded_count": len(rows) - counts["READY"],
        "drifted_count": counts["DRIFTED"],
        "expired_count": len(rows) if now >= min(proposal_expires, review_expires) else 0,
        "conflicting_count": counts["CONFLICT"],
        "incomplete_count": counts["INCOMPLETE"],
        "disposition_counts": dict(sorted(counts.items())),
        "contains_sql": False,
        "contains_executable_mutation": False,
        "database_mutation_performed": False,
        "execution_authorized": False,
        "rows_hash": canonical_hash(rows),
        "rows": rows,
    }
    pair_id = canonical_hash({"envelope_id": envelope_id, "state": state})
    envelope["publication_pair_id"] = pair_id
    envelope["artifact_hash"] = _hash(envelope)
    deadlines = [proposal_expires, review_expires] + (
        [] if approval_expires is None else [approval_expires]
    )
    handoff: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "publication_pair_id": pair_id,
        "readiness_envelope_hash": envelope["artifact_hash"],
        "phase4aj_attestation_hash": aj["artifact_hash"],
        "phase4aj_advancement_manifest_hash": aj_manifest["manifest_hash"],
        "phase4ai_proposal_hash": proposal["artifact_hash"],
        "external_approval_hash": None if approval is None else approval["artifact_hash"],
        "readiness_row_hashes": sorted(
            row["readiness_row_hash"] for row in rows if row["readiness_result"] == "READY"
        )
        if state == "READY_FOR_SEPARATE_EXECUTOR_DESIGN"
        else [],
        "earliest_expiration_deadline": min(deadlines).isoformat(),
        "handoff_state": state,
        "required_future_safeguards": SAFEGUARDS,
        "no_executor_exists_or_is_authorized_by_phase4ak": True,
        "executor_implemented": False,
        "execution_authorized": False,
    }
    handoff["manifest_hash"] = _hash(handoff, "manifest_hash")
    return envelope, handoff


def publish_pair(
    envelope_path: Path,
    handoff_path: Path,
    envelope: dict[str, Any],
    handoff: dict[str, Any],
    *,
    replace: bool = False,
) -> None:
    if envelope_path.parent != handoff_path.parent:
        raise ValueError("PHASE4AK_OUTPUT_DIRECTORIES_DIFFER")
    envelope_path.parent.mkdir(parents=True, exist_ok=True)
    if not replace and (envelope_path.exists() or handoff_path.exists()):
        raise FileExistsError("PHASE4AK_OUTPUT_EXISTS")
    temporary = [
        envelope_path.with_name(f".{envelope_path.name}.{os.getpid()}.tmp"),
        handoff_path.with_name(f".{handoff_path.name}.{os.getpid()}.tmp"),
    ]
    backups = [
        envelope_path.with_name(f".{envelope_path.name}.{os.getpid()}.bak"),
        handoff_path.with_name(f".{handoff_path.name}.{os.getpid()}.bak"),
    ]
    published: list[Path] = []
    try:
        for path, payload in zip(temporary, (envelope, handoff), strict=True):
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        if replace:
            for final, backup in zip((envelope_path, handoff_path), backups, strict=True):
                if final.exists():
                    os.replace(final, backup)
        for temp, final in zip(temporary, (envelope_path, handoff_path), strict=True):
            os.replace(temp, final)
            published.append(final)
        try:
            fd = os.open(envelope_path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
    except Exception:
        for final in published:
            if final.exists():
                final.unlink()
        for backup, final in zip(backups, (envelope_path, handoff_path), strict=True):
            if backup.exists():
                os.replace(backup, final)
        raise
    finally:
        for path in temporary + backups:
            if path.exists():
                path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase4aj-attestation", type=Path, required=True)
    parser.add_argument("--phase4aj-advancement-manifest", type=Path, required=True)
    parser.add_argument("--phase4ai-proposal", type=Path, required=True)
    parser.add_argument("--phase4ai-review-manifest", type=Path, required=True)
    parser.add_argument("--human-approval-artifact", type=Path)
    parser.add_argument("--phase4ah-gate-artifact", type=Path, required=True)
    parser.add_argument("--phase4af-reaudit-artifact", type=Path, required=True)
    parser.add_argument("--baseline-phase4af-artifact", type=Path, required=True)
    parser.add_argument("--phase4ag-status-artifact", type=Path, required=True)
    parser.add_argument("--phase4ag-evidence-artifact", type=Path, required=True)
    parser.add_argument("--phase4ad-artifact", type=Path, required=True)
    parser.add_argument("--phase4ae-artifact", type=Path, required=True)
    parser.add_argument("--history-dir", type=Path, required=True)
    parser.add_argument("--production-db", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--readiness-envelope-output", type=Path, required=True)
    parser.add_argument("--executor-design-handoff-output", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    envelope, handoff = build(
        args.production_db,
        args.phase4aj_attestation,
        args.phase4aj_advancement_manifest,
        args.phase4ai_proposal,
        args.phase4ai_review_manifest,
        args.human_approval_artifact,
        args.phase4ah_gate_artifact,
        args.phase4af_reaudit_artifact,
        args.baseline_phase4af_artifact,
        args.phase4ag_status_artifact,
        args.phase4ag_evidence_artifact,
        args.phase4ad_artifact,
        args.phase4ae_artifact,
        args.history_dir,
        now=_time(args.evaluation_time, "EVALUATION_TIME"),
    )
    publish_pair(
        args.readiness_envelope_output,
        args.executor_design_handoff_output,
        envelope,
        handoff,
        replace=args.replace,
    )
    print(
        json.dumps({key: value for key, value in envelope.items() if key != "rows"}, sort_keys=True)
    )


if __name__ == "__main__":
    main()
