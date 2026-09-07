"""Phase 4BB artifact schema compatibility and non-mutating migration proposal verifier."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bb.compatibility-input.v1"
SCHEMA = "phase4bb.schema-compatibility-report.v1"
MIGRATION_SCHEMA = "phase4bb.deterministic-migration-proposals.v1"
JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4BB_INPUT_UNREADABLE") from exc
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4BB_INPUT_SCHEMA_OR_HASH_INVALID")
    return payload


def _family(schema: str) -> str:
    return schema.rsplit(".v", 1)[0] if ".v" in schema else schema


def build(input_path: Path, *, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4BB_EVALUATION_TIMEZONE_MISSING")
    source = _load(input_path)
    policies = source.get("policies")
    artifacts = source.get("artifacts")
    if not isinstance(policies, list) or not policies or not isinstance(artifacts, list):
        raise ValueError("PHASE4BB_POLICIES_OR_ARTIFACTS_INVALID")
    index: dict[str, dict[str, Any]] = {}
    for policy in policies:
        if not isinstance(policy, dict) or not isinstance(policy.get("schema"), str):
            raise ValueError("PHASE4BB_POLICY_INVALID")
        schema = policy["schema"]
        if schema in index:
            raise ValueError("PHASE4BB_DUPLICATE_POLICY")
        required, optional = policy.get("required_fields"), policy.get("optional_fields")
        if not isinstance(required, dict) or not isinstance(optional, dict):
            raise ValueError("PHASE4BB_POLICY_FIELDS_INVALID")
        if set(required) & set(optional) or any(
            type_name not in JSON_TYPES for type_name in [*required.values(), *optional.values()]
        ):
            raise ValueError("PHASE4BB_POLICY_TYPE_INVALID")
        if policy.get("canonicalization") != "RFC8785_SORTED_KEYS_V1":
            raise ValueError("PHASE4BB_CANONICALIZATION_POLICY_INVALID")
        index[schema] = policy
    rows: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    for position, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict) or not isinstance(artifact.get("schema"), str):
            raise ValueError("PHASE4BB_ARTIFACT_INVALID")
        schema = artifact["schema"]
        reasons: list[str] = []
        policy = index.get(schema)
        if policy is None:
            reasons.append("UNKNOWN_OR_FORWARD_SCHEMA_VERSION")
            targets = sorted(item for item in index if _family(item) == _family(schema))
            if targets:
                proposals.append(
                    {
                        "artifact_index": position,
                        "source_schema": schema,
                        "target_schema": targets[-1],
                        "action": "CREATE_NEW_TEMPORARY_ARTIFACT",
                        "source_artifact_unchanged": True,
                    }
                )
        else:
            required = policy["required_fields"]
            optional = policy["optional_fields"]
            for field, type_name in required.items():
                if field not in artifact:
                    reasons.append(f"MISSING_FIELD:{field}")
                elif not isinstance(artifact[field], JSON_TYPES[type_name]) or (
                    type_name == "integer" and isinstance(artifact[field], bool)
                ):
                    reasons.append(f"TYPE_CHANGED:{field}")
            for field, type_name in optional.items():
                if field in artifact and not isinstance(artifact[field], JSON_TYPES[type_name]):
                    reasons.append(f"TYPE_CHANGED:{field}")
            allowed = set(required) | set(optional)
            added = sorted(set(artifact) - allowed)
            if added and policy.get("allow_additional_fields") is not True:
                reasons.extend(f"ADDED_FIELD:{field}" for field in added)
            hash_field = policy.get("hash_field")
            if hash_field:
                if hash_field not in {"artifact_hash", "manifest_hash"}:
                    raise ValueError("PHASE4BB_POLICY_HASH_FIELD_INVALID")
                if artifact.get(hash_field) != _hash(artifact, hash_field):
                    reasons.append("CANONICAL_HASH_MISMATCH")
        row = {
            "artifact_index": position,
            "schema": schema,
            "supported_exactly": not reasons,
            "reason_codes": sorted(reasons),
            "artifact_hash": canonical_hash(artifact),
        }
        row["row_hash"] = canonical_hash(row)
        rows.append(row)
    evaluated_at = now.astimezone(UTC).isoformat()
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BB",
        "evaluated_at": evaluated_at,
        "input_hash": source["artifact_hash"],
        "supported_schema_count": len(index),
        "artifact_count": len(rows),
        "rows": rows,
        "all_artifacts_compatible": all(row["supported_exactly"] for row in rows),
        "database_mutation_performed": False,
        "execution_authorized": False,
    }
    report["artifact_hash"] = _hash(report)
    migration: dict[str, Any] = {
        "schema": MIGRATION_SCHEMA,
        "phase": "4BB",
        "evaluated_at": evaluated_at,
        "compatibility_report_hash": report["artifact_hash"],
        "proposals": sorted(proposals, key=lambda row: row["artifact_index"]),
        "proposal_count": len(proposals),
        "existing_artifacts_rewritten": False,
        "database_mutation_performed": False,
        "execution_authorized": False,
    }
    migration["artifact_hash"] = _hash(migration)
    return report, migration


def _atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compatibility-input", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--migration-proposals-output", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    report, migration = build(args.compatibility_input, now=now)
    _atomic(args.report_output, report)
    _atomic(args.migration_proposals_output, migration)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
