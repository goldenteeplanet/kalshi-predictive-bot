"""Replay paired synthetic legacy and optimized risk/paper decisions for exact equivalence."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4eu.replay-input.v1"
REPORT_SCHEMA = "phase4eu.replay-report.v1"
DECISION_FIELDS = {"eligible", "quantity", "caps", "reason_codes"}
CAP_FIELDS = {"candidate_max_contracts", "position_limit_contracts", "loss_limit_micros"}
COMPARE_FIELDS = ("eligible", "quantity", "caps", "reason_codes")


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(code)
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(code) from exc
    return value


def _text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(code)
    return value


def _integer(value: Any, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(code)
    return value


def _decision(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != DECISION_FIELDS:
        raise ValueError(f"PHASE4EU_{path}_FIELDS_INVALID")
    eligible = value["eligible"]
    if not isinstance(eligible, bool):
        raise ValueError(f"PHASE4EU_{path}_ELIGIBILITY_INVALID")
    quantity = _integer(value["quantity"], 0, 1_000_000, f"PHASE4EU_{path}_QUANTITY_INVALID")
    if (eligible and quantity <= 0) or (not eligible and quantity != 0):
        raise ValueError(f"PHASE4EU_{path}_ELIGIBILITY_QUANTITY_INCONSISTENT")
    caps = value["caps"]
    if not isinstance(caps, dict) or set(caps) != CAP_FIELDS:
        raise ValueError(f"PHASE4EU_{path}_CAPS_FIELDS_INVALID")
    normalized_caps = {
        "candidate_max_contracts": _integer(
            caps["candidate_max_contracts"], 0, 1_000_000, f"PHASE4EU_{path}_CANDIDATE_CAP_INVALID"
        ),
        "position_limit_contracts": _integer(
            caps["position_limit_contracts"], 0, 1_000_000, f"PHASE4EU_{path}_POSITION_CAP_INVALID"
        ),
        "loss_limit_micros": _integer(
            caps["loss_limit_micros"], 0, 10**15, f"PHASE4EU_{path}_LOSS_CAP_INVALID"
        ),
    }
    reasons = value["reason_codes"]
    if (
        not isinstance(reasons, list)
        or len(reasons) != len(set(reasons))
        or any(
            not isinstance(reason, str) or not reason or reason.strip() != reason
            for reason in reasons
        )
    ):
        raise ValueError(f"PHASE4EU_{path}_REASONS_INVALID")
    return {
        "eligible": eligible,
        "quantity": quantity,
        "caps": normalized_caps,
        "reason_codes": sorted(reasons),
    }


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "source_artifact_hash",
        "cases",
        "artifact_hash",
    }:
        raise ValueError("PHASE4EU_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EU_INPUT_SCHEMA_OR_HASH_INVALID")
    source_hash = _digest(payload["source_artifact_hash"], "PHASE4EU_SOURCE_HASH_INVALID")
    cases = payload["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("PHASE4EU_CASES_INVALID")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"case_id", "legacy", "optimized"}:
            raise ValueError("PHASE4EU_CASE_FIELDS_INVALID")
        case_id = _text(case["case_id"], "PHASE4EU_CASE_ID_INVALID")
        if case_id in seen:
            raise ValueError("PHASE4EU_CASE_DUPLICATE")
        seen.add(case_id)
        legacy = _decision(case["legacy"], "LEGACY")
        optimized = _decision(case["optimized"], "OPTIMIZED")
        differences = [field for field in COMPARE_FIELDS if legacy[field] != optimized[field]]
        equivalent = not differences
        results.append(
            {
                "case_id": case_id,
                "legacy": legacy,
                "optimized": optimized,
                "equivalent": equivalent,
                "difference_fields": differences,
                "reason_codes": []
                if equivalent
                else [f"DIFFERENTIAL_{field.upper()}_MISMATCH" for field in differences],
            }
        )
    results.sort(key=lambda item: item["case_id"])
    mismatch_reasons = [
        f"CASE_{result['case_id']}_{reason}"
        for result in results
        for reason in result["reason_codes"]
    ]
    all_equivalent = not mismatch_reasons
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EU",
        "input_hash": payload["artifact_hash"],
        "source_artifact_hash": source_hash,
        "compared_fields": list(COMPARE_FIELDS),
        "case_results": results,
        "case_count": len(results),
        "equivalent_count": sum(result["equivalent"] for result in results),
        "all_equivalent": all_equivalent,
        "gate_reason_codes": mismatch_reasons,
        "paper_eligibility_equivalence_certified": all_equivalent,
        "paper_eligibility_authorized": False,
        "paper_order_creation_authorized": False,
        "paper_orders_created": 0,
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
