"""Map ranking dependencies, freshness, invalidation, and stable ordering semantics."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dj.ranking-map-input.v1"
REPORT_SCHEMA = "phase4dj.ranking-map-report.v1"
MAX_INPUTS = 10_000
REQUIRED_TRIGGERS = {"SOURCE_HASH_CHANGED", "SOURCE_STALE"}


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("PHASE4DJ_HASH_INVALID")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("PHASE4DJ_HASH_INVALID") from exc
    return value


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4DJ_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4DJ_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("PHASE4DJ_TIMESTAMP_INVALID")
    return parsed


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    required = {"schema", "evaluated_at", "inputs", "ranking", "artifact_hash"}
    if set(payload) != required:
        raise ValueError("PHASE4DJ_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DJ_INPUT_SCHEMA_OR_HASH_INVALID")
    evaluated_at = _time(payload["evaluated_at"])
    inputs = payload["inputs"]
    if not isinstance(inputs, list) or not inputs or len(inputs) > MAX_INPUTS:
        raise ValueError("PHASE4DJ_INPUT_COUNT_INVALID")
    input_fields = {
        "input_id",
        "artifact_hash",
        "observed_at",
        "max_age_seconds",
        "invalidation_triggers",
    }
    mapped = []
    identifiers: set[str] = set()
    stale = []
    for item in inputs:
        if not isinstance(item, dict) or set(item) != input_fields:
            raise ValueError("PHASE4DJ_DEPENDENCY_FIELDS_INVALID")
        identifier = item["input_id"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4DJ_DEPENDENCY_ID_INVALID")
        identifiers.add(identifier)
        _digest(item["artifact_hash"])
        observed = _time(item["observed_at"])
        max_age = item["max_age_seconds"]
        if (
            observed > evaluated_at
            or not isinstance(max_age, int)
            or isinstance(max_age, bool)
            or max_age < 0
        ):
            raise ValueError("PHASE4DJ_FRESHNESS_POLICY_INVALID")
        triggers = item["invalidation_triggers"]
        if (
            not isinstance(triggers, list)
            or len(set(triggers)) != len(triggers)
            or any(not isinstance(trigger, str) or not trigger for trigger in triggers)
            or not REQUIRED_TRIGGERS <= set(triggers)
        ):
            raise ValueError("PHASE4DJ_INVALIDATION_TRIGGERS_INVALID")
        age = int((evaluated_at - observed).total_seconds())
        is_stale = age > max_age
        if is_stale:
            stale.append(identifier)
        mapped.append(
            {
                "input_id": identifier,
                "artifact_hash": item["artifact_hash"],
                "age_seconds": age,
                "max_age_seconds": max_age,
                "fresh": not is_stale,
                "invalidation_triggers": sorted(triggers),
            }
        )
    ranking = payload["ranking"]
    ranking_fields = {
        "ranking_id",
        "score_dependencies",
        "sort_keys",
        "stable_sort_required",
    }
    if not isinstance(ranking, dict) or set(ranking) != ranking_fields:
        raise ValueError("PHASE4DJ_RANKING_FIELDS_INVALID")
    if not isinstance(ranking["ranking_id"], str) or not ranking["ranking_id"]:
        raise ValueError("PHASE4DJ_RANKING_ID_INVALID")
    dependencies = ranking["score_dependencies"]
    if (
        not isinstance(dependencies, list)
        or not dependencies
        or len(set(dependencies)) != len(dependencies)
        or set(dependencies) != identifiers
    ):
        raise ValueError("PHASE4DJ_SCORE_DEPENDENCIES_INVALID")
    sort_keys = ranking["sort_keys"]
    if not isinstance(sort_keys, list) or not sort_keys:
        raise ValueError("PHASE4DJ_SORT_KEYS_INVALID")
    seen_fields: set[str] = set()
    for key in sort_keys:
        if not isinstance(key, dict) or set(key) != {"field", "direction", "nulls"}:
            raise ValueError("PHASE4DJ_SORT_KEY_FIELDS_INVALID")
        if (
            not isinstance(key["field"], str)
            or not key["field"]
            or key["field"] in seen_fields
            or key["direction"] not in {"ASC", "DESC"}
            or key["nulls"] not in {"FIRST", "LAST", "FORBIDDEN"}
        ):
            raise ValueError("PHASE4DJ_SORT_KEY_INVALID")
        seen_fields.add(key["field"])
    if ranking["stable_sort_required"] is not True:
        raise ValueError("PHASE4DJ_STABLE_SORT_REQUIRED")
    tie = sort_keys[-1]
    if tie != {"field": "candidate_id", "direction": "ASC", "nulls": "FORBIDDEN"}:
        raise ValueError("PHASE4DJ_TOTAL_TIE_BREAKER_REQUIRED")
    all_triggers = sorted(
        {trigger for item in mapped for trigger in item["invalidation_triggers"]}
        | {"RANKING_SPEC_CHANGED", "SORT_KEY_CHANGED", "CANDIDATE_SET_CHANGED"}
    )
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DJ",
        "input_hash": payload["artifact_hash"],
        "ranking_id": ranking["ranking_id"],
        "dependency_map": sorted(mapped, key=lambda item: item["input_id"]),
        "sort_keys": sort_keys,
        "tie_breaker": tie,
        "stable_sort_required": True,
        "invalidation_triggers": all_triggers,
        "stale_input_ids": sorted(stale),
        "disposition": "READY" if not stale else "REFUSE_STALE_INPUT",
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
