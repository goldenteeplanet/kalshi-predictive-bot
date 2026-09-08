"""Review every residual path that could eventually require execution capability."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ey.review-input.v1"
REPORT_SCHEMA = "phase4ey.review-report.v1"
REQUIRED_PATHS = {
    "operator_authorization": {
        "capability": "HUMAN_AUTHORIZATION",
        "prerequisites": {"EXPLICIT_HUMAN_AUTHORIZATION", "IDENTITY_BOUND_APPROVAL"},
    },
    "credential_loading": {
        "capability": "CREDENTIALS",
        "prerequisites": {"CREDENTIAL_PROVISIONING", "SECRET_SCOPE_APPROVAL"},
    },
    "production_writer_access": {
        "capability": "WRITER_ACCESS",
        "prerequisites": {
            "EXPLICIT_WRITER_AUTHORIZATION",
            "PRODUCTION_DATABASE_IDENTITY",
            "WRITER_LOCK",
        },
    },
    "exchange_connectivity": {
        "capability": "EXCHANGE_ACCESS",
        "prerequisites": {
            "EXCHANGE_ACCESS_AUTHORIZATION",
            "NETWORK_ENABLEMENT",
            "SCOPED_CREDENTIALS",
        },
    },
    "paper_order_creation": {
        "capability": "PAPER_ORDER_CREATION",
        "prerequisites": {
            "ALL_SAFETY_GATES",
            "EXPLICIT_HUMAN_AUTHORIZATION",
            "PAPER_ORDER_FEATURE_ENABLEMENT",
        },
    },
}
REQUIRED_CAPABILITIES = {
    "HUMAN_AUTHORIZATION",
    "CREDENTIALS",
    "WRITER_ACCESS",
    "EXCHANGE_ACCESS",
    "PAPER_ORDER_CREATION",
}


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


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "review_scope_hash",
        "paths",
        "artifact_hash",
    }:
        raise ValueError("PHASE4EY_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EY_INPUT_SCHEMA_OR_HASH_INVALID")
    scope_hash = _digest(payload["review_scope_hash"], "PHASE4EY_SCOPE_HASH_INVALID")
    paths = payload["paths"]
    if not isinstance(paths, list) or len(paths) != len(REQUIRED_PATHS):
        raise ValueError("PHASE4EY_PATH_SET_INVALID")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        if not isinstance(path, dict) or set(path) != {
            "path_id",
            "capability",
            "current_state",
            "activation_prerequisites",
            "control_owner",
            "evidence_hash",
        }:
            raise ValueError("PHASE4EY_PATH_FIELDS_INVALID")
        path_id = path["path_id"]
        if path_id not in REQUIRED_PATHS or path_id in seen:
            raise ValueError("PHASE4EY_PATH_ID_INVALID")
        seen.add(path_id)
        expected = REQUIRED_PATHS[path_id]
        if path["capability"] != expected["capability"]:
            raise ValueError("PHASE4EY_CAPABILITY_INVALID")
        if path["current_state"] != "ABSENT_OR_DISABLED":
            raise ValueError("PHASE4EY_CURRENT_STATE_UNSAFE")
        prerequisites = path["activation_prerequisites"]
        if (
            not isinstance(prerequisites, list)
            or len(prerequisites) != len(set(prerequisites))
            or any(not isinstance(item, str) or not item for item in prerequisites)
            or set(prerequisites) != expected["prerequisites"]
        ):
            raise ValueError("PHASE4EY_PREREQUISITES_INVALID")
        normalized.append(
            {
                "path_id": path_id,
                "capability": path["capability"],
                "current_state": path["current_state"],
                "activation_prerequisites": sorted(prerequisites),
                "control_owner": _text(path["control_owner"], "PHASE4EY_CONTROL_OWNER_INVALID"),
                "evidence_hash": _digest(path["evidence_hash"], "PHASE4EY_EVIDENCE_HASH_INVALID"),
                "risk_disposition": "DOCUMENTED_NOT_AUTHORIZED",
            }
        )
    if seen != set(REQUIRED_PATHS):
        raise ValueError("PHASE4EY_PATH_SET_INVALID")
    normalized.sort(key=lambda item: item["path_id"])
    capabilities = {item["capability"] for item in normalized}
    complete = capabilities == REQUIRED_CAPABILITIES
    if not complete:
        raise ValueError("PHASE4EY_CAPABILITY_COVERAGE_INCOMPLETE")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EY",
        "input_hash": payload["artifact_hash"],
        "review_scope_hash": scope_hash,
        "required_path_ids": sorted(REQUIRED_PATHS),
        "required_capabilities": sorted(REQUIRED_CAPABILITIES),
        "residual_paths": normalized,
        "residual_path_count": len(normalized),
        "all_residual_paths_documented": complete,
        "review_complete": complete,
        "human_authorization_granted": False,
        "credentials_loaded": False,
        "writer_access_granted": False,
        "exchange_access_granted": False,
        "paper_order_creation_enabled": False,
        "paper_order_creation_authorized": False,
        "paper_orders_created": 0,
        "production_database_mutated": False,
        "services_controlled": False,
        "exchange_requests_made": False,
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
