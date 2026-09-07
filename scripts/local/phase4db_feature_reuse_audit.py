"""Audit content-addressable forecast-feature reuse across offline instances."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4db.reuse-input.v1"
REPORT_SCHEMA = "phase4db.reuse-report.v1"
SCOPES = ("IMMUTABLE", "TIME_BOUND", "FORECAST_SPECIFIC")
MAX_INSTANCES = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4DB_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4DB_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("PHASE4DB_TIMESTAMP_INVALID")
    return parsed


def _digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("PHASE4DB_UPSTREAM_HASH_INVALID")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("PHASE4DB_UPSTREAM_HASH_INVALID") from exc
    return value


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "evaluated_at", "instances", "artifact_hash"}:
        raise ValueError("PHASE4DB_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DB_INPUT_SCHEMA_OR_HASH_INVALID")
    evaluated_at = _time(payload["evaluated_at"])
    instances = payload.get("instances")
    if not isinstance(instances, list) or not instances or len(instances) > MAX_INSTANCES:
        raise ValueError("PHASE4DB_INSTANCE_COUNT_INVALID")

    identifiers: set[str] = set()
    eligible_groups: dict[str, list[dict[str, Any]]] = {}
    decisions = []
    required = {
        "instance_id",
        "forecast_id",
        "feature_id",
        "value",
        "computation_version",
        "upstream_hashes",
        "deterministic",
        "scope",
        "valid_until",
    }
    for instance in instances:
        if not isinstance(instance, dict) or set(instance) != required:
            raise ValueError("PHASE4DB_INSTANCE_FIELDS_INVALID")
        identifier = instance["instance_id"]
        forecast = instance["forecast_id"]
        feature = instance["feature_id"]
        version = instance["computation_version"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4DB_INSTANCE_ID_INVALID")
        if any(not isinstance(value, str) or not value for value in (forecast, feature, version)):
            raise ValueError("PHASE4DB_IDENTITY_INVALID")
        upstream = instance["upstream_hashes"]
        if (
            not isinstance(upstream, dict)
            or not upstream
            or any(not isinstance(name, str) or not name for name in upstream)
        ):
            raise ValueError("PHASE4DB_UPSTREAM_INVALID")
        for digest in upstream.values():
            _digest(digest)
        if not isinstance(instance["deterministic"], bool) or instance["scope"] not in SCOPES:
            raise ValueError("PHASE4DB_POLICY_INVALID")
        valid_until = instance["valid_until"]
        if instance["scope"] == "TIME_BOUND":
            expiry = _time(valid_until)
        elif valid_until is not None:
            raise ValueError("PHASE4DB_VALID_UNTIL_INVALID")
        else:
            expiry = None
        identifiers.add(identifier)
        content = {
            "feature_id": feature,
            "value": instance["value"],
            "computation_version": version,
            "upstream_hashes": upstream,
        }
        content_key = canonical_hash(content)
        reasons = []
        if not instance["deterministic"]:
            reasons.append("NONDETERMINISTIC_COMPUTATION")
        if instance["scope"] == "FORECAST_SPECIFIC":
            reasons.append("FORECAST_SPECIFIC_SCOPE")
        if expiry is not None and expiry < evaluated_at:
            reasons.append("FEATURE_EXPIRED")
        decision = {
            "instance_id": identifier,
            "forecast_id": forecast,
            "feature_id": feature,
            "content_key": content_key,
            "eligible": not reasons,
            "reasons": reasons,
        }
        decisions.append(decision)
        if not reasons:
            eligible_groups.setdefault(content_key, []).append(decision)

    groups = []
    for key in sorted(eligible_groups):
        group = eligible_groups[key]
        forecasts = sorted({row["forecast_id"] for row in group})
        if len(forecasts) < 2:
            continue
        groups.append(
            {
                "content_key": key,
                "feature_id": group[0]["feature_id"],
                "forecast_ids": forecasts,
                "instance_ids": sorted(row["instance_id"] for row in group),
                "reusable_across_forecasts": True,
                "computations_saved": len(group) - 1,
            }
        )
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DB",
        "input_hash": payload["artifact_hash"],
        "decisions": decisions,
        "reuse_groups": groups,
        "total_computations_saved": sum(group["computations_saved"] for group in groups),
        "content_store_writes": 0,
        "forecast_records_created": 0,
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
    parser.add_argument("--instances", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.instances.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
