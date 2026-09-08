"""Differentially replay supplied legacy and optimized forecast pipeline results."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dx.replay-input.v1"
REPORT_SCHEMA = "phase4dx.replay-report.v1"
REQUIRED_CATEGORIES = {"NORMAL", "BOUNDARY", "TIE", "STALE", "REFUSAL"}
MAX_FIXTURES = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _work(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("PHASE4DX_WORK_UNITS_INVALID")
    return value


def _validate_output(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"status", "results", "reasons"}:
        raise ValueError("PHASE4DX_OUTPUT_FIELDS_INVALID")
    if value["status"] not in {"SUCCESS", "REFUSED"}:
        raise ValueError("PHASE4DX_OUTPUT_STATUS_INVALID")
    if not isinstance(value["results"], list) or not isinstance(value["reasons"], list):
        raise ValueError("PHASE4DX_OUTPUT_COLLECTION_INVALID")
    if any(not isinstance(reason, str) or not reason for reason in value["reasons"]):
        raise ValueError("PHASE4DX_OUTPUT_REASON_INVALID")
    if value["status"] == "SUCCESS" and value["reasons"]:
        raise ValueError("PHASE4DX_SUCCESS_REASONS_INVALID")
    if value["status"] == "REFUSED" and (value["results"] or not value["reasons"]):
        raise ValueError("PHASE4DX_REFUSAL_OUTPUT_INVALID")


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "fixtures", "artifact_hash"}:
        raise ValueError("PHASE4DX_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DX_INPUT_SCHEMA_OR_HASH_INVALID")
    fixtures = payload["fixtures"]
    if not isinstance(fixtures, list) or not fixtures or len(fixtures) > MAX_FIXTURES:
        raise ValueError("PHASE4DX_FIXTURE_COUNT_INVALID")
    fields = {
        "fixture_id",
        "category",
        "input_hash",
        "legacy_output",
        "optimized_output",
        "legacy_work_units",
        "optimized_work_units",
    }
    identifiers: set[str] = set()
    categories: set[str] = set()
    results = []
    legacy_work = 0
    optimized_work = 0
    for fixture in fixtures:
        if not isinstance(fixture, dict) or set(fixture) != fields:
            raise ValueError("PHASE4DX_FIXTURE_FIELDS_INVALID")
        identifier = fixture["fixture_id"]
        category = fixture["category"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4DX_FIXTURE_ID_INVALID")
        if category not in REQUIRED_CATEGORIES:
            raise ValueError("PHASE4DX_CATEGORY_INVALID")
        identifiers.add(identifier)
        categories.add(category)
        if not isinstance(fixture["input_hash"], str) or len(fixture["input_hash"]) != 64:
            raise ValueError("PHASE4DX_INPUT_HASH_INVALID")
        try:
            int(fixture["input_hash"], 16)
        except ValueError as exc:
            raise ValueError("PHASE4DX_INPUT_HASH_INVALID") from exc
        _validate_output(fixture["legacy_output"])
        _validate_output(fixture["optimized_output"])
        before = _work(fixture["legacy_work_units"])
        after = _work(fixture["optimized_work_units"])
        legacy_work += before
        optimized_work += after
        legacy_hash = canonical_hash(fixture["legacy_output"])
        optimized_hash = canonical_hash(fixture["optimized_output"])
        equivalent = fixture["legacy_output"] == fixture["optimized_output"]
        results.append(
            {
                "fixture_id": identifier,
                "category": category,
                "legacy_output_hash": legacy_hash,
                "optimized_output_hash": optimized_hash,
                "logically_equivalent": equivalent,
                "work_units_saved": before - after,
            }
        )
    if categories != REQUIRED_CATEGORIES:
        raise ValueError("PHASE4DX_REPRESENTATIVE_CORPUS_INCOMPLETE")
    mismatches = sorted(row["fixture_id"] for row in results if not row["logically_equivalent"])
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DX",
        "input_hash": payload["artifact_hash"],
        "status": "PASS" if not mismatches else "FAIL",
        "fixture_results": sorted(results, key=lambda row: row["fixture_id"]),
        "covered_categories": sorted(categories),
        "mismatched_fixture_ids": mismatches,
        "exact_logical_equivalence": not mismatches,
        "legacy_work_units": legacy_work,
        "optimized_work_units": optimized_work,
        "work_units_saved": legacy_work - optimized_work,
        "wall_clock_used_as_gate": False,
        "forecast_records_created": 0,
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
