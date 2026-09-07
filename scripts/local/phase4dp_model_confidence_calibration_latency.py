"""Audit calibration work units and safe no-lookahead reuse boundaries offline."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dp.calibration-input.v1"
REPORT_SCHEMA = "phase4dp.calibration-report.v1"
MAX_REQUESTS = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("PHASE4DP_HASH_INVALID")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("PHASE4DP_HASH_INVALID") from exc
    return value


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4DP_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4DP_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("PHASE4DP_TIMESTAMP_INVALID")
    return parsed


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "requests", "artifact_hash"}:
        raise ValueError("PHASE4DP_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DP_INPUT_SCHEMA_OR_HASH_INVALID")
    requests = payload["requests"]
    if not isinstance(requests, list) or not requests or len(requests) > MAX_REQUESTS:
        raise ValueError("PHASE4DP_REQUEST_COUNT_INVALID")
    fields = {
        "request_id",
        "decision_at",
        "model_hash",
        "calibration_version",
        "segment",
        "training_data_hash",
        "trained_through",
        "valid_until",
        "calibration_hash",
        "work_units",
    }
    identifiers: set[str] = set()
    groups: dict[str, list[dict[str, Any]]] = {}
    decisions = []
    baseline_work = 0
    for request in requests:
        if not isinstance(request, dict) or set(request) != fields:
            raise ValueError("PHASE4DP_REQUEST_FIELDS_INVALID")
        identifier = request["request_id"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4DP_REQUEST_ID_INVALID")
        identifiers.add(identifier)
        for field in ("model_hash", "training_data_hash", "calibration_hash"):
            _digest(request[field])
        if any(
            not isinstance(request[field], str) or not request[field]
            for field in ("calibration_version", "segment")
        ):
            raise ValueError("PHASE4DP_CALIBRATION_IDENTITY_INVALID")
        decision_at = _time(request["decision_at"])
        trained_through = _time(request["trained_through"])
        valid_until = _time(request["valid_until"])
        work = request["work_units"]
        if not isinstance(work, int) or isinstance(work, bool) or work < 1:
            raise ValueError("PHASE4DP_WORK_UNITS_INVALID")
        baseline_work += work
        reasons = []
        if trained_through > decision_at:
            reasons.append("LOOKAHEAD_TRAINING_DATA")
        if decision_at > valid_until:
            reasons.append("CALIBRATION_EXPIRED")
        identity = {
            "model_hash": request["model_hash"],
            "calibration_version": request["calibration_version"],
            "segment": request["segment"],
            "training_data_hash": request["training_data_hash"],
            "trained_through": request["trained_through"],
            "valid_until": request["valid_until"],
            "calibration_hash": request["calibration_hash"],
        }
        key = canonical_hash(identity)
        decision = {
            "request_id": identifier,
            "reuse_key": key,
            "eligible_for_reuse": not reasons,
            "reasons": reasons,
            "work_units": work,
        }
        decisions.append(decision)
        if not reasons:
            groups.setdefault(key, []).append(decision)
    reused_work = 0
    reuse_groups = []
    for key in sorted(groups):
        group = groups[key]
        if len(group) < 2:
            continue
        canonical_work = group[0]["work_units"]
        if any(item["work_units"] != canonical_work for item in group):
            raise ValueError("PHASE4DP_REUSE_COST_AMBIGUOUS")
        saved = canonical_work * (len(group) - 1)
        reused_work += saved
        reuse_groups.append(
            {
                "reuse_key": key,
                "request_ids": sorted(item["request_id"] for item in group),
                "work_units_saved": saved,
            }
        )
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DP",
        "input_hash": payload["artifact_hash"],
        "request_decisions": decisions,
        "reuse_groups": reuse_groups,
        "baseline_work_units": baseline_work,
        "reusable_work_units_saved": reused_work,
        "optimized_work_units": baseline_work - reused_work,
        "wall_clock_used_as_gate": False,
        "no_lookahead_preserved": all(
            "LOOKAHEAD_TRAINING_DATA" not in decision["reasons"]
            or not decision["eligible_for_reuse"]
            for decision in decisions
        ),
        "calibration_records_created": 0,
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
