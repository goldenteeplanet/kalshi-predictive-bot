"""Phase 4AH read-only settlement timestamp re-audit and advancement gate."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4ah.settlement-timestamp-readiness-gate.v1"
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
        raise ValueError(f"PHASE4AH_{label}_SCHEMA_INVALID")
    if payload.get("artifact_hash") != artifact_hash(payload):
        raise ValueError(f"PHASE4AH_{label}_HASH_MISMATCH")
    rows = payload.get("rows")
    if not isinstance(rows, list) or payload.get("rows_hash") != canonical_hash(rows):
        raise ValueError(f"PHASE4AH_{label}_ROWS_HASH_MISMATCH")
    return payload


def _module(name: str, filename: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise ValueError(f"PHASE4AH_{name.upper()}_LOADER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _history_hash(history_dir: Path) -> str:
    module = _module("phase4ac_for_4ah", "phase4ac_artifact_history.py")
    return str(module.reconstruct_phase4ab_history(history_dir)["manifest_hash"])


def validate_pair(
    status: dict[str, Any], evidence: dict[str, Any], baseline: dict[str, Any]
) -> None:
    if status.get("pair_id") != evidence.get("pair_id"):
        raise ValueError("PHASE4AH_PAIR_ID_MISMATCH")
    if evidence.get("source_phase4ag_artifact_hash") != status["artifact_hash"]:
        raise ValueError("PHASE4AH_EVIDENCE_STATUS_HASH_MISMATCH")
    if status.get("source_phase4af_artifact_hash") != baseline["artifact_hash"]:
        raise ValueError("PHASE4AH_PHASE4AG_BASELINE_LINEAGE_MISMATCH")
    successful = {
        row["ticker"]: row
        for row in status["rows"]
        if row.get("disposition")
        in {"ARCHIVED_EXCHANGE_EVIDENCE_FOUND", "DIRECT_EXCHANGE_EVIDENCE_FOUND"}
    }
    seen: set[str] = set()
    for row in evidence["rows"]:
        if set(row) != {"ticker", "settlement_timestamp", "source_record_identity"}:
            raise ValueError("PHASE4AH_EVIDENCE_FIELDS_INVALID")
        ticker = str(row["ticker"])
        if ticker in seen:
            raise ValueError("PHASE4AH_DUPLICATE_EVIDENCE_TICKER")
        seen.add(ticker)
        source = successful.get(ticker)
        if source is None:
            raise ValueError("PHASE4AH_EVIDENCE_TICKER_NOT_SUCCESSFUL")
        expected_identity = (
            f"phase4ag:{source['source_kind']}:{source['response_hash']}:{source['field_path']}"
        )
        if row["settlement_timestamp"] != source.get("normalized_timestamp"):
            raise ValueError("PHASE4AH_EVIDENCE_TIMESTAMP_MISMATCH")
        if row["source_record_identity"] != expected_identity:
            raise ValueError("PHASE4AH_EVIDENCE_SOURCE_IDENTITY_MISMATCH")
    if set(successful) != seen:
        raise ValueError("PHASE4AH_SUCCESSFUL_STATUS_EVIDENCE_SET_MISMATCH")


def transition(baseline: dict[str, Any] | None, current: dict[str, Any] | None) -> str:
    if baseline is None:
        return "ROW_ADDED"
    if current is None:
        return "ROW_REMOVED"
    before, after = baseline.get("classification"), current.get("classification")
    if baseline.get("settlement_lineage_hash") != current.get("settlement_lineage_hash"):
        return "LINEAGE_DRIFTED"
    if current.get("source_phase4ae_disposition") == "EVALUATION_APPEARED":
        return "EVALUATION_APPEARED"
    if before == after:
        return "UNCHANGED_BLOCKED"
    if after == "VALIDATED_ARTIFACT_TIMESTAMP_AVAILABLE":
        return "AUTHORITATIVE_EVIDENCE_ADDED"
    if after == "CANONICAL_SETTLED_AT_PRESENT":
        return "CANONICAL_TIMESTAMP_APPEARED"
    if after == "TIMESTAMP_CONFLICT":
        return "CONFLICT_DISCOVERED"
    if after == "TIMEZONE_AMBIGUOUS":
        return "TIMEZONE_AMBIGUITY_DISCOVERED"
    if after == "SOURCE_STALE":
        return "SOURCE_BECAME_STALE"
    if after == "SOURCE_MISSING":
        return "SOURCE_DISAPPEARED"
    if after == "LINEAGE_FAILURE":
        return "LINEAGE_DRIFTED"
    return "UNEXPECTED_TRANSITION"


def _gate_state(
    transitions: list[dict[str, Any]], reaudit: dict[str, Any]
) -> tuple[str, list[str]]:
    if not transitions:
        return "NO_ELIGIBLE_ROWS", ["VALIDATED_COHORT_EMPTY"]
    names = Counter(row["transition"] for row in transitions)
    classifications = Counter(row.get("reaudit_classification") for row in transitions)
    if names["UNEXPECTED_TRANSITION"]:
        return "BLOCKED", ["UNEXPECTED_TRANSITION_PRESENT"]
    if names["LINEAGE_DRIFTED"] or names["ROW_REMOVED"] or names["ROW_ADDED"]:
        return "ATTENTION_LINEAGE", ["LINEAGE_OR_COHORT_CHANGED"]
    if classifications["TIMESTAMP_CONFLICT"] or names["CONFLICT_DISCOVERED"]:
        return "ATTENTION_CONFLICT", ["AUTHORITATIVE_TIMESTAMP_CONFLICT"]
    if classifications["SOURCE_STALE"] or names["SOURCE_BECAME_STALE"]:
        return "ATTENTION_STALE", ["SOURCE_EVIDENCE_STALE"]
    ready = int(reaudit["ready_count"])
    total = int(reaudit["input_row_count"])
    if total and ready == total:
        return "READY_FOR_CANONICALIZATION_PROPOSAL", ["ALL_ROWS_HAVE_UNIQUE_EVIDENCE"]
    if ready:
        return "PARTIALLY_READY", ["SOME_ROWS_REMAIN_BLOCKED"]
    return "WAITING_FOR_EVIDENCE", ["NO_ADMISSIBLE_TIMESTAMP_ADDED"]


def build(
    source_db: Path,
    baseline_path: Path,
    status_path: Path,
    evidence_path: Path,
    ad_path: Path,
    ae_path: Path,
    history_dir: Path,
    *,
    now: datetime,
    freshness_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    baseline = _load(baseline_path, AF_SCHEMA, "BASELINE_PHASE4AF")
    status = _load(status_path, AG_SCHEMA, "PHASE4AG_STATUS")
    evidence = _load(evidence_path, EVIDENCE_SCHEMA, "PHASE4AG_EVIDENCE")
    ad = _load(ad_path, AD_SCHEMA, "PHASE4AD")
    ae = _load(ae_path, AE_SCHEMA, "PHASE4AE")
    manifest_hash = _history_hash(history_dir)
    validate_pair(status, evidence, baseline)
    if baseline.get("source_phase4ad_artifact_hash") != ad["artifact_hash"]:
        raise ValueError("PHASE4AH_BASELINE_AD_LINEAGE_MISMATCH")
    if baseline.get("source_phase4ae_artifact_hash") != ae["artifact_hash"]:
        raise ValueError("PHASE4AH_BASELINE_AE_LINEAGE_MISMATCH")
    if baseline.get("source_phase4ac_manifest_hash") != manifest_hash:
        raise ValueError("PHASE4AH_BASELINE_AC_LINEAGE_MISMATCH")
    af_module = _module("phase4af_for_4ah", "phase4af_settlement_timestamp_audit.py")
    reaudit = af_module.audit(
        source_db,
        ad_path,
        ae_path,
        history_dir,
        now=now,
        freshness_seconds=freshness_seconds,
        evidence_dir=evidence_path,
    )
    baseline_index = {(row.get("capture_id"), row.get("ticker")): row for row in baseline["rows"]}
    reaudit_index = {(row.get("capture_id"), row.get("ticker")): row for row in reaudit["rows"]}
    if len(baseline_index) != len(baseline["rows"]):
        raise ValueError("PHASE4AH_DUPLICATE_BASELINE_ROW")
    if len(reaudit_index) != len(reaudit["rows"]):
        raise ValueError("PHASE4AH_DUPLICATE_REAUDIT_ROW")
    rows = []
    for key in sorted(
        set(baseline_index) | set(reaudit_index), key=lambda item: (str(item[1]), str(item[0]))
    ):
        before, after = baseline_index.get(key), reaudit_index.get(key)
        row: dict[str, Any] = {
            "capture_id": key[0],
            "ticker": key[1],
            "baseline_classification": before.get("classification") if before else None,
            "reaudit_classification": after.get("classification") if after else None,
            "transition": transition(before, after),
            "baseline_row_hash": before.get("row_hash") if before else None,
            "reaudit_row_hash": after.get("row_hash") if after else None,
        }
        row["row_hash"] = canonical_hash(row)
        rows.append(row)
    state, reasons = _gate_state(rows, reaudit)
    transition_counts = Counter(row["transition"] for row in rows)
    classification_counts = Counter(row["reaudit_classification"] for row in rows)
    pair_id = canonical_hash(
        {"baseline": baseline["artifact_hash"], "reaudit": reaudit["artifact_hash"], "rows": rows}
    )
    gate: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AH",
        "generated_at": now.astimezone(UTC).isoformat(),
        "publication_pair_id": pair_id,
        "baseline_phase4af_path": str(baseline_path),
        "baseline_phase4af_artifact_hash": baseline["artifact_hash"],
        "reaudit_phase4af_artifact_hash": reaudit["artifact_hash"],
        "phase4ag_status_path": str(status_path),
        "phase4ag_status_artifact_hash": status["artifact_hash"],
        "phase4ag_evidence_path": str(evidence_path),
        "phase4ag_evidence_artifact_hash": evidence["artifact_hash"],
        "phase4ag_pair_id": status["pair_id"],
        "source_phase4ad_artifact_hash": ad["artifact_hash"],
        "source_phase4ae_artifact_hash": ae["artifact_hash"],
        "source_phase4ac_manifest_hash": manifest_hash,
        "production_database_identity": reaudit["production_database_identity"],
        "freshness_seconds": freshness_seconds,
        "input_count": len(baseline["rows"]),
        "reaudit_count": len(reaudit["rows"]),
        "transition_counts": dict(sorted(transition_counts.items())),
        "baseline_classification_counts": baseline["classification_counts"],
        "reaudit_classification_counts": dict(sorted(classification_counts.items())),
        "ready_count": reaudit["ready_count"],
        "blocked_count": reaudit["blocked_count"],
        "conflict_count": classification_counts["TIMESTAMP_CONFLICT"],
        "lineage_failure_count": transition_counts["LINEAGE_DRIFTED"],
        "gate_state": state,
        "gate_reason_codes": reasons,
        "safe_for_canonicalization_proposal": state == "READY_FOR_CANONICALIZATION_PROPOSAL",
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
    gate["artifact_hash"] = artifact_hash(gate)
    reaudit["publication_pair_id"] = pair_id
    reaudit["artifact_hash"] = af_module.artifact_hash(reaudit)
    gate["reaudit_phase4af_artifact_hash"] = reaudit["artifact_hash"]
    gate["artifact_hash"] = artifact_hash(gate)
    return reaudit, gate


def publish_pair(
    reaudit_path: Path,
    gate_path: Path,
    reaudit: dict[str, Any],
    gate: dict[str, Any],
    *,
    replace: bool = False,
) -> None:
    if reaudit_path.parent != gate_path.parent:
        raise ValueError("PHASE4AH_OUTPUT_DIRECTORIES_DIFFER")
    reaudit_path.parent.mkdir(parents=True, exist_ok=True)
    if not replace and (reaudit_path.exists() or gate_path.exists()):
        raise FileExistsError("PHASE4AH_OUTPUT_EXISTS")
    temporary_paths = [
        reaudit_path.with_name(f".{reaudit_path.name}.{os.getpid()}.tmp"),
        gate_path.with_name(f".{gate_path.name}.{os.getpid()}.tmp"),
    ]
    try:
        for path, payload in zip(temporary_paths, (reaudit, gate), strict=True):
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary_paths[0], reaudit_path)
        os.replace(temporary_paths[1], gate_path)
        try:
            fd = os.open(reaudit_path.parent, os.O_RDONLY)
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
    parser.add_argument("--baseline-phase4af-artifact", type=Path, required=True)
    parser.add_argument("--phase4ag-status-artifact", type=Path, required=True)
    parser.add_argument("--phase4ag-evidence-artifact", type=Path, required=True)
    parser.add_argument("--phase4ad-artifact", type=Path, required=True)
    parser.add_argument("--phase4ae-artifact", type=Path, required=True)
    parser.add_argument("--history-dir", type=Path, required=True)
    parser.add_argument("--production-db", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--freshness-seconds", type=int, default=1800)
    parser.add_argument("--reaudit-output", type=Path, required=True)
    parser.add_argument("--gate-output", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    if now.tzinfo is None:
        raise ValueError("PHASE4AH_EVALUATION_TIMEZONE_MISSING")
    reaudit, gate = build(
        args.production_db,
        args.baseline_phase4af_artifact,
        args.phase4ag_status_artifact,
        args.phase4ag_evidence_artifact,
        args.phase4ad_artifact,
        args.phase4ae_artifact,
        args.history_dir,
        now=now,
        freshness_seconds=args.freshness_seconds,
    )
    publish_pair(args.reaudit_output, args.gate_output, reaudit, gate, replace=args.replace)
    print(json.dumps({key: value for key, value in gate.items() if key != "rows"}, sort_keys=True))


if __name__ == "__main__":
    main()
