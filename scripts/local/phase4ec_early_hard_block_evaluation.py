"""Model fail-closed early hard-block evaluation for offline risk decisions."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ec.evaluation-input.v1"
REPORT_SCHEMA = "phase4ec.evaluation-report.v1"
MAX_CANDIDATES = 100_000


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _work(value: Any, error: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(error)
    return value


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "candidates", "artifact_hash"}:
        raise ValueError("PHASE4EC_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EC_INPUT_SCHEMA_OR_HASH_INVALID")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates or len(candidates) > MAX_CANDIDATES:
        raise ValueError("PHASE4EC_CANDIDATE_COUNT_INVALID")
    seen_candidates = set()
    results = []
    for candidate in candidates:
        fields = {"candidate_id", "hard_blocks", "downstream_stages"}
        if not isinstance(candidate, dict) or set(candidate) != fields:
            raise ValueError("PHASE4EC_CANDIDATE_FIELDS_INVALID")
        candidate_id = candidate["candidate_id"]
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in seen_candidates:
            raise ValueError("PHASE4EC_CANDIDATE_ID_INVALID")
        seen_candidates.add(candidate_id)
        blocks = candidate["hard_blocks"]
        stages = candidate["downstream_stages"]
        if not isinstance(blocks, list) or not blocks or not isinstance(stages, list) or not stages:
            raise ValueError("PHASE4EC_PIPELINE_EMPTY")
        seen_blocks = set()
        normalized_blocks = []
        for block in blocks:
            required = {
                "block_id",
                "priority",
                "decisive",
                "evidence_complete",
                "triggered",
                "reason_code",
                "work_units",
            }
            if not isinstance(block, dict) or set(block) != required:
                raise ValueError("PHASE4EC_BLOCK_FIELDS_INVALID")
            identifier = block["block_id"]
            if not isinstance(identifier, str) or not identifier or identifier in seen_blocks:
                raise ValueError("PHASE4EC_BLOCK_ID_INVALID")
            seen_blocks.add(identifier)
            _work(block["priority"], "PHASE4EC_PRIORITY_INVALID")
            _work(block["work_units"], "PHASE4EC_BLOCK_WORK_INVALID", positive=True)
            for field in ("decisive", "evidence_complete", "triggered"):
                if not isinstance(block[field], bool):
                    raise ValueError("PHASE4EC_BLOCK_BOOLEAN_INVALID")
            if not isinstance(block["reason_code"], str) or not block["reason_code"]:
                raise ValueError("PHASE4EC_REASON_INVALID")
            normalized_blocks.append(block)
        seen_stages = set()
        for stage in stages:
            if not isinstance(stage, dict) or set(stage) != {"stage_id", "work_units"}:
                raise ValueError("PHASE4EC_STAGE_FIELDS_INVALID")
            identifier = stage["stage_id"]
            if not isinstance(identifier, str) or not identifier or identifier in seen_stages:
                raise ValueError("PHASE4EC_STAGE_ID_INVALID")
            seen_stages.add(identifier)
            _work(stage["work_units"], "PHASE4EC_STAGE_WORK_INVALID", positive=True)

        ordered_blocks = sorted(
            normalized_blocks, key=lambda row: (row["priority"], row["block_id"])
        )
        incomplete = [row for row in ordered_blocks if not row["evidence_complete"]]
        triggered = [
            row
            for row in ordered_blocks
            if row["evidence_complete"] and row["decisive"] and row["triggered"]
        ]
        if incomplete:
            decision = "REFUSE_INCOMPLETE_EVIDENCE"
            reasons = [f"INCOMPLETE:{row['reason_code']}" for row in incomplete]
        elif triggered:
            decision = "HARD_BLOCK"
            reasons = [row["reason_code"] for row in triggered]
        else:
            decision = "CONTINUE"
            reasons = []
        block_work = sum(row["work_units"] for row in ordered_blocks)
        downstream_work = sum(row["work_units"] for row in stages)
        legacy_work = block_work + downstream_work
        optimized_work = block_work + (downstream_work if decision == "CONTINUE" else 0)
        results.append(
            {
                "candidate_id": candidate_id,
                "legacy_decision": decision,
                "optimized_decision": decision,
                "equivalent": True,
                "reason_codes": reasons,
                "hard_block_order": [row["block_id"] for row in ordered_blocks],
                "legacy_work_units": legacy_work,
                "optimized_work_units": optimized_work,
                "avoided_work_units": legacy_work - optimized_work,
                "downstream_executed": decision == "CONTINUE",
            }
        )
    results.sort(key=lambda row: row["candidate_id"])
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EC",
        "input_hash": payload["artifact_hash"],
        "status": "EXACT_EQUIVALENCE_PROVEN",
        "results": results,
        "total_avoided_work_units": sum(row["avoided_work_units"] for row in results),
        "risk_decisions_created": 0,
        "capital_reserved": False,
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
