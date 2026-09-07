"""Publish deterministic per-stage and end-to-end paper-eligibility latency SLO evidence."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4et.slo-input.v1"
REPORT_SCHEMA = "phase4et.slo-report.v1"
STAGES = (
    "market_data",
    "features",
    "forecast",
    "ranking",
    "risk",
    "operator_handoff",
)


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


def _integer(value: Any, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(code)
    return value


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "run_id",
        "source_artifact_hash",
        "stages",
        "end_to_end_objective_us",
        "artifact_hash",
    }:
        raise ValueError("PHASE4ET_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4ET_INPUT_SCHEMA_OR_HASH_INVALID")
    run_id = payload["run_id"]
    if not isinstance(run_id, str) or not run_id or run_id.strip() != run_id:
        raise ValueError("PHASE4ET_RUN_ID_INVALID")
    source_hash = _digest(payload["source_artifact_hash"], "PHASE4ET_SOURCE_HASH_INVALID")
    end_objective = _integer(
        payload["end_to_end_objective_us"], 1, 10**12, "PHASE4ET_END_OBJECTIVE_INVALID"
    )
    stages = payload["stages"]
    if not isinstance(stages, list) or len(stages) != len(STAGES):
        raise ValueError("PHASE4ET_STAGE_SET_INVALID")

    by_name: dict[str, dict[str, int]] = {}
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) != {"name", "elapsed_us", "objective_us"}:
            raise ValueError("PHASE4ET_STAGE_FIELDS_INVALID")
        name = stage["name"]
        if name not in STAGES or name in by_name:
            raise ValueError("PHASE4ET_STAGE_NAME_INVALID")
        by_name[name] = {
            "elapsed_us": _integer(
                stage["elapsed_us"], 0, 10**12, "PHASE4ET_STAGE_ELAPSED_INVALID"
            ),
            "objective_us": _integer(
                stage["objective_us"], 1, 10**12, "PHASE4ET_STAGE_OBJECTIVE_INVALID"
            ),
        }
    if set(by_name) != set(STAGES):
        raise ValueError("PHASE4ET_STAGE_SET_INVALID")

    stage_results: list[dict[str, Any]] = []
    breach_reasons: list[str] = []
    observed_total = 0
    objective_total = 0
    for name in STAGES:
        elapsed = by_name[name]["elapsed_us"]
        objective = by_name[name]["objective_us"]
        passed = elapsed <= objective
        reason_codes = [] if passed else [f"STAGE_{name.upper()}_SLO_EXCEEDED"]
        breach_reasons.extend(reason_codes)
        observed_total += elapsed
        objective_total += objective
        stage_results.append(
            {
                "name": name,
                "elapsed_us": elapsed,
                "objective_us": objective,
                "headroom_us": objective - elapsed,
                "passed": passed,
                "reason_codes": reason_codes,
            }
        )

    end_passed = observed_total <= end_objective
    end_reasons = [] if end_passed else ["END_TO_END_SLO_EXCEEDED"]
    breach_reasons.extend(end_reasons)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4ET",
        "input_hash": payload["artifact_hash"],
        "run_id": run_id,
        "source_artifact_hash": source_hash,
        "unit": "microseconds",
        "stage_order": list(STAGES),
        "stage_results": stage_results,
        "stage_objective_total_us": objective_total,
        "end_to_end": {
            "elapsed_us": observed_total,
            "objective_us": end_objective,
            "headroom_us": end_objective - observed_total,
            "passed": end_passed,
            "reason_codes": end_reasons,
        },
        "all_slos_passed": not breach_reasons,
        "breach_reason_codes": breach_reasons,
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
