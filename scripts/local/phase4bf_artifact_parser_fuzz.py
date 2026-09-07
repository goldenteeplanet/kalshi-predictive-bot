"""Phase 4BF deterministic bounded artifact-parser fuzz harness."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bf.fuzz-target-catalog.v1"
SCHEMA = "phase4bf.deterministic-fuzz-report.v1"
PROOF_SCHEMA = "phase4bf.parser-bounds-proof.v1"
REQUIRED_PHASES = tuple(
    [f"4A{chr(code)}" for code in range(ord("C"), ord("Z") + 1)]
    + [f"4B{chr(code)}" for code in range(ord("A"), ord("E") + 1)]
)
MAX_BYTES = 131_072
MAX_DEPTH = 24
MAX_LIST = 512
MAX_INTEGER = 2**63 - 1
CASES = (
    "TRUNCATED_JSON",
    "DEEPLY_NESTED_VALUE",
    "EXTREME_INTEGER",
    "DUPLICATE_LOGICAL_KEY",
    "UNICODE_EDGE_CASE",
    "INVALID_PATH",
    "MALFORMED_TIMESTAMP",
    "OVERSIZED_LIST",
    "HASH_CONFUSION",
    "UNEXPECTED_NULL",
    "TYPE_SUBSTITUTION",
)


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("PHASE4BF_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _bounds(value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ValueError("PHASE4BF_MAX_DEPTH_EXCEEDED")
    if value is None:
        raise ValueError("PHASE4BF_UNEXPECTED_NULL")
    if isinstance(value, bool):
        return
    if isinstance(value, int) and not -MAX_INTEGER <= value <= MAX_INTEGER:
        raise ValueError("PHASE4BF_INTEGER_OUT_OF_RANGE")
    if isinstance(value, list):
        if len(value) > MAX_LIST:
            raise ValueError("PHASE4BF_LIST_LIMIT_EXCEEDED")
        for item in value:
            _bounds(item, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("PHASE4BF_KEY_TYPE_INVALID")
            _bounds(item, depth + 1)


def parse(data: bytes, *, expected_schema: str, hash_field: str) -> dict[str, Any]:
    if len(data) > MAX_BYTES:
        raise ValueError("PHASE4BF_ARTIFACT_SIZE_LIMIT_EXCEEDED")
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4BF_JSON_INVALID") from exc
    if not isinstance(payload, dict) or payload.get("schema") != expected_schema:
        raise ValueError("PHASE4BF_SCHEMA_OR_ROOT_TYPE_INVALID")
    _bounds(payload)
    allowed = {"schema", "value", "relative_path", "evaluated_at", hash_field}
    if set(payload) != allowed:
        raise ValueError("PHASE4BF_FIELDS_INVALID")
    relative = payload.get("relative_path")
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
        or "\x00" in relative
    ):
        raise ValueError("PHASE4BF_PATH_INVALID")
    timestamp = payload.get("evaluated_at")
    if not isinstance(timestamp, str):
        raise ValueError("PHASE4BF_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("PHASE4BF_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError("PHASE4BF_TIMESTAMP_TIMEZONE_MISSING")
    if payload.get(hash_field) != _hash(payload, hash_field):
        raise ValueError("PHASE4BF_HASH_MISMATCH")
    return payload


def _base(schema: str, hash_field: str) -> dict[str, Any]:
    payload = {
        "schema": schema,
        "value": "safe",
        "relative_path": "artifacts/fixture.json",
        "evaluated_at": "2026-08-25T12:00:00+00:00",
    }
    payload[hash_field] = _hash(payload, hash_field)
    return payload


def _case(schema: str, hash_field: str, name: str) -> tuple[bytes, bool]:
    payload = _base(schema, hash_field)
    if name == "TRUNCATED_JSON":
        return json.dumps(payload).encode()[:-2], False
    if name == "DEEPLY_NESTED_VALUE":
        value: Any = "leaf"
        for _ in range(MAX_DEPTH + 2):
            value = [value]
        payload["value"] = value
    elif name == "EXTREME_INTEGER":
        payload["value"] = MAX_INTEGER + 1
    elif name == "DUPLICATE_LOGICAL_KEY":
        return (
            (f'{{"schema":"{schema}","schema":"{schema}","value":"x"}}').encode(),
            False,
        )
    elif name == "UNICODE_EDGE_CASE":
        payload["value"] = "snowman-☃-combining-e\u0301"
        payload[hash_field] = _hash(payload, hash_field)
        return json.dumps(payload, ensure_ascii=False).encode(), True
    elif name == "INVALID_PATH":
        payload["relative_path"] = "../../production.db"
    elif name == "MALFORMED_TIMESTAMP":
        payload["evaluated_at"] = "not-a-time"
    elif name == "OVERSIZED_LIST":
        payload["value"] = [0] * (MAX_LIST + 1)
    elif name == "HASH_CONFUSION":
        payload[hash_field] = "0" * 64
        return json.dumps(payload).encode(), False
    elif name == "UNEXPECTED_NULL":
        payload["value"] = None
    elif name == "TYPE_SUBSTITUTION":
        payload["schema"] = [schema]
    else:
        raise ValueError("PHASE4BF_CASE_UNKNOWN")
    payload[hash_field] = _hash(payload, hash_field)
    return json.dumps(payload).encode(), False


def build(catalog_path: Path, *, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4BF_EVALUATION_TIMEZONE_MISSING")
    source = parse(
        catalog_path.read_bytes(), expected_schema=INPUT_SCHEMA, hash_field="artifact_hash"
    )
    targets = json.loads(source["value"])
    if not isinstance(targets, list) or [
        target.get("phase") for target in targets if isinstance(target, dict)
    ] != list(REQUIRED_PHASES):
        raise ValueError("PHASE4BF_TARGET_COVERAGE_OR_ORDER_INVALID")
    rows: list[dict[str, Any]] = []
    seen_schemas: set[str] = set()
    for target in targets:
        schema, cli, hash_field = target.get("schema"), target.get("cli"), target.get("hash_field")
        if (
            not isinstance(schema, str)
            or not isinstance(cli, str)
            or hash_field not in {"artifact_hash", "manifest_hash"}
        ):
            raise ValueError("PHASE4BF_TARGET_INVALID")
        if schema in seen_schemas:
            raise ValueError("PHASE4BF_DUPLICATE_TARGET_SCHEMA")
        seen_schemas.add(schema)
        for case_name in CASES:
            data, should_accept = _case(schema, hash_field, case_name)
            accepted = True
            reason = None
            try:
                parse(data, expected_schema=schema, hash_field=hash_field)
            except ValueError as exc:
                accepted = False
                reason = str(exc)
            if accepted != should_accept:
                raise ValueError("PHASE4BF_FUZZ_EXPECTATION_FAILED")
            row = {
                "phase": target["phase"],
                "schema": schema,
                "cli": cli,
                "case": case_name,
                "safe_outcome": "CANONICAL_ACCEPT" if accepted else "FAIL_CLOSED",
                "reason": reason,
            }
            row["row_hash"] = canonical_hash(row)
            rows.append(row)
    evaluated_at = now.astimezone(UTC).isoformat()
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BF",
        "evaluated_at": evaluated_at,
        "catalog_hash": source["artifact_hash"],
        "target_count": len(targets),
        "case_count": len(rows),
        "rows_hash": canonical_hash(rows),
        "all_cases_safe": True,
        "database_mutation_performed": False,
        "execution_authorized": False,
    }
    report["artifact_hash"] = _hash(report)
    proof: dict[str, Any] = {
        "schema": PROOF_SCHEMA,
        "phase": "4BF",
        "evaluated_at": evaluated_at,
        "fuzz_report_hash": report["artifact_hash"],
        "max_artifact_bytes": MAX_BYTES,
        "max_depth": MAX_DEPTH,
        "max_list_length": MAX_LIST,
        "max_integer": MAX_INTEGER,
        "duplicate_keys_rejected": True,
        "bounded_resource_use": True,
        "production_database_mutated": False,
        "network_access_performed": False,
        "execution_authorized": False,
    }
    proof["artifact_hash"] = _hash(proof)
    return report, proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fuzz-target-catalog", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    report, proof = build(args.fuzz_target_catalog, now=now)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.report_output, args.proof_output, report, proof)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
