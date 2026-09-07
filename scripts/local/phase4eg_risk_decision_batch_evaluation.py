"""Evaluate synthetic candidates independently against one immutable portfolio snapshot."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4eg.batch-input.v1"
REPORT_SCHEMA = "phase4eg.batch-report.v1"


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _digest(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("PHASE4EG_DECIMAL_INVALID")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("PHASE4EG_DECIMAL_INVALID") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("PHASE4EG_DECIMAL_INVALID")
    return parsed


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "snapshot", "candidates", "artifact_hash"}:
        raise ValueError("PHASE4EG_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EG_INPUT_SCHEMA_OR_HASH_INVALID")
    snapshot = payload.get("snapshot")
    snapshot_fields = {
        "snapshot_id",
        "artifact_hash",
        "freshness_gate_hash",
        "immutable",
        "available_capital",
        "current_exposure",
        "max_exposure",
    }
    if not isinstance(snapshot, dict) or set(snapshot) != snapshot_fields:
        raise ValueError("PHASE4EG_SNAPSHOT_FIELDS_INVALID")
    if not isinstance(snapshot["snapshot_id"], str) or not snapshot["snapshot_id"]:
        raise ValueError("PHASE4EG_SNAPSHOT_ID_INVALID")
    if not _digest(snapshot["artifact_hash"]) or not _digest(snapshot["freshness_gate_hash"]):
        raise ValueError("PHASE4EG_SNAPSHOT_HASH_INVALID")
    if snapshot["immutable"] is not True:
        raise ValueError("PHASE4EG_SNAPSHOT_NOT_IMMUTABLE")
    available = _decimal(snapshot["available_capital"])
    current = _decimal(snapshot["current_exposure"])
    maximum = _decimal(snapshot["max_exposure"])
    if current > maximum:
        raise ValueError("PHASE4EG_SNAPSHOT_EXPOSURE_INVALID")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("PHASE4EG_CANDIDATES_EMPTY")
    seen = set()
    results = []
    for candidate in candidates:
        fields = {
            "candidate_id",
            "snapshot_artifact_hash",
            "requested_notional",
            "candidate_cap",
            "hard_blocked",
        }
        if not isinstance(candidate, dict) or set(candidate) != fields:
            raise ValueError("PHASE4EG_CANDIDATE_FIELDS_INVALID")
        identifier = candidate["candidate_id"]
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("PHASE4EG_CANDIDATE_ID_INVALID")
        seen.add(identifier)
        if candidate["snapshot_artifact_hash"] != snapshot["artifact_hash"]:
            raise ValueError("PHASE4EG_CANDIDATE_SNAPSHOT_MISMATCH")
        requested = _decimal(candidate["requested_notional"])
        cap = _decimal(candidate["candidate_cap"])
        if not isinstance(candidate["hard_blocked"], bool):
            raise ValueError("PHASE4EG_HARD_BLOCK_INVALID")
        reasons = []
        if candidate["hard_blocked"]:
            reasons.append("HARD_BLOCK")
        if requested > cap:
            reasons.append("CANDIDATE_CAP_EXCEEDED")
        if requested > available:
            reasons.append("AVAILABLE_CAPITAL_EXCEEDED")
        if current + requested > maximum:
            reasons.append("PORTFOLIO_EXPOSURE_EXCEEDED")
        results.append(
            {
                "candidate_id": identifier,
                "snapshot_artifact_hash": snapshot["artifact_hash"],
                "status": "ELIGIBLE" if not reasons else "INELIGIBLE",
                "requested_notional": candidate["requested_notional"],
                "reasons": reasons,
                "available_capital_before": snapshot["available_capital"],
                "available_capital_after": snapshot["available_capital"],
                "current_exposure_before": snapshot["current_exposure"],
                "current_exposure_after": snapshot["current_exposure"],
            }
        )
    results.sort(key=lambda row: row["candidate_id"])
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EG",
        "input_hash": payload["artifact_hash"],
        "snapshot": snapshot,
        "results": results,
        "candidate_count": len(results),
        "eligible_count": sum(row["status"] == "ELIGIBLE" for row in results),
        "snapshot_mutations": 0,
        "capital_reserved": False,
        "risk_decisions_created": 0,
        "execution_authorized": False,
        "production_records_created": 0,
    }
    report["artifact_hash"] = _hash(report)
    return report


def publish(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.input.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
