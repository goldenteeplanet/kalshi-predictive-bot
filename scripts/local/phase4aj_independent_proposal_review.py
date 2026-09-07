"""Phase 4AJ independent, read-only canonicalization proposal review."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import (
    binary_outcome,
    canonical_hash,
    settlement_lineage_hash,
)

SCHEMA = "phase4aj.independent-canonicalization-review.v1"
MANIFEST_SCHEMA = "phase4aj.advancement-manifest.v1"
APPROVAL_SCHEMA = "phase4aj.human-approval.v1"
AI_SCHEMA = "phase4ai.settlement-timestamp-canonicalization-proposal.v1"
AI_REVIEW_SCHEMA = "phase4ai.settlement-timestamp-review-manifest.v1"
AH_SCHEMA = "phase4ah.settlement-timestamp-readiness-gate.v1"
AF_SCHEMA = "phase4af.settlement-timestamp-audit.v1"
AG_SCHEMA = "phase4ag.settlement-timestamp-collection.v1"
EVIDENCE_SCHEMA = "phase4af.settlement-timestamp-evidence.v1"
AD_SCHEMA = "phase4ad.reconciliation-attribution.v1"
AE_SCHEMA = "phase4ae.reconciliation-plan.v1"


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _load_rows(path: Path, schema: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"PHASE4AJ_{label}_UNREADABLE") from exc
    if payload.get("schema") != schema:
        raise ValueError(f"PHASE4AJ_{label}_SCHEMA_INVALID")
    if payload.get("artifact_hash") != _hash(payload):
        raise ValueError(f"PHASE4AJ_{label}_HASH_MISMATCH")
    if not isinstance(payload.get("rows"), list):
        raise ValueError(f"PHASE4AJ_{label}_ROWS_INVALID")
    if payload.get("rows_hash") != canonical_hash(payload["rows"]):
        raise ValueError(f"PHASE4AJ_{label}_ROWS_HASH_MISMATCH")
    return payload


def _load_ai_review(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4AJ_PHASE4AI_REVIEW_UNREADABLE") from exc
    if payload.get("schema") != AI_REVIEW_SCHEMA:
        raise ValueError("PHASE4AJ_PHASE4AI_REVIEW_SCHEMA_INVALID")
    if payload.get("manifest_hash") != _hash(payload, "manifest_hash"):
        raise ValueError("PHASE4AJ_PHASE4AI_REVIEW_HASH_MISMATCH")
    return payload


def _history_hash(history_dir: Path) -> str:
    path = Path(__file__).with_name("phase4ac_artifact_history.py")
    spec = importlib.util.spec_from_file_location("phase4ac_for_4aj", path)
    if not spec or not spec.loader:
        raise ValueError("PHASE4AJ_HISTORY_LOADER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return str(module.reconstruct_phase4ab_history(history_dir)["manifest_hash"])
    except Exception as exc:
        raise ValueError("PHASE4AJ_HISTORY_INVALID") from exc


def _ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError("PHASE4AJ_DATABASE_NOT_FILE")
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise ValueError("PHASE4AJ_QUERY_ONLY_NOT_ENFORCED")
    return connection


def _time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"PHASE4AJ_{label}_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"PHASE4AJ_{label}_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"PHASE4AJ_{label}_TIMEZONE_MISSING")
    return parsed.astimezone(UTC)


def _validate_lineage(
    proposal: dict[str, Any],
    ai_review: dict[str, Any],
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
        "AI_PAIR": ai_review.get("publication_pair_id") == proposal.get("proposal_id"),
        "AI_REVIEW_PROPOSAL": ai_review.get("proposal_artifact_hash")
        == proposal.get("artifact_hash"),
        "AI_REVIEW_ROWS": ai_review.get("proposal_rows_hash") == proposal.get("rows_hash"),
        "AH": proposal.get("source_phase4ah_gate_hash") == gate.get("artifact_hash"),
        "AF_REAUDIT": proposal.get("source_phase4af_reaudit_hash") == reaudit.get("artifact_hash"),
        "AF_BASELINE": proposal.get("source_baseline_phase4af_hash")
        == baseline.get("artifact_hash"),
        "AG_STATUS": proposal.get("source_phase4ag_status_hash") == status.get("artifact_hash"),
        "AG_EVIDENCE": proposal.get("source_phase4ag_evidence_hash")
        == evidence.get("artifact_hash"),
        "AG_PAIR": proposal.get("source_phase4ag_pair_id")
        == status.get("pair_id")
        == evidence.get("pair_id"),
        "AD": proposal.get("source_phase4ad_hash") == ad.get("artifact_hash"),
        "AE": proposal.get("source_phase4ae_hash") == ae.get("artifact_hash"),
        "AC": proposal.get("source_phase4ac_manifest_hash") == history_hash,
        "AH_REAUDIT": gate.get("reaudit_phase4af_artifact_hash") == reaudit.get("artifact_hash"),
        "AH_BASELINE": gate.get("baseline_phase4af_artifact_hash") == baseline.get("artifact_hash"),
        "AH_STATUS": gate.get("phase4ag_status_artifact_hash") == status.get("artifact_hash"),
        "AH_EVIDENCE": gate.get("phase4ag_evidence_artifact_hash") == evidence.get("artifact_hash"),
        "AH_AD": gate.get("source_phase4ad_artifact_hash") == ad.get("artifact_hash"),
        "AH_AE": gate.get("source_phase4ae_artifact_hash") == ae.get("artifact_hash"),
        "AH_AC": gate.get("source_phase4ac_manifest_hash") == history_hash,
    }
    failed = [name for name, valid in checks.items() if not valid]
    if failed:
        raise ValueError(f"PHASE4AJ_LINEAGE_FAILURE_{failed[0]}")


def _load_approval(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4AJ_APPROVAL_UNREADABLE") from exc
    if payload.get("schema") != APPROVAL_SCHEMA:
        raise ValueError("PHASE4AJ_APPROVAL_SCHEMA_INVALID")
    if payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4AJ_APPROVAL_HASH_MISMATCH")
    return payload


def _approval_status(
    approval: dict[str, Any] | None,
    proposal: dict[str, Any],
    ai_review: dict[str, Any],
    eligible_hashes: list[str],
    now: datetime,
    clock_skew_seconds: int,
) -> tuple[str, list[str]]:
    if approval is None:
        return "ABSENT", ["HUMAN_APPROVAL_NOT_SUPPLIED"]
    required = {
        "proposal_artifact_hash",
        "phase4ai_review_manifest_hash",
        "approved_proposal_row_hashes",
        "reviewer_identifier",
        "approval_timestamp",
        "approval_expires_at",
        "future_separate_execution_only",
        "phase4aj_execution_authorized",
    }
    if not required.issubset(approval):
        return "INVALID", ["HUMAN_APPROVAL_FIELDS_MISSING"]
    if approval["proposal_artifact_hash"] != proposal["artifact_hash"]:
        return "INVALID", ["HUMAN_APPROVAL_PROPOSAL_MISMATCH"]
    if approval["phase4ai_review_manifest_hash"] != ai_review["manifest_hash"]:
        return "INVALID", ["HUMAN_APPROVAL_REVIEW_MISMATCH"]
    if approval.get("phase4aj_execution_authorized") is not False:
        return "INVALID", ["HUMAN_APPROVAL_SELF_AUTHORIZES"]
    if approval.get("future_separate_execution_only") is not True:
        return "INVALID", ["HUMAN_APPROVAL_SCOPE_INVALID"]
    approved = approval.get("approved_proposal_row_hashes")
    if not isinstance(approved, list) or len(approved) != len(set(approved)):
        return "INVALID", ["HUMAN_APPROVAL_ROWS_INVALID"]
    if sorted(approved) != sorted(eligible_hashes):
        return "INVALID", ["HUMAN_APPROVAL_ROWS_NOT_EXACT"]
    approved_at = _time(approval["approval_timestamp"], "APPROVAL_TIME")
    expires_at = _time(approval["approval_expires_at"], "APPROVAL_EXPIRATION")
    if expires_at <= approved_at:
        return "INVALID", ["HUMAN_APPROVAL_INTERVAL_INVALID"]
    if approved_at > now + timedelta(seconds=clock_skew_seconds):
        return "INVALID", ["HUMAN_APPROVAL_FROM_FUTURE"]
    if now >= expires_at:
        return "INVALID", ["HUMAN_APPROVAL_EXPIRED"]
    if approval.get("decision", "APPROVED") == "REJECTED":
        return "REJECTED", ["HUMAN_REVIEW_REJECTED"]
    if not str(approval.get("reviewer_identifier", "")).strip():
        return "INVALID", ["HUMAN_APPROVAL_REVIEWER_MISSING"]
    return "VALID", ["HUMAN_APPROVAL_VALID_AND_EXACTLY_BOUND"]


def build(
    source_db: Path,
    proposal_path: Path,
    ai_review_path: Path,
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
    approval_path: Path | None = None,
    approval_clock_skew_seconds: int = 300,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AJ_EVALUATION_TIMEZONE_MISSING")
    if approval_clock_skew_seconds < 0:
        raise ValueError("PHASE4AJ_CLOCK_SKEW_INVALID")
    now = now.astimezone(UTC)
    proposal = _load_rows(proposal_path, AI_SCHEMA, "PHASE4AI_PROPOSAL")
    ai_review = _load_ai_review(ai_review_path)
    gate = _load_rows(gate_path, AH_SCHEMA, "PHASE4AH_GATE")
    reaudit = _load_rows(reaudit_path, AF_SCHEMA, "PHASE4AF_REAUDIT")
    baseline = _load_rows(baseline_path, AF_SCHEMA, "BASELINE_PHASE4AF")
    status = _load_rows(status_path, AG_SCHEMA, "PHASE4AG_STATUS")
    evidence = _load_rows(evidence_path, EVIDENCE_SCHEMA, "PHASE4AG_EVIDENCE")
    ad = _load_rows(ad_path, AD_SCHEMA, "PHASE4AD")
    ae = _load_rows(ae_path, AE_SCHEMA, "PHASE4AE")
    history_hash = _history_hash(history_dir)
    _validate_lineage(
        proposal, ai_review, gate, reaudit, baseline, status, evidence, ad, ae, history_hash
    )
    generated = _time(proposal.get("generated_at"), "PROPOSAL_GENERATED_AT")
    expires = _time(proposal.get("proposal_expires_at"), "PROPOSAL_EXPIRATION")
    if expires <= generated:
        raise ValueError("PHASE4AJ_PROPOSAL_VALIDITY_INVALID")
    expired = now >= expires
    if (
        proposal.get("contains_executable_sql") is not False
        or proposal.get("execution_authorized") is not False
    ):
        raise ValueError("PHASE4AJ_PROPOSAL_EXECUTION_CAPABILITY_PRESENT")
    proposal_rows = proposal["rows"]
    keys = [str(row.get("ticker")) for row in proposal_rows]
    if len(keys) != len(set(keys)):
        raise ValueError("PHASE4AJ_DUPLICATE_PROPOSAL_ROW")
    for row in proposal_rows:
        expected_hash = canonical_hash(
            {key: value for key, value in row.items() if key != "proposal_row_hash"}
        )
        if row.get("proposal_row_hash") != expected_hash:
            raise ValueError("PHASE4AJ_PROPOSAL_ROW_HASH_MISMATCH")
    gate_index = {str(row.get("ticker")): row for row in gate["rows"]}
    reaudit_index = {str(row.get("ticker")): row for row in reaudit["rows"]}
    status_index = {str(row.get("ticker")): row for row in status["rows"]}
    evidence_index = {str(row.get("ticker")): row for row in evidence["rows"]}
    if any(
        len(index) != len(rows)
        for index, rows in (
            (gate_index, gate["rows"]),
            (reaudit_index, reaudit["rows"]),
            (status_index, status["rows"]),
            (evidence_index, evidence["rows"]),
        )
    ):
        raise ValueError("PHASE4AJ_DUPLICATE_LINEAGE_ROW")
    connection = _ro(source_db)
    reviewed_rows: list[dict[str, Any]] = []
    try:
        for proposed in sorted(proposal_rows, key=lambda row: str(row["ticker"])):
            ticker = str(proposed["ticker"])
            reasons: list[str] = []
            result = "ELIGIBLE"
            gate_row = gate_index.get(ticker)
            audited = reaudit_index.get(ticker)
            ag_row = status_index.get(ticker)
            evidence_row = evidence_index.get(ticker)
            current = connection.execute(
                "SELECT * FROM settlements WHERE ticker=?", (ticker,)
            ).fetchone()
            preconditions = proposed.get("compare_and_swap_preconditions")
            required_preconditions = {
                "settled_at",
                "result",
                "settlement_lineage_hash",
                "updated_at",
                "no_linked_evaluation",
                "proposal_not_expired_before",
                "proposal_not_previously_applied",
                "proposal_not_superseded",
            }
            if proposed.get("disposition") != "PROPOSED":
                result, reasons = "INELIGIBLE", ["PHASE4AI_ROW_NOT_PROPOSED"]
            elif (
                not isinstance(preconditions, dict) or set(preconditions) != required_preconditions
            ):
                result, reasons = "LINEAGE_FAILURE", ["COMPARE_AND_SWAP_INCOMPLETE"]
            elif (
                gate.get("gate_state")
                not in {
                    "READY_FOR_CANONICALIZATION_PROPOSAL",
                    "PARTIALLY_READY",
                }
                or gate_row is None
                or gate_row.get("transition") != "AUTHORITATIVE_EVIDENCE_ADDED"
            ):
                result, reasons = "LINEAGE_FAILURE", ["PHASE4AH_TRANSITION_NOT_ELIGIBLE"]
            elif audited is None or not audited.get("ready_for_future_canonicalization"):
                result, reasons = "LINEAGE_FAILURE", ["PHASE4AF_REAUDIT_NOT_READY"]
            elif evidence_row is None or ag_row is None:
                result, reasons = "EVIDENCE_CONFLICT", ["AUTHORITATIVE_EVIDENCE_MISSING"]
            elif audited.get("classification") == "TIMESTAMP_CONFLICT":
                result, reasons = "EVIDENCE_CONFLICT", ["TIMESTAMP_CONFLICT"]
            elif not audited.get("timezone_unambiguous") or not audited.get("source_fresh"):
                result, reasons = "EVIDENCE_CONFLICT", ["EVIDENCE_AMBIGUOUS_OR_STALE"]
            elif evidence_row.get("settlement_timestamp") != proposed.get("proposed_settled_at"):
                result, reasons = "EVIDENCE_CONFLICT", ["PROPOSED_TIMESTAMP_MISMATCH"]
            elif audited.get("candidate_evidence_hash") != proposed.get("evidence_hash"):
                result, reasons = "EVIDENCE_CONFLICT", ["EVIDENCE_HASH_MISMATCH"]
            elif current is None:
                result, reasons = "DRIFTED", ["SETTLEMENT_ROW_MISSING"]
            elif current["settled_at"] not in (None, ""):
                result, reasons = "DRIFTED", ["CANONICAL_TIMESTAMP_APPEARED"]
            elif (
                binary_outcome(current["result"]) is None
                or current["result"] != preconditions["result"]
            ):
                result, reasons = "DRIFTED", ["SETTLEMENT_RESULT_CHANGED"]
            elif settlement_lineage_hash(dict(current)) != preconditions["settlement_lineage_hash"]:
                result, reasons = "DRIFTED", ["SETTLEMENT_LINEAGE_CHANGED"]
            elif preconditions.get("no_linked_evaluation") is not True:
                result, reasons = "DRIFTED", ["LINKED_EVALUATION_PRESENT"]
            elif (
                preconditions.get("proposal_not_previously_applied") is not True
                or preconditions.get("proposal_not_superseded") is not True
            ):
                result, reasons = "DRIFTED", ["PROPOSAL_APPLIED_OR_SUPERSEDED"]
            row = {
                "ticker": ticker,
                "proposal_row_hash": proposed.get("proposal_row_hash"),
                "review_result": result,
                "reason_codes": reasons or ["INDEPENDENT_REVIEW_PRECONDITIONS_SATISFIED"],
                "recomputed_settlement_lineage_hash": None
                if current is None
                else settlement_lineage_hash(dict(current)),
                "recomputed_evidence_hash": None
                if audited is None
                else audited.get("candidate_evidence_hash"),
                "compare_and_swap_verified": result == "ELIGIBLE",
            }
            row["review_row_hash"] = canonical_hash(row)
            reviewed_rows.append(row)
    finally:
        connection.close()
    counts = Counter(row["review_result"] for row in reviewed_rows)
    eligible_hashes = sorted(
        row["proposal_row_hash"] for row in reviewed_rows if row["review_result"] == "ELIGIBLE"
    )
    approval = _load_approval(approval_path)
    approval_status, approval_reasons = _approval_status(
        approval,
        proposal,
        ai_review,
        eligible_hashes,
        now,
        approval_clock_skew_seconds,
    )
    if counts["LINEAGE_FAILURE"]:
        state, reasons = "LINEAGE_FAILURE", ["ROW_LINEAGE_FAILURE"]
    elif expired:
        state, reasons = "PROPOSAL_EXPIRED", ["PROPOSAL_EXPIRED_AT_EVALUATION_TIME"]
    elif counts["DRIFTED"]:
        state, reasons = "CURRENT_STATE_DRIFTED", ["CURRENT_DATABASE_STATE_CHANGED"]
    elif counts["EVIDENCE_CONFLICT"]:
        state, reasons = "EVIDENCE_CONFLICT", ["AUTHORITATIVE_EVIDENCE_NO_LONGER_MATCHES"]
    elif approval_status == "INVALID":
        state, reasons = "HUMAN_APPROVAL_INVALID", approval_reasons
    elif approval_status == "REJECTED":
        state, reasons = "REJECTED", approval_reasons
    elif approval_status == "ABSENT":
        if eligible_hashes:
            state, reasons = "AWAITING_HUMAN_APPROVAL", approval_reasons
        else:
            state, reasons = "NO_ELIGIBLE_ROWS", ["NO_ROWS_ELIGIBLE_FOR_ADVANCEMENT"]
    else:
        if eligible_hashes:
            state, reasons = (
                "APPROVED_FOR_SEPARATELY_AUTHORIZED_EXECUTION",
                approval_reasons,
            )
        else:
            state, reasons = "NO_ELIGIBLE_ROWS", ["NO_ROWS_ELIGIBLE_FOR_ADVANCEMENT"]
    stat = source_db.stat()
    review_id = canonical_hash(
        {
            "proposal": proposal["artifact_hash"],
            "evaluated_at": now.isoformat(),
            "approval": None if approval is None else approval["artifact_hash"],
            "rows": reviewed_rows,
        }
    )
    attestation: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AJ",
        "review_id": review_id,
        "evaluated_at": now.isoformat(),
        "review_state": state,
        "reason_codes": reasons,
        "source_phase4ai_proposal_hash": proposal["artifact_hash"],
        "source_phase4ai_review_manifest_hash": ai_review["manifest_hash"],
        "source_phase4ah_gate_hash": gate["artifact_hash"],
        "source_phase4af_reaudit_hash": reaudit["artifact_hash"],
        "source_phase4ag_status_hash": status["artifact_hash"],
        "source_phase4ag_evidence_hash": evidence["artifact_hash"],
        "source_phase4ad_hash": ad["artifact_hash"],
        "source_phase4ae_hash": ae["artifact_hash"],
        "source_phase4ac_manifest_hash": history_hash,
        "production_database_identity": {
            "path": str(source_db),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        },
        "proposal_expires_at": expires.isoformat(),
        "proposal_expired": expired,
        "human_approval_status": approval_status,
        "human_approval_artifact_hash": None if approval is None else approval["artifact_hash"],
        "eligible_count": counts["ELIGIBLE"],
        "approved_count": len(eligible_hashes) if approval_status == "VALID" else 0,
        "rejected_count": counts["REJECTED"],
        "drifted_count": counts["DRIFTED"],
        "expired_count": len(reviewed_rows) if expired else 0,
        "ineligible_count": counts["INELIGIBLE"],
        "disposition_counts": dict(sorted(counts.items())),
        "contains_executable_sql": False,
        "database_mutation_performed": False,
        "execution_authorized": False,
        "rows_hash": canonical_hash(reviewed_rows),
        "rows": reviewed_rows,
    }
    pair_id = canonical_hash({"review_id": review_id, "state": state})
    attestation["publication_pair_id"] = pair_id
    attestation["artifact_hash"] = _hash(attestation)
    advancement_hashes = (
        eligible_hashes if state == "APPROVED_FOR_SEPARATELY_AUTHORIZED_EXECUTION" else []
    )
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "publication_pair_id": pair_id,
        "review_attestation_hash": attestation["artifact_hash"],
        "phase4ai_proposal_hash": proposal["artifact_hash"],
        "phase4ai_review_manifest_hash": ai_review["manifest_hash"],
        "eligible_proposal_row_hashes": advancement_hashes,
        "human_approval_artifact_hash": None if approval is None else approval["artifact_hash"],
        "expires_at": expires.isoformat(),
        "advancement_state": state,
        "separate_execution_implementation_and_authorization_required": True,
        "execution_authorized": False,
    }
    manifest["manifest_hash"] = _hash(manifest, "manifest_hash")
    return attestation, manifest


def publish_pair(
    attestation_path: Path,
    manifest_path: Path,
    attestation: dict[str, Any],
    manifest: dict[str, Any],
    *,
    replace: bool = False,
) -> None:
    if attestation_path.parent != manifest_path.parent:
        raise ValueError("PHASE4AJ_OUTPUT_DIRECTORIES_DIFFER")
    attestation_path.parent.mkdir(parents=True, exist_ok=True)
    if not replace and (attestation_path.exists() or manifest_path.exists()):
        raise FileExistsError("PHASE4AJ_OUTPUT_EXISTS")
    temporary = [
        attestation_path.with_name(f".{attestation_path.name}.{os.getpid()}.tmp"),
        manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp"),
    ]
    backups = [
        attestation_path.with_name(f".{attestation_path.name}.{os.getpid()}.bak"),
        manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.bak"),
    ]
    published: list[Path] = []
    try:
        for path, payload in zip(temporary, (attestation, manifest), strict=True):
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        if replace:
            for final, backup in zip((attestation_path, manifest_path), backups, strict=True):
                if final.exists():
                    os.replace(final, backup)
        for temp, final in zip(temporary, (attestation_path, manifest_path), strict=True):
            os.replace(temp, final)
            published.append(final)
        try:
            directory_fd = os.open(attestation_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except Exception:
        for final in published:
            if final.exists():
                final.unlink()
        for backup, final in zip(backups, (attestation_path, manifest_path), strict=True):
            if backup.exists():
                os.replace(backup, final)
        raise
    finally:
        for path in temporary + backups:
            if path.exists():
                path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase4ai-proposal", type=Path, required=True)
    parser.add_argument("--phase4ai-review-manifest", type=Path, required=True)
    parser.add_argument("--phase4ah-gate-artifact", type=Path, required=True)
    parser.add_argument("--phase4af-reaudit-artifact", type=Path, required=True)
    parser.add_argument("--baseline-phase4af-artifact", type=Path, required=True)
    parser.add_argument("--phase4ag-status-artifact", type=Path, required=True)
    parser.add_argument("--phase4ag-evidence-artifact", type=Path, required=True)
    parser.add_argument("--phase4ad-artifact", type=Path, required=True)
    parser.add_argument("--phase4ae-artifact", type=Path, required=True)
    parser.add_argument("--history-dir", type=Path, required=True)
    parser.add_argument("--production-db", type=Path, required=True)
    parser.add_argument("--human-approval-artifact", type=Path)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--approval-clock-skew-seconds", type=int, default=300)
    parser.add_argument("--attestation-output", type=Path, required=True)
    parser.add_argument("--advancement-manifest-output", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    attestation, manifest = build(
        args.production_db,
        args.phase4ai_proposal,
        args.phase4ai_review_manifest,
        args.phase4ah_gate_artifact,
        args.phase4af_reaudit_artifact,
        args.baseline_phase4af_artifact,
        args.phase4ag_status_artifact,
        args.phase4ag_evidence_artifact,
        args.phase4ad_artifact,
        args.phase4ae_artifact,
        args.history_dir,
        now=_time(args.evaluation_time, "EVALUATION_TIME"),
        approval_path=args.human_approval_artifact,
        approval_clock_skew_seconds=args.approval_clock_skew_seconds,
    )
    publish_pair(
        args.attestation_output,
        args.advancement_manifest_output,
        attestation,
        manifest,
        replace=args.replace,
    )
    print(
        json.dumps(
            {key: value for key, value in attestation.items() if key != "rows"}, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
