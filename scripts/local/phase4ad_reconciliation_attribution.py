"""Strictly read-only Phase 4AD prospective reconciliation attribution."""

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

from kalshi_predictor.ingest.settlement_hints import (
    ALLOWED_HINT_FIELDS,
)
from kalshi_predictor.ingest.settlement_hints import (
    SCHEMA as HINT_SCHEMA,
)
from kalshi_predictor.ingest.settlement_hints import artifact_hash as hint_artifact_hash
from kalshi_predictor.phase4cd.reconciliation_audit import (
    binary_outcome,
    canonical_hash,
    capture_lineage_hash,
    executable_evidence_reason,
    settlement_lineage_hash,
    valid_hash,
    valid_json_container,
    valid_probability,
)

SCHEMA = "phase4ad.reconciliation-attribution.v1"


def _utc(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=1")
    return connection


def artifact_hash(payload: dict[str, Any]) -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != "artifact_hash"})


def _load_phase4ac(history_dir: Path) -> tuple[str, dict[str, Any]]:
    path = Path(__file__).with_name("phase4ac_artifact_history.py")
    spec = importlib.util.spec_from_file_location("phase4ac_artifact_history_for_4ad", path)
    if not spec or not spec.loader:
        raise ValueError("PHASE4AD_PHASE4AC_LOADER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reconstructed = module.reconstruct_phase4ab_history(history_dir)
    gates = reconstructed["phase4aa_gate_payloads"]
    if not gates:
        raise ValueError("PHASE4AD_PHASE4AA_HISTORY_EMPTY")
    return str(reconstructed["manifest_hash"]), gates[-1]


def _load_hints(path: Path) -> tuple[str, list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != HINT_SCHEMA:
        raise ValueError("PHASE4AD_HINT_SCHEMA_INVALID")
    if payload.get("artifact_hash") != hint_artifact_hash(payload):
        raise ValueError("PHASE4AD_HINT_HASH_MISMATCH")
    hints = payload.get("hints")
    if not isinstance(hints, list):
        raise ValueError("PHASE4AD_HINT_ROWS_INVALID")
    tickers: list[str] = []
    for hint in hints:
        if not isinstance(hint, dict) or set(hint) != ALLOWED_HINT_FIELDS:
            raise ValueError("PHASE4AD_HINT_FIELDS_INVALID")
        tickers.append(str(hint["ticker"]))
    if len(tickers) != len(set(tickers)):
        raise ValueError("PHASE4AD_HINT_DUPLICATE_TICKER")
    return str(payload["artifact_hash"]), tickers


def _capture_lineage_reasons(capture: sqlite3.Row) -> list[str]:
    reasons: list[str] = []
    required = ("capture_id", "ticker", "event_ticker", "snapshot_id", "snapshot_timestamp")
    if any(capture[field] in (None, "") for field in required):
        reasons.append("REQUIRED_CAPTURE_FIELD_MISSING")
    for field in ("snapshot_hash", "bundle_hash"):
        if not valid_hash(capture[field]):
            reasons.append(f"{field.upper()}_INVALID")
    for field in (
        "feature_ids_json",
        "feature_hashes_json",
        "source_observations_json",
        "model_versions_json",
    ):
        if not valid_json_container(capture[field]):
            reasons.append(f"{field.upper()}_INVALID")
    return reasons


def _classify(
    capture: sqlite3.Row,
    settlement: sqlite3.Row,
    evaluation: sqlite3.Row | None,
    *,
    stale: bool,
) -> tuple[str, list[str], str]:
    current_hash = settlement_lineage_hash(dict(settlement))
    if evaluation is not None:
        if evaluation["settlement_hash"] not in (None, current_hash):
            return "SETTLEMENT_LINEAGE_CONFLICT", ["SETTLEMENT_HASH_CHANGED"], current_hash
        return "ALREADY_EVALUATED", ["EVALUATION_APPEARED_AFTER_GATE"], current_hash
    if stale:
        return "SOURCE_ARTIFACT_STALE", ["MAXIMUM_AGE_THRESHOLD_REACHED"], current_hash
    if binary_outcome(settlement["result"]) is None:
        return "SETTLEMENT_RESULT_UNUSABLE", ["RESULT_NOT_BINARY_YES_NO"], current_hash
    if settlement["settled_at"] in (None, ""):
        return "SETTLEMENT_RESULT_UNUSABLE", ["SETTLED_AT_MISSING"], current_hash
    try:
        if _utc(settlement["settled_at"]) <= _utc(capture["snapshot_timestamp"]):
            return "SETTLEMENT_LINEAGE_CONFLICT", ["SETTLEMENT_NOT_AFTER_CAPTURE"], current_hash
    except (TypeError, ValueError):
        return "CAPTURE_LINEAGE_INCOMPLETE", ["CAPTURE_TIMESTAMP_INVALID"], current_hash
    lineage_reasons = _capture_lineage_reasons(capture)
    if lineage_reasons:
        return "CAPTURE_LINEAGE_INCOMPLETE", lineage_reasons[:6], current_hash
    if not valid_probability(capture["market_probability"]):
        return "PROBABILITY_INPUT_INVALID", ["MARKET_PROBABILITY_INVALID"], current_hash
    if not valid_probability(capture["crypto_probability"]):
        return "PROBABILITY_INPUT_INVALID", ["MODEL_PROBABILITY_INVALID"], current_hash
    evidence_reason = executable_evidence_reason(dict(capture))
    if evidence_reason:
        return "EXECUTABLE_EVIDENCE_INCOMPLETE", [evidence_reason], current_hash
    return "READY_FOR_RECONCILIATION", ["PHASE4CD_INPUTS_VALID"], current_hash


def audit(
    source_db: Path,
    research_db: Path,
    hint_path: Path,
    history_dir: Path,
    *,
    now: datetime,
    maximum_age_seconds: int,
) -> dict[str, Any]:
    if maximum_age_seconds < 0:
        raise ValueError("PHASE4AD_MAXIMUM_AGE_INVALID")
    manifest_hash, gate = _load_phase4ac(history_dir)
    hint_hash, hinted_tickers = _load_hints(hint_path)
    if gate.get("source_hint_artifact_hash") != hint_hash:
        raise ValueError("PHASE4AD_GATE_HINT_LINEAGE_MISMATCH")
    age = (now.astimezone(UTC) - _utc(gate["generated_at"])).total_seconds()
    stale = age < 0 or age >= maximum_age_seconds
    source, research = _ro(source_db), _ro(research_db)
    rows: list[dict[str, Any]] = []
    placeholders = ",".join("?" for _ in hinted_tickers)
    captures = []
    if hinted_tickers:
        captures = research.execute(
            f"""
            SELECT * FROM prospective_paired_captures
            WHERE ticker IN ({placeholders}) ORDER BY ticker,snapshot_timestamp,capture_id
            """,
            hinted_tickers,
        ).fetchall()
    for capture in captures:
        settlement = source.execute(
            "SELECT * FROM settlements WHERE ticker=?", (capture["ticker"],)
        ).fetchone()
        if settlement is None:
            continue  # Phase 4AD only attributes canonical settlements.
        evaluation = research.execute(
            "SELECT * FROM prospective_pair_evaluations WHERE capture_id=?",
            (capture["capture_id"],),
        ).fetchone()
        try:
            classification, reasons, lineage_hash = _classify(
                capture, settlement, evaluation, stale=stale
            )
        except Exception as error:  # fail closed per-row with bounded diagnostics
            classification, reasons = "UNKNOWN_BLOCKER", [type(error).__name__]
            lineage_hash = settlement_lineage_hash(dict(settlement))
        rows.append(
            {
                "capture_id": capture["capture_id"],
                "ticker": capture["ticker"],
                "event_ticker": capture["event_ticker"],
                "settlement_hash": lineage_hash,
                "capture_lineage_hash": capture_lineage_hash(dict(capture)),
                "classification": classification,
                "reason_codes": reasons[:6],
            }
        )
    source.close()
    research.close()
    counts = Counter(row["classification"] for row in rows)
    non_evaluated = len(rows) - counts["ALREADY_EVALUATED"]
    ready = counts["READY_FOR_RECONCILIATION"]
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": now.astimezone(UTC).isoformat(),
        "source_mode": "ro/query_only",
        "research_mode": "ro/query_only",
        "source_phase4ac_manifest_hash": manifest_hash,
        "source_phase4aa_artifact_hash": gate["artifact_hash"],
        "source_hint_artifact_hash": hint_hash,
        "source_evidence_age_seconds": age,
        "maximum_age_seconds": maximum_age_seconds,
        "capture_count": len(rows),
        "ticker_count": len({row["ticker"] for row in rows}),
        "event_count": len({row["event_ticker"] for row in rows}),
        "classification_counts": dict(sorted(counts.items())),
        "ready_count": ready,
        "blocked_count": non_evaluated - ready,
        "already_evaluated_count": counts["ALREADY_EVALUATED"],
        "safe_to_reconcile": bool(non_evaluated) and ready == non_evaluated,
        "production_database_written": False,
        "trading_mode_changed": False,
        "rows_hash": canonical_hash(rows),
        "rows": rows,
    }
    payload["artifact_hash"] = artifact_hash(payload)
    return payload


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--research-db", type=Path, required=True)
    parser.add_argument("--hint-artifact", type=Path, required=True)
    parser.add_argument("--history-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-age-seconds", type=int, default=1800)
    args = parser.parse_args()
    result = audit(
        args.source_db,
        args.research_db,
        args.hint_artifact,
        args.history_dir,
        now=datetime.now(UTC),
        maximum_age_seconds=args.maximum_age_seconds,
    )
    write_atomic(args.output, result)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
