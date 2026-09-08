"""Select near-money candidates with deterministic starvation protection offline."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4do.selection-input.v1"
REPORT_SCHEMA = "phase4do.selection-report.v1"
MAX_CANDIDATES = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("PHASE4DO_DECIMAL_INVALID")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("PHASE4DO_DECIMAL_INVALID") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("PHASE4DO_DECIMAL_INVALID")
    return parsed


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "current_cycle",
        "near_money_threshold",
        "starvation_cycles",
        "selection_limit",
        "candidates",
        "artifact_hash",
    }
    if set(payload) != required:
        raise ValueError("PHASE4DO_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DO_INPUT_SCHEMA_OR_HASH_INVALID")
    current = payload["current_cycle"]
    starvation = payload["starvation_cycles"]
    limit = payload["selection_limit"]
    if (
        any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in (current, starvation, limit)
        )
        or current < 0
        or starvation < 1
        or limit < 1
    ):
        raise ValueError("PHASE4DO_POLICY_INVALID")
    threshold = _decimal(payload["near_money_threshold"])
    candidates = payload["candidates"]
    if not isinstance(candidates, list) or not candidates or len(candidates) > MAX_CANDIDATES:
        raise ValueError("PHASE4DO_CANDIDATE_COUNT_INVALID")
    if limit > len(candidates):
        raise ValueError("PHASE4DO_SELECTION_LIMIT_INVALID")
    fields = {"candidate_id", "distance_to_money", "last_selected_cycle"}
    identifiers: set[str] = set()
    rows = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != fields:
            raise ValueError("PHASE4DO_CANDIDATE_FIELDS_INVALID")
        identifier = candidate["candidate_id"]
        last = candidate["last_selected_cycle"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4DO_CANDIDATE_ID_INVALID")
        if not isinstance(last, int) or isinstance(last, bool) or not 0 <= last <= current:
            raise ValueError("PHASE4DO_LAST_SELECTED_CYCLE_INVALID")
        identifiers.add(identifier)
        distance = _decimal(candidate["distance_to_money"])
        wait = current - last
        rows.append(
            {
                "candidate_id": identifier,
                "distance": distance,
                "wait_cycles": wait,
                "near_money": distance <= threshold,
                "starvation_due": wait >= starvation,
            }
        )
    mandatory = [row for row in rows if row["near_money"] or row["starvation_due"]]
    if len(mandatory) > limit:
        raise ValueError("PHASE4DO_MANDATORY_COVERAGE_EXCEEDS_CAPACITY")
    selected_ids = {row["candidate_id"] for row in mandatory}
    remaining = sorted(
        (row for row in rows if row["candidate_id"] not in selected_ids),
        key=lambda row: (row["distance"], -row["wait_cycles"], row["candidate_id"]),
    )
    for row in remaining[: limit - len(mandatory)]:
        selected_ids.add(row["candidate_id"])
    selected = sorted(
        (row for row in rows if row["candidate_id"] in selected_ids),
        key=lambda row: (
            not row["starvation_due"],
            not row["near_money"],
            row["distance"],
            -row["wait_cycles"],
            row["candidate_id"],
        ),
    )
    missing_near = sorted(
        row["candidate_id"]
        for row in rows
        if row["near_money"] and row["candidate_id"] not in selected_ids
    )
    missing_starved = sorted(
        row["candidate_id"]
        for row in rows
        if row["starvation_due"] and row["candidate_id"] not in selected_ids
    )
    if missing_near or missing_starved:
        raise ValueError("PHASE4DO_COVERAGE_INVARIANT_FAILED")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DO",
        "input_hash": payload["artifact_hash"],
        "selected": [
            {
                "selection_rank": index,
                "candidate_id": row["candidate_id"],
                "distance_to_money": format(row["distance"], "f"),
                "wait_cycles": row["wait_cycles"],
                "near_money": row["near_money"],
                "starvation_due": row["starvation_due"],
            }
            for index, row in enumerate(selected, start=1)
        ],
        "deferred_candidate_ids": sorted(identifiers - selected_ids),
        "near_money_coverage_complete": True,
        "starvation_coverage_complete": True,
        "selection_records_created": 0,
        "execution_authorized": False,
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
