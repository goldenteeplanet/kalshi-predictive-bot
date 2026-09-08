"""Evaluate a deterministic, artifact-only air-gapped acceptance transcript."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bm.airgap-acceptance-input.v1"
SCHEMA = "phase4bm.airgap-acceptance-report.v1"
CHECKS = (
    "COMPLETE_ARTIFACT_CHAIN",
    "DISPOSABLE_SIMULATION_SUCCEEDED",
    "ALL_REFUSAL_CLASSES_EXERCISED",
    "ROLLBACK_VERIFIED",
    "DETERMINISTIC_OUTPUT_VERIFIED",
    "NO_NETWORK_ATTEMPTS",
    "NO_PRODUCTION_PATH_DEPENDENCY",
    "NO_SERVICE_DEPENDENCY",
)


def _hash(payload: dict[str, Any]) -> str:
    return canonical_hash({k: v for k, v in payload.items() if k != "artifact_hash"})


def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4BM_INPUT_SCHEMA_OR_HASH_INVALID")
    rows = payload.get("checks")
    if not isinstance(rows, list) or [
        row.get("check") for row in rows if isinstance(row, dict)
    ] != list(CHECKS):
        raise ValueError("PHASE4BM_CHECK_COVERAGE_OR_ORDER_INVALID")
    if any(
        row.get("passed") is not True
        or not isinstance(row.get("evidence_hash"), str)
        or len(row["evidence_hash"]) != 64
        for row in rows
    ):
        raise ValueError("PHASE4BM_ACCEPTANCE_CHECK_FAILED")
    refusals = payload.get("refusal_classes")
    if not isinstance(refusals, list) or not refusals or refusals != sorted(set(refusals)):
        raise ValueError("PHASE4BM_REFUSAL_COVERAGE_INVALID")
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BM",
        "input_hash": payload["artifact_hash"],
        "checks": rows,
        "refusal_classes": refusals,
        "accepted": True,
        "synthetic_or_copied_artifacts_only": True,
        "network_attempts": 0,
        "production_dependencies": 0,
        "service_dependencies": 0,
        "execution_authorized": False,
    }
    report["artifact_hash"] = _hash(report)
    return report


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    report = evaluate(payload)
    atomic_write_json(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
