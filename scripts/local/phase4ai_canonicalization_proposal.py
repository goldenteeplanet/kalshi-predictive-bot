"""Phase 4AI deterministic non-executable settlement timestamp proposal."""

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

SCHEMA = "phase4ai.settlement-timestamp-canonicalization-proposal.v1"
REVIEW_SCHEMA = "phase4ai.settlement-timestamp-review-manifest.v1"
AH_SCHEMA = "phase4ah.settlement-timestamp-readiness-gate.v1"
AF_SCHEMA = "phase4af.settlement-timestamp-audit.v1"
AG_SCHEMA = "phase4ag.settlement-timestamp-collection.v1"
EVIDENCE_SCHEMA = "phase4af.settlement-timestamp-evidence.v1"
AD_SCHEMA = "phase4ad.reconciliation-attribution.v1"
AE_SCHEMA = "phase4ae.reconciliation-plan.v1"


def artifact_hash(payload: dict[str, Any]) -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != "artifact_hash"})


def _load(path: Path, schema: str, label: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != schema:
        raise ValueError(f"PHASE4AI_{label}_SCHEMA_INVALID")
    if payload.get("artifact_hash") != artifact_hash(payload):
        raise ValueError(f"PHASE4AI_{label}_HASH_MISMATCH")
    rows = payload.get("rows")
    if not isinstance(rows, list) or payload.get("rows_hash") != canonical_hash(rows):
        raise ValueError(f"PHASE4AI_{label}_ROWS_HASH_MISMATCH")
    return payload


def _history_hash(history_dir: Path) -> str:
    path = Path(__file__).with_name("phase4ac_artifact_history.py")
    spec = importlib.util.spec_from_file_location("phase4ac_for_4ai", path)
    if not spec or not spec.loader:
        raise ValueError("PHASE4AI_HISTORY_LOADER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.reconstruct_phase4ab_history(history_dir)["manifest_hash"])


def _ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError("PHASE4AI_DATABASE_NOT_FILE")
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise ValueError("PHASE4AI_QUERY_ONLY_NOT_ENFORCED")
    return connection


def _validate_lineage(
    gate: dict[str, Any],
    reaudit: dict[str, Any],
    baseline: dict[str, Any],
    status: dict[str, Any],
    evidence: dict[str, Any],
    ad: dict[str, Any],
    ae: dict[str, Any],
    manifest_hash: str,
) -> None:
    checks = {
        "GATE_REAUDIT": gate.get("reaudit_phase4af_artifact_hash") == reaudit["artifact_hash"],
        "GATE_BASELINE": gate.get("baseline_phase4af_artifact_hash") == baseline["artifact_hash"],
        "GATE_AG_STATUS": gate.get("phase4ag_status_artifact_hash") == status["artifact_hash"],
        "GATE_AG_EVIDENCE": gate.get("phase4ag_evidence_artifact_hash")
        == evidence["artifact_hash"],
        "AG_PAIR": status.get("pair_id") == evidence.get("pair_id") == gate.get("phase4ag_pair_id"),
        "AG_EVIDENCE_STATUS": evidence.get("source_phase4ag_artifact_hash")
        == status["artifact_hash"],
        "AD": gate.get("source_phase4ad_artifact_hash") == ad["artifact_hash"],
        "AE": gate.get("source_phase4ae_artifact_hash") == ae["artifact_hash"],
        "AC": gate.get("source_phase4ac_manifest_hash") == manifest_hash,
        "AH_PAIR": gate.get("publication_pair_id") == reaudit.get("publication_pair_id"),
    }
    failed = [name for name, valid in checks.items() if not valid]
    if failed:
        raise ValueError(f"PHASE4AI_LINEAGE_FAILURE_{failed[0]}")


def _proposal_state(counts: Counter[str], proposed: int, total: int) -> tuple[str, list[str]]:
    if total == 0:
        return "NO_ELIGIBLE_ROWS", ["PHASE4AH_COHORT_EMPTY"]
    conflict = counts["TIMESTAMP_CONFLICT"] + counts["SETTLEMENT_RESULT_CHANGED"]
    drift = (
        counts["CANONICAL_TIMESTAMP_ALREADY_PRESENT"]
        + counts["SETTLEMENT_LINEAGE_DRIFTED"]
        + counts["EVIDENCE_DRIFTED"]
        + counts["EVALUATION_APPEARED"]
    )
    if conflict:
        return "ATTENTION_CONFLICT", ["RESULT_OR_TIMESTAMP_CONFLICT"]
    if drift:
        return "ATTENTION_DRIFT", ["CURRENT_STATE_CHANGED"]
    if proposed == total:
        return "READY_FOR_REVIEW", ["ALL_ROWS_PROPOSED_FOR_REVIEW"]
    if proposed:
        return "PARTIALLY_READY_FOR_REVIEW", ["SOME_ROWS_EXCLUDED"]
    return "NO_ELIGIBLE_ROWS", ["NO_PROPOSABLE_TRANSITIONS"]


def build(
    source_db: Path,
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
    valid_for_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AI_EVALUATION_TIMEZONE_MISSING")
    if valid_for_seconds <= 0:
        raise ValueError("PHASE4AI_VALIDITY_INVALID")
    now = now.astimezone(UTC)
    expires = now + timedelta(seconds=valid_for_seconds)
    gate = _load(gate_path, AH_SCHEMA, "PHASE4AH_GATE")
    reaudit = _load(reaudit_path, AF_SCHEMA, "PHASE4AF_REAUDIT")
    baseline = _load(baseline_path, AF_SCHEMA, "BASELINE_PHASE4AF")
    status = _load(status_path, AG_SCHEMA, "PHASE4AG_STATUS")
    evidence = _load(evidence_path, EVIDENCE_SCHEMA, "PHASE4AG_EVIDENCE")
    ad = _load(ad_path, AD_SCHEMA, "PHASE4AD")
    ae = _load(ae_path, AE_SCHEMA, "PHASE4AE")
    manifest_hash = _history_hash(history_dir)
    _validate_lineage(gate, reaudit, baseline, status, evidence, ad, ae, manifest_hash)
    gate_index = {(row.get("capture_id"), row.get("ticker")): row for row in gate["rows"]}
    reaudit_index = {(row.get("capture_id"), row.get("ticker")): row for row in reaudit["rows"]}
    if len(gate_index) != len(gate["rows"]) or len(reaudit_index) != len(reaudit["rows"]):
        raise ValueError("PHASE4AI_DUPLICATE_INPUT_ROW")
    status_index = {str(row["ticker"]): row for row in status["rows"]}
    evidence_index = {str(row["ticker"]): row for row in evidence["rows"]}
    if len(status_index) != len(status["rows"]) or len(evidence_index) != len(evidence["rows"]):
        raise ValueError("PHASE4AI_DUPLICATE_TICKER")
    connection = _ro(source_db)
    rows: list[dict[str, Any]] = []
    try:
        for key, transition_row in sorted(
            gate_index.items(), key=lambda item: (str(item[0][1]), str(item[0][0]))
        ):
            capture_id, ticker = key
            audited = reaudit_index.get(key)
            current = connection.execute(
                "SELECT * FROM settlements WHERE ticker=?", (ticker,)
            ).fetchone()
            reasons: list[str] = []
            disposition = "PROPOSED"
            if gate.get("gate_state") not in {
                "READY_FOR_CANONICALIZATION_PROPOSAL",
                "PARTIALLY_READY",
            }:
                disposition, reasons = "GATE_NOT_READY", ["PHASE4AH_GATE_NOT_READY"]
            elif transition_row.get("transition") != "AUTHORITATIVE_EVIDENCE_ADDED":
                disposition, reasons = "NO_ELIGIBLE_TRANSITION", ["TRANSITION_NOT_EVIDENCE_ADDED"]
            elif audited is None:
                disposition, reasons = "LINEAGE_FAILURE", ["REAUDIT_ROW_MISSING"]
            elif current is None:
                disposition, reasons = "SOURCE_MISSING", ["SETTLEMENT_ROW_MISSING"]
            elif current["settled_at"] not in (None, ""):
                disposition, reasons = (
                    "CANONICAL_TIMESTAMP_ALREADY_PRESENT",
                    ["SETTLED_AT_APPEARED"],
                )
            elif binary_outcome(current["result"]) is None:
                disposition, reasons = "SETTLEMENT_RESULT_CHANGED", ["RESULT_NOT_BINARY"]
            elif settlement_lineage_hash(dict(current)) != audited.get("settlement_lineage_hash"):
                disposition, reasons = "SETTLEMENT_LINEAGE_DRIFTED", ["CURRENT_HASH_CHANGED"]
            elif audited.get("classification") == "TIMESTAMP_CONFLICT":
                disposition, reasons = "TIMESTAMP_CONFLICT", ["AUTHORITATIVE_TIMES_CONFLICT"]
            elif not audited.get("timezone_unambiguous"):
                disposition, reasons = "TIMEZONE_AMBIGUOUS", ["TIMEZONE_NOT_EXPLICIT"]
            elif not audited.get("source_fresh"):
                disposition, reasons = "PROPOSAL_SOURCE_STALE", ["SOURCE_NOT_FRESH"]
            elif not audited.get("ready_for_future_canonicalization"):
                disposition, reasons = "PROPOSAL_BLOCKED", ["REAUDIT_NOT_READY"]
            elif audited.get("source_phase4ae_disposition") == "EVALUATION_APPEARED":
                disposition, reasons = "EVALUATION_APPEARED", ["CAPTURE_ALREADY_EVALUATED"]
            ag_row = status_index.get(str(ticker))
            evidence_row = evidence_index.get(str(ticker))
            proposed_at = audited.get("normalized_timestamp_candidate") if audited else None
            if disposition == "PROPOSED" and (ag_row is None or evidence_row is None):
                disposition, reasons = "EVIDENCE_DRIFTED", ["PHASE4AG_EVIDENCE_MISSING"]
            if (
                disposition == "PROPOSED"
                and evidence_row.get("settlement_timestamp") != proposed_at
            ):
                disposition, reasons = "EVIDENCE_DRIFTED", ["PROPOSED_TIMESTAMP_MISMATCH"]
            expected = {
                "settled_at": None,
                "result": None if current is None else current["result"],
                "settlement_lineage_hash": None
                if current is None
                else settlement_lineage_hash(dict(current)),
                "updated_at": None if current is None else current["updated_at"],
                "no_linked_evaluation": True,
                "proposal_not_expired_before": expires.isoformat(),
                "proposal_not_previously_applied": True,
                "proposal_not_superseded": True,
            }
            row: dict[str, Any] = {
                "ticker": ticker,
                "linked_capture_ids": []
                if ag_row is None
                else ag_row.get("linked_capture_ids", []),
                "capture_lineage_hashes": []
                if ag_row is None
                else ag_row.get("capture_lineage_hashes", []),
                "expected_result": expected["result"],
                "existing_settled_at": None if current is None else current["settled_at"],
                "proposed_settled_at": proposed_at,
                "evidence_source_identity": None
                if evidence_row is None
                else evidence_row["source_record_identity"],
                "evidence_hash": None
                if audited is None
                else audited.get("candidate_evidence_hash"),
                "phase4ag_status_row_hash": None if ag_row is None else ag_row.get("row_hash"),
                "phase4af_reaudit_row_hash": None if audited is None else audited.get("row_hash"),
                "settlement_lineage_hash": expected["settlement_lineage_hash"],
                "compare_and_swap_preconditions": expected,
                "semantic_mutation": {
                    "operation": "SET_CANONICAL_SETTLED_AT_IF_UNCHANGED",
                    "table": "settlements",
                    "identity": {"ticker": ticker},
                    "expected": {
                        "settled_at": None,
                        "result": expected["result"],
                        "settlement_lineage_hash": expected["settlement_lineage_hash"],
                    },
                    "proposed": {"settled_at": proposed_at},
                    "executable": False,
                },
                "disposition": disposition,
                "reason_codes": reasons or ["ALL_PROPOSAL_PRECONDITIONS_SATISFIED"],
            }
            row["proposal_row_hash"] = canonical_hash(row)
            rows.append(row)
    finally:
        connection.close()
    counts = Counter(row["disposition"] for row in rows)
    proposed_count = counts["PROPOSED"]
    state, state_reasons = _proposal_state(counts, proposed_count, len(rows))
    proposal_id = canonical_hash(
        {"phase4ah": gate["artifact_hash"], "generated_at": now.isoformat(), "rows": rows}
    )
    stat = source_db.stat()
    proposal: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AI",
        "generated_at": now.isoformat(),
        "proposal_id": proposal_id,
        "proposal_expires_at": expires.isoformat(),
        "source_phase4ah_gate_hash": gate["artifact_hash"],
        "source_phase4af_reaudit_hash": reaudit["artifact_hash"],
        "source_baseline_phase4af_hash": baseline["artifact_hash"],
        "source_phase4ag_status_hash": status["artifact_hash"],
        "source_phase4ag_evidence_hash": evidence["artifact_hash"],
        "source_phase4ag_pair_id": status["pair_id"],
        "source_phase4ah_publication_pair_id": gate["publication_pair_id"],
        "source_phase4ad_hash": ad["artifact_hash"],
        "source_phase4ae_hash": ae["artifact_hash"],
        "source_phase4ac_manifest_hash": manifest_hash,
        "production_database_identity": {
            "path": str(source_db),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        },
        "proposal_state": state,
        "proposal_reason_codes": state_reasons,
        "input_count": len(rows),
        "proposed_count": proposed_count,
        "excluded_count": len(rows) - proposed_count,
        "disposition_counts": dict(sorted(counts.items())),
        "write_authorized": False,
        "execution_authorized": False,
        "contains_executable_sql": False,
        "production_database_written": False,
        "research_database_written": False,
        "canonical_timestamp_written": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "trading_mode_changed": False,
        "orders_created": False,
        "existing_artifacts_modified": False,
        "rows_hash": canonical_hash(rows),
        "rows": rows,
    }
    proposal["artifact_hash"] = artifact_hash(proposal)
    assertions = [
        "EXACT_EXCHANGE_EVIDENCE_INSPECTED",
        "TICKER_AND_RESULT_MATCH",
        "TIMESTAMP_IS_EXPLICIT_SETTLEMENT_OR_DETERMINATION_TIME",
        "TIMEZONE_IS_EXPLICIT",
        "TIMESTAMP_FOLLOWS_LINKED_CAPTURES",
        "CANONICAL_TIMESTAMP_REMAINS_ABSENT",
        "NO_EVALUATION_APPEARED",
        "NO_CONFLICT_EXISTS",
        "PROPOSAL_NOT_EXPIRED",
        "DATABASE_MUTATION_HAS_NOT_OCCURRED",
    ]
    review: dict[str, Any] = {
        "schema": REVIEW_SCHEMA,
        "publication_pair_id": proposal_id,
        "proposal_artifact_hash": proposal["artifact_hash"],
        "proposal_id": proposal_id,
        "proposal_expires_at": expires.isoformat(),
        "proposed_ticker_count": proposed_count,
        "proposed_tickers": sorted(
            row["ticker"] for row in rows if row["disposition"] == "PROPOSED"
        ),
        "proposal_rows_hash": proposal["rows_hash"],
        "required_reviewer_assertions": assertions,
        "required_independent_verification_steps": assertions,
        "approval_status": "UNREVIEWED",
        "execution_authorized": False,
    }
    review["manifest_hash"] = canonical_hash(
        {key: value for key, value in review.items() if key != "manifest_hash"}
    )
    return proposal, review


def publish_pair(
    proposal_path: Path,
    review_path: Path,
    proposal: dict[str, Any],
    review: dict[str, Any],
    *,
    replace: bool = False,
) -> None:
    if proposal_path.parent != review_path.parent:
        raise ValueError("PHASE4AI_OUTPUT_DIRECTORIES_DIFFER")
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    if not replace and (proposal_path.exists() or review_path.exists()):
        raise FileExistsError("PHASE4AI_OUTPUT_EXISTS")
    temporary_paths = [
        proposal_path.with_name(f".{proposal_path.name}.{os.getpid()}.tmp"),
        review_path.with_name(f".{review_path.name}.{os.getpid()}.tmp"),
    ]
    try:
        for path, payload in zip(temporary_paths, (proposal, review), strict=True):
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary_paths[0], proposal_path)
        os.replace(temporary_paths[1], review_path)
        try:
            fd = os.open(proposal_path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
    finally:
        for path in temporary_paths:
            if path.exists():
                path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
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
    parser.add_argument("--proposal-valid-for-seconds", type=int, default=3600)
    parser.add_argument("--proposal-output", type=Path, required=True)
    parser.add_argument("--review-manifest-output", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    proposal, review = build(
        args.production_db,
        args.phase4ah_gate_artifact,
        args.phase4af_reaudit_artifact,
        args.baseline_phase4af_artifact,
        args.phase4ag_status_artifact,
        args.phase4ag_evidence_artifact,
        args.phase4ad_artifact,
        args.phase4ae_artifact,
        args.history_dir,
        now=now,
        valid_for_seconds=args.proposal_valid_for_seconds,
    )
    publish_pair(
        args.proposal_output,
        args.review_manifest_output,
        proposal,
        review,
        replace=args.replace,
    )
    print(
        json.dumps({key: value for key, value in proposal.items() if key != "rows"}, sort_keys=True)
    )


if __name__ == "__main__":
    main()
