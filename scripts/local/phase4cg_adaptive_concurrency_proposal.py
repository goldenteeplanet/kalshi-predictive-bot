"""Produce a non-executing bounded concurrency proposal from captured evidence."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cg.concurrency-evidence.v1"
REPORT_SCHEMA = "phase4cg.adaptive-concurrency-proposal.v1"
MIN_SAMPLES = 100
MAX_CONCURRENCY = 32
THROTTLE_DECREASE_PPM = 10_000
TIMEOUT_DECREASE_PPM = 20_000
LOW_ERROR_PPM = 1_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _nonnegative_int(value: Any, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(reason)
    return value


def build_proposal(evidence: dict[str, Any]) -> dict[str, Any]:
    if evidence.get("schema") != INPUT_SCHEMA or evidence.get("artifact_hash") != _hash(evidence):
        raise ValueError("PHASE4CG_INPUT_SCHEMA_OR_HASH_INVALID")
    current = _nonnegative_int(evidence.get("current_concurrency"), "PHASE4CG_CURRENT_INVALID")
    if not 1 <= current <= MAX_CONCURRENCY:
        raise ValueError("PHASE4CG_CURRENT_BOUND_INVALID")
    target = _nonnegative_int(evidence.get("target_p95_ms"), "PHASE4CG_TARGET_INVALID")
    windows = evidence.get("windows")
    if target == 0 or not isinstance(windows, list) or not windows:
        raise ValueError("PHASE4CG_TARGET_OR_WINDOWS_INVALID")
    normalized = []
    for index, window in enumerate(windows, start=1):
        fields = {"window", "sample_count", "p95_ms", "throttle_ppm", "timeout_ppm"}
        if not isinstance(window, dict) or set(window) != fields or window["window"] != index:
            raise ValueError("PHASE4CG_WINDOW_FIELDS_OR_ORDER_INVALID")
        values = {
            key: _nonnegative_int(window[key], f"PHASE4CG_{key.upper()}_INVALID")
            for key in ("sample_count", "p95_ms", "throttle_ppm", "timeout_ppm")
        }
        if values["throttle_ppm"] > 1_000_000 or values["timeout_ppm"] > 1_000_000:
            raise ValueError("PHASE4CG_RATE_PPM_INVALID")
        normalized.append({"window": index, **values})
    total_samples = sum(row["sample_count"] for row in normalized)
    worst_throttle = max(row["throttle_ppm"] for row in normalized)
    worst_timeout = max(row["timeout_ppm"] for row in normalized)
    worst_p95 = max(row["p95_ms"] for row in normalized)
    if worst_throttle >= THROTTLE_DECREASE_PPM or worst_timeout >= TIMEOUT_DECREASE_PPM:
        proposed, decision = max(1, current // 2), "DECREASE_FOR_DEGRADATION"
    elif total_samples < MIN_SAMPLES:
        proposed, decision = current, "HOLD_INSUFFICIENT_EVIDENCE"
    elif worst_throttle <= LOW_ERROR_PPM and worst_timeout <= LOW_ERROR_PPM and worst_p95 < target:
        proposed, decision = min(MAX_CONCURRENCY, current + 1), "INCREASE_ONE_STEP"
    else:
        proposed, decision = current, "HOLD_WITHIN_GUARDRAILS"
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CG",
        "input_hash": evidence["artifact_hash"],
        "current_concurrency": current,
        "proposed_concurrency": proposed,
        "decision": decision,
        "evidence_summary": {
            "sample_count": total_samples,
            "worst_p95_ms": worst_p95,
            "worst_throttle_ppm": worst_throttle,
            "worst_timeout_ppm": worst_timeout,
        },
        "proposal_only": True,
        "live_setting_changes_applied": 0,
        "production_records_created": 0,
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
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_proposal(json.loads(args.evidence.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
