"""Attribute offline ranking drift to explicit evidence and execution semantics."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dm.drift-input.v1"
REPORT_SCHEMA = "phase4dm.drift-report.v1"


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("PHASE4DM_HASH_INVALID")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("PHASE4DM_HASH_INVALID") from exc
    return value


def _validate_snapshot(snapshot: Any) -> None:
    fields = {
        "model_id",
        "model_hash",
        "dependency_hashes",
        "arithmetic_mode",
        "sort_spec_hash",
        "inputs_fresh",
        "ranking",
    }
    if not isinstance(snapshot, dict) or set(snapshot) != fields:
        raise ValueError("PHASE4DM_SNAPSHOT_FIELDS_INVALID")
    if not isinstance(snapshot["model_id"], str) or not snapshot["model_id"]:
        raise ValueError("PHASE4DM_MODEL_ID_INVALID")
    _digest(snapshot["model_hash"])
    _digest(snapshot["sort_spec_hash"])
    dependencies = snapshot["dependency_hashes"]
    if (
        not isinstance(dependencies, dict)
        or not dependencies
        or any(not isinstance(key, str) or not key for key in dependencies)
    ):
        raise ValueError("PHASE4DM_DEPENDENCIES_INVALID")
    for digest in dependencies.values():
        _digest(digest)
    if snapshot["arithmetic_mode"] not in {"DECIMAL", "BINARY_FLOAT"}:
        raise ValueError("PHASE4DM_ARITHMETIC_MODE_INVALID")
    if not isinstance(snapshot["inputs_fresh"], bool):
        raise ValueError("PHASE4DM_FRESHNESS_INVALID")
    ranking = snapshot["ranking"]
    if not isinstance(ranking, list) or not ranking:
        raise ValueError("PHASE4DM_RANKING_INVALID")
    ids: set[str] = set()
    for index, row in enumerate(ranking, start=1):
        if not isinstance(row, dict) or set(row) != {"rank", "candidate_id", "score"}:
            raise ValueError("PHASE4DM_RANKING_ROW_INVALID")
        if row["rank"] != index:
            raise ValueError("PHASE4DM_RANK_SEQUENCE_INVALID")
        identifier = row["candidate_id"]
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            raise ValueError("PHASE4DM_CANDIDATE_ID_INVALID")
        ids.add(identifier)
        if not isinstance(row["score"], str) or not row["score"]:
            raise ValueError("PHASE4DM_SCORE_INVALID")


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "baseline", "current", "artifact_hash"}:
        raise ValueError("PHASE4DM_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DM_INPUT_SCHEMA_OR_HASH_INVALID")
    baseline, current = payload["baseline"], payload["current"]
    _validate_snapshot(baseline)
    _validate_snapshot(current)
    reasons = []
    if (
        baseline["model_id"] != current["model_id"]
        or baseline["model_hash"] != current["model_hash"]
    ):
        reasons.append("MODEL_IDENTITY_CHANGED")
    if baseline["dependency_hashes"] != current["dependency_hashes"]:
        reasons.append("DEPENDENCY_CHANGED")
    if baseline["arithmetic_mode"] != current["arithmetic_mode"]:
        reasons.append("ARITHMETIC_BEHAVIOR_CHANGED")
    if baseline["sort_spec_hash"] != current["sort_spec_hash"]:
        reasons.append("ORDERING_SPEC_CHANGED")
    if baseline["inputs_fresh"] != current["inputs_fresh"] or not current["inputs_fresh"]:
        reasons.append("STALE_INPUT_STATE")
    drift = baseline["ranking"] != current["ranking"]
    if drift and not reasons:
        reasons.append("UNATTRIBUTED_RANKING_DRIFT")
    if drift:
        status = (
            "UNATTRIBUTED_DRIFT"
            if reasons == ["UNATTRIBUTED_RANKING_DRIFT"]
            else "ATTRIBUTED_DRIFT"
        )
    else:
        status = "NO_RANKING_DRIFT"
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DM",
        "input_hash": payload["artifact_hash"],
        "status": status,
        "ranking_changed": drift,
        "reasons": reasons,
        "baseline_ranking_hash": canonical_hash(baseline["ranking"]),
        "current_ranking_hash": canonical_hash(current["ranking"]),
        "safe_to_ignore": not drift,
        "ranking_records_created": 0,
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
