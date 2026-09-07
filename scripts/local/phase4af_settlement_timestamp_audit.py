"""Phase 4AF read-only settlement timestamp canonicalization readiness audit."""

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
    normalized_timestamp,
    settlement_lineage_hash,
)

SCHEMA = "phase4af.settlement-timestamp-audit.v1"
AD_SCHEMA = "phase4ad.reconciliation-attribution.v1"
AE_SCHEMA = "phase4ae.reconciliation-plan.v1"
EVIDENCE_SCHEMA = "phase4af.settlement-timestamp-evidence.v1"
POLICY_VERSION = "phase4af.timestamp-precedence.v1"
BLOCKED_AD_CLASSIFICATIONS = {
    "SETTLEMENT_RESULT_UNUSABLE",
    "SETTLEMENT_LINEAGE_CONFLICT",
    "CAPTURE_LINEAGE_INCOMPLETE",
    "PROBABILITY_INPUT_INVALID",
    "EXECUTABLE_EVIDENCE_INCOMPLETE",
    "SOURCE_ARTIFACT_STALE",
    "UNKNOWN_BLOCKER",
}
EXCHANGE_TIMESTAMP_KEYS = (
    "settled_at",
    "settlement_ts",
    "settlement_time",
    "result_ts",
    "result_time",
    "determination_ts",
)
CONTEXT_MARKET_COLUMNS = ("close_time", "expected_expiration_time", "expiration_time")


def artifact_hash(payload: dict[str, Any]) -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != "artifact_hash"})


def _strict_utc(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("TIMEZONE_MISSING")
    return parsed.astimezone(UTC)


def source_freshness(
    now: datetime, reference: object, freshness_seconds: int
) -> tuple[float | None, bool]:
    try:
        age = (_strict_utc(now) - _strict_utc(reference)).total_seconds()
    except (TypeError, ValueError):
        return None, False
    return age, 0 <= age < freshness_seconds


def _ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError("PHASE4AF_DATABASE_NOT_FILE")
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise ValueError("PHASE4AF_QUERY_ONLY_NOT_ENFORCED")
    return connection


def _load_artifact(path: Path, schema: str, label: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != schema:
        raise ValueError(f"PHASE4AF_{label}_SCHEMA_INVALID")
    if payload.get("artifact_hash") != artifact_hash(payload):
        raise ValueError(f"PHASE4AF_{label}_HASH_MISMATCH")
    rows = payload.get("rows")
    if not isinstance(rows, list) or payload.get("rows_hash") != canonical_hash(rows):
        raise ValueError(f"PHASE4AF_{label}_ROWS_HASH_MISMATCH")
    return payload


def _history_hash(history_dir: Path) -> str:
    script = Path(__file__).with_name("phase4ac_artifact_history.py")
    spec = importlib.util.spec_from_file_location("phase4ac_for_4af", script)
    if not spec or not spec.loader:
        raise ValueError("PHASE4AF_HISTORY_LOADER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.reconstruct_phase4ab_history(history_dir)["manifest_hash"])


def _candidate(
    source_type: str,
    identity: str,
    value: object,
    *,
    authority: str,
    provenance: Any,
) -> dict[str, Any]:
    timezone_explicit = False
    normalized = None
    reason = None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        timezone_explicit = parsed.tzinfo is not None
        if not timezone_explicit:
            reason = "TIMEZONE_MISSING"
        else:
            normalized = normalized_timestamp(parsed)
    except (TypeError, ValueError):
        reason = "TIMESTAMP_MALFORMED"
    body = {
        "source_type": source_type,
        "source_record_identity": identity,
        "original_timestamp": value,
        "normalized_utc_timestamp": normalized,
        "timezone_explicit": timezone_explicit,
        "authority": authority,
        "inadmissible_reason": reason,
        "provenance_hash": canonical_hash(provenance),
    }
    body["evidence_hash"] = canonical_hash(body)
    return body


def _raw_exchange_candidates(settlement: dict[str, Any]) -> list[dict[str, Any]]:
    raw = settlement.get("raw_json")
    try:
        decoded = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(decoded, dict):
        return []
    candidates = []
    for key in EXCHANGE_TIMESTAMP_KEYS:
        value = decoded.get(key)
        if value not in (None, ""):
            candidates.append(
                _candidate(
                    "EXCHANGE_SETTLEMENT_TIMESTAMP",
                    f"settlements:{settlement.get('ticker')}:raw_json:{key}",
                    value,
                    authority="authoritative",
                    provenance={"ticker": settlement.get("ticker"), "key": key, "raw_json": raw},
                )
            )
    return candidates


def _evidence_artifacts(directory: Path | None) -> dict[str, list[dict[str, Any]]]:
    indexed: dict[str, list[dict[str, Any]]] = {}
    if directory is None:
        return indexed
    if directory.is_file():
        paths = [directory]
    elif directory.is_dir():
        paths = sorted(directory.glob("*.json"))
    else:
        raise ValueError("PHASE4AF_EVIDENCE_DIRECTORY_MISSING")
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != EVIDENCE_SCHEMA:
            raise ValueError("PHASE4AF_EVIDENCE_SCHEMA_INVALID")
        if payload.get("artifact_hash") != artifact_hash(payload):
            raise ValueError("PHASE4AF_EVIDENCE_HASH_MISMATCH")
        rows = payload.get("rows")
        if not isinstance(rows, list) or payload.get("rows_hash") != canonical_hash(rows):
            raise ValueError("PHASE4AF_EVIDENCE_ROWS_HASH_MISMATCH")
        for row in rows:
            if set(row) != {"ticker", "settlement_timestamp", "source_record_identity"}:
                raise ValueError("PHASE4AF_EVIDENCE_FIELDS_INVALID")
            indexed.setdefault(str(row["ticker"]), []).append(
                _candidate(
                    "VALIDATED_SETTLEMENT_ARTIFACT",
                    str(row["source_record_identity"]),
                    row["settlement_timestamp"],
                    authority="authoritative",
                    provenance={"artifact_hash": payload["artifact_hash"], "row": row},
                )
            )
    return indexed


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _market_candidates(connection: sqlite3.Connection, ticker: str) -> list[dict[str, Any]]:
    columns = _table_columns(connection, "markets")
    if "ticker" not in columns:
        return []
    selected = [field for field in CONTEXT_MARKET_COLUMNS if field in columns]
    if not selected:
        return []
    row = connection.execute(
        f"SELECT {','.join(selected)} FROM markets WHERE ticker=?", (ticker,)
    ).fetchone()
    if row is None:
        return []
    return [
        _candidate(
            f"MARKET_{field.upper()}",
            f"markets:{ticker}:{field}",
            row[field],
            authority="non-authoritative",
            provenance={"ticker": ticker, "field": field, "value": row[field]},
        )
        for field in selected
        if row[field] not in (None, "")
    ]


def classify_timestamp_evidence(
    *,
    settlement_result: object,
    candidates: list[dict[str, Any]],
    source_fresh: bool,
    lineage_valid: bool,
    source_present: bool,
    existing_evaluation: bool,
) -> tuple[str, dict[str, bool], dict[str, Any] | None]:
    valid_result = binary_outcome(settlement_result) is not None
    authoritative = [item for item in candidates if item["authority"] == "authoritative"]
    ambiguous = [item for item in authoritative if item["normalized_utc_timestamp"] is None]
    usable = [item for item in authoritative if item["normalized_utc_timestamp"] is not None]
    unique_times = {item["normalized_utc_timestamp"] for item in usable}
    canonical = [item for item in usable if item["source_type"] == "CANONICAL_SETTLEMENT"]
    exchange = [item for item in usable if item["source_type"] == "EXCHANGE_SETTLEMENT_TIMESTAMP"]
    artifact = [item for item in usable if item["source_type"] == "VALIDATED_SETTLEMENT_ARTIFACT"]
    conflict = len(unique_times) > 1
    chosen = (
        canonical[0]
        if canonical
        else (exchange[0] if exchange else (artifact[0] if artifact else None))
    )
    if not source_present:
        disposition = "SOURCE_MISSING"
    elif not lineage_valid:
        disposition = "LINEAGE_FAILURE"
    elif not valid_result:
        disposition = "NOT_ACTUALLY_SETTLED"
    elif ambiguous:
        disposition = "TIMEZONE_AMBIGUOUS"
    elif conflict:
        disposition = "TIMESTAMP_CONFLICT"
    elif chosen is not None and not source_fresh:
        disposition = "SOURCE_STALE"
    elif canonical:
        disposition = "CANONICAL_SETTLED_AT_PRESENT"
    elif exchange:
        disposition = "AUTHORITATIVE_EXCHANGE_TIMESTAMP_AVAILABLE"
    elif artifact:
        disposition = "VALIDATED_ARTIFACT_TIMESTAMP_AVAILABLE"
    else:
        disposition = "RESULT_PRESENT_TIMESTAMP_MISSING"
    ready = (
        disposition
        in {
            "AUTHORITATIVE_EXCHANGE_TIMESTAMP_AVAILABLE",
            "VALIDATED_ARTIFACT_TIMESTAMP_AVAILABLE",
        }
        and len(unique_times) == 1
        and not existing_evaluation
    )
    flags = {
        "settlement_result_valid": valid_result,
        "canonical_timestamp_present": bool(canonical),
        "authoritative_candidate_present": bool(usable),
        "timestamp_conflict_present": conflict,
        "timezone_unambiguous": not ambiguous,
        "lineage_valid": lineage_valid,
        "source_fresh": source_fresh,
        "ready_for_future_canonicalization": ready,
    }
    return disposition, flags, chosen


def audit(
    source_db: Path,
    attribution_path: Path,
    plan_path: Path,
    history_dir: Path,
    *,
    now: datetime,
    freshness_seconds: int,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    if freshness_seconds < 0:
        raise ValueError("PHASE4AF_FRESHNESS_INVALID")
    now = _strict_utc(now)
    attribution = _load_artifact(attribution_path, AD_SCHEMA, "PHASE4AD")
    plan = _load_artifact(plan_path, AE_SCHEMA, "PHASE4AE")
    manifest_hash = _history_hash(history_dir)
    if attribution.get("source_phase4ac_manifest_hash") != manifest_hash:
        raise ValueError("PHASE4AF_PHASE4AC_LINEAGE_MISMATCH")
    if plan.get("source_phase4ac_manifest_hash") != manifest_hash:
        raise ValueError("PHASE4AF_PLAN_HISTORY_LINEAGE_MISMATCH")
    if plan.get("source_phase4ad_artifact_hash") != attribution["artifact_hash"]:
        raise ValueError("PHASE4AF_PHASE4AD_PHASE4AE_LINEAGE_MISMATCH")
    ad_rows = attribution["rows"]
    ae_rows = plan["rows"]
    ad_keys = [(row.get("capture_id"), row.get("ticker")) for row in ad_rows]
    ae_keys = [(row.get("capture_id"), row.get("ticker")) for row in ae_rows]
    if len(ad_keys) != len(set(ad_keys)) or len(ae_keys) != len(set(ae_keys)):
        raise ValueError("PHASE4AF_DUPLICATE_INPUT_ROW")
    if set(ad_keys) != set(ae_keys):
        raise ValueError("PHASE4AF_INPUT_ROW_LINEAGE_MISMATCH")
    ae_index = {key: row for key, row in zip(ae_keys, ae_rows, strict=True)}
    artifact_evidence = _evidence_artifacts(evidence_dir)
    connection = _ro(source_db)
    rows: list[dict[str, Any]] = []
    try:
        settlement_columns = _table_columns(connection, "settlements")
        if not {"ticker", "settled_at", "result", "raw_json", "updated_at"}.issubset(
            settlement_columns
        ):
            raise ValueError("PHASE4AF_SETTLEMENT_SCHEMA_INVALID")
        for attributed in ad_rows:
            capture_id, ticker = attributed.get("capture_id"), attributed.get("ticker")
            key = (capture_id, ticker)
            plan_row = ae_index[key]
            source = connection.execute(
                "SELECT * FROM settlements WHERE ticker=?", (ticker,)
            ).fetchone()
            source_dict = dict(source) if source is not None else {}
            candidates: list[dict[str, Any]] = []
            if source is not None:
                if source["settled_at"] not in (None, ""):
                    candidates.append(
                        _candidate(
                            "CANONICAL_SETTLEMENT",
                            f"settlements:{ticker}:settled_at",
                            source["settled_at"],
                            authority="authoritative",
                            provenance=source_dict,
                        )
                    )
                candidates.extend(_raw_exchange_candidates(source_dict))
                if source["updated_at"] not in (None, ""):
                    candidates.append(
                        _candidate(
                            "SETTLEMENT_UPDATED_AT",
                            f"settlements:{ticker}:updated_at",
                            source["updated_at"],
                            authority="non-authoritative",
                            provenance=source_dict,
                        )
                    )
                candidates.extend(_market_candidates(connection, str(ticker)))
            candidates.extend(artifact_evidence.get(str(ticker), []))
            current_hash = settlement_lineage_hash(source_dict) if source is not None else None
            lineage_valid = (
                source is not None
                and current_hash == attributed.get("settlement_hash")
                and isinstance(attributed.get("capture_lineage_hash"), str)
                and len(str(attributed.get("capture_lineage_hash"))) == 64
            )
            freshness_reference = source_dict.get("updated_at") if source is not None else None
            age, source_fresh = source_freshness(now, freshness_reference, freshness_seconds)
            existing_evaluation = plan_row.get("disposition") == "EVALUATION_APPEARED"
            known_classification = attributed.get(
                "classification"
            ) in BLOCKED_AD_CLASSIFICATIONS | {
                "READY_FOR_RECONCILIATION",
                "ALREADY_EVALUATED",
            }
            if not known_classification:
                disposition, flags, chosen = (
                    "AUDIT_BLOCKED",
                    {
                        "settlement_result_valid": False,
                        "canonical_timestamp_present": False,
                        "authoritative_candidate_present": False,
                        "timestamp_conflict_present": False,
                        "timezone_unambiguous": False,
                        "lineage_valid": False,
                        "source_fresh": False,
                        "ready_for_future_canonicalization": False,
                    },
                    None,
                )
            else:
                disposition, flags, chosen = classify_timestamp_evidence(
                    settlement_result=source_dict.get("result"),
                    candidates=candidates,
                    source_fresh=source_fresh,
                    lineage_valid=lineage_valid,
                    source_present=source is not None,
                    existing_evaluation=existing_evaluation,
                )
            row: dict[str, Any] = {
                "capture_id": capture_id,
                "ticker": ticker,
                "source_phase4ad_classification": attributed.get("classification"),
                "source_phase4ae_disposition": plan_row.get("disposition"),
                "classification": disposition,
                **flags,
                "source_age_seconds": age,
                "normalized_timestamp_candidate": (
                    chosen["normalized_utc_timestamp"] if chosen is not None else None
                ),
                "candidate_evidence_hash": chosen["evidence_hash"] if chosen is not None else None,
                "capture_lineage_hash": attributed.get("capture_lineage_hash"),
                "settlement_lineage_hash": current_hash,
                "evidence_inventory": sorted(
                    candidates,
                    key=lambda item: (
                        item["source_type"],
                        item["source_record_identity"],
                        str(item["original_timestamp"]),
                    ),
                ),
            }
            row["row_hash"] = canonical_hash(row)
            rows.append(row)
    finally:
        connection.close()
    rows.sort(key=lambda row: (str(row["ticker"]), str(row["capture_id"])))
    counts = Counter(row["classification"] for row in rows)
    ready = sum(bool(row["ready_for_future_canonicalization"]) for row in rows)
    stat = source_db.stat()
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AF",
        "generated_at": now.isoformat(),
        "source_phase4ad_path": str(attribution_path),
        "source_phase4ad_artifact_hash": attribution["artifact_hash"],
        "source_phase4ae_path": str(plan_path),
        "source_phase4ae_artifact_hash": plan["artifact_hash"],
        "source_phase4ac_manifest_hash": manifest_hash,
        "production_database_identity": {
            "path": str(source_db),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        },
        "source_mode": "ro/query_only",
        "timestamp_precedence_policy_version": POLICY_VERSION,
        "freshness_seconds": freshness_seconds,
        "input_row_count": len(rows),
        "classification_counts": dict(sorted(counts.items())),
        "ready_count": ready,
        "blocked_count": len(rows) - ready,
        "safe_for_canonicalization": bool(rows) and ready == len(rows),
        "production_database_written": False,
        "research_database_written": False,
        "services_controlled": False,
        "trading_mode_changed": False,
        "orders_created": False,
        "existing_artifacts_modified": False,
        "rows_hash": canonical_hash(rows),
        "rows": rows,
    }
    payload["artifact_hash"] = artifact_hash(payload)
    return payload


def write_atomic(path: Path, payload: dict[str, Any], *, replace: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise FileExistsError("PHASE4AF_OUTPUT_EXISTS")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase4ad-artifact", type=Path, required=True)
    parser.add_argument("--phase4ae-artifact", type=Path, required=True)
    parser.add_argument("--history-dir", type=Path, required=True)
    parser.add_argument("--production-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evaluation-time")
    parser.add_argument("--freshness-seconds", type=int, default=1800)
    parser.add_argument("--settlement-evidence-dir", type=Path)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    now = _strict_utc(args.evaluation_time) if args.evaluation_time else datetime.now(UTC)
    result = audit(
        args.production_db,
        args.phase4ad_artifact,
        args.phase4ae_artifact,
        args.history_dir,
        now=now,
        freshness_seconds=args.freshness_seconds,
        evidence_dir=args.settlement_evidence_dir,
    )
    write_atomic(args.output, result, replace=args.replace)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
