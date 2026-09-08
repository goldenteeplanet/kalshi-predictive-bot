"""Build a minimal, non-authorizing paper-eligibility handoff artifact."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ej.handoff-input.v1"
REPORT_SCHEMA = "phase4ej.handoff-report.v1"


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _digest(value: Any, error: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(error)
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(error) from exc
    return value


def _reasons(value: Any, error: str) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) != len(set(value))
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise ValueError(error)
    return sorted(value)


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "candidate_id",
        "forecast",
        "ranking",
        "risk",
        "operator",
        "artifact_hash",
    }
    if set(payload) != required:
        raise ValueError("PHASE4EJ_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EJ_INPUT_SCHEMA_OR_HASH_INVALID")
    candidate_id = payload["candidate_id"]
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("PHASE4EJ_CANDIDATE_ID_INVALID")

    forecast = payload["forecast"]
    if not isinstance(forecast, dict) or set(forecast) != {
        "quality_passed",
        "reason_codes",
        "artifact_hash",
    }:
        raise ValueError("PHASE4EJ_FORECAST_FIELDS_INVALID")
    if not isinstance(forecast["quality_passed"], bool):
        raise ValueError("PHASE4EJ_FORECAST_STATUS_INVALID")
    forecast_reasons = _reasons(forecast["reason_codes"], "PHASE4EJ_FORECAST_REASONS_INVALID")
    _digest(forecast["artifact_hash"], "PHASE4EJ_FORECAST_HASH_INVALID")

    ranking = payload["ranking"]
    if not isinstance(ranking, dict) or set(ranking) != {
        "eligible",
        "reason_codes",
        "artifact_hash",
    }:
        raise ValueError("PHASE4EJ_RANKING_FIELDS_INVALID")
    if not isinstance(ranking["eligible"], bool):
        raise ValueError("PHASE4EJ_RANKING_STATUS_INVALID")
    ranking_reasons = _reasons(ranking["reason_codes"], "PHASE4EJ_RANKING_REASONS_INVALID")
    _digest(ranking["artifact_hash"], "PHASE4EJ_RANKING_HASH_INVALID")

    risk = payload["risk"]
    if not isinstance(risk, dict) or set(risk) != {
        "eligible",
        "allowed_quantity",
        "reason_codes",
        "artifact_hash",
    }:
        raise ValueError("PHASE4EJ_RISK_FIELDS_INVALID")
    if not isinstance(risk["eligible"], bool):
        raise ValueError("PHASE4EJ_RISK_STATUS_INVALID")
    quantity = risk["allowed_quantity"]
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        raise ValueError("PHASE4EJ_QUANTITY_INVALID")
    if (risk["eligible"] and quantity <= 0) or (not risk["eligible"] and quantity != 0):
        raise ValueError("PHASE4EJ_RISK_QUANTITY_INCONSISTENT")
    risk_reasons = _reasons(risk["reason_codes"], "PHASE4EJ_RISK_REASONS_INVALID")
    _digest(risk["artifact_hash"], "PHASE4EJ_RISK_HASH_INVALID")

    operator = payload["operator"]
    if not isinstance(operator, dict) or set(operator) != {
        "approved",
        "approval_id",
        "artifact_hash",
    }:
        raise ValueError("PHASE4EJ_OPERATOR_FIELDS_INVALID")
    if not isinstance(operator["approved"], bool):
        raise ValueError("PHASE4EJ_OPERATOR_STATUS_INVALID")
    approval_id = operator["approval_id"]
    if operator["approved"]:
        if not isinstance(approval_id, str) or not approval_id:
            raise ValueError("PHASE4EJ_APPROVAL_ID_INVALID")
    elif approval_id is not None:
        raise ValueError("PHASE4EJ_APPROVAL_ID_INVALID")
    _digest(operator["artifact_hash"], "PHASE4EJ_OPERATOR_HASH_INVALID")

    gate_values = {
        "forecast_quality": forecast["quality_passed"],
        "ranking_eligibility": ranking["eligible"],
        "risk_eligibility": risk["eligible"],
        "operator_approval": operator["approved"],
    }
    routing_eligible = all(gate_values.values())
    routing_reasons = [
        f"{name.upper()}_FAILED" for name, passed in gate_values.items() if not passed
    ]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EJ",
        "input_hash": payload["artifact_hash"],
        "candidate_id": candidate_id,
        "forecast_quality": {
            "passed": forecast["quality_passed"],
            "reason_codes": forecast_reasons,
            "artifact_hash": forecast["artifact_hash"],
        },
        "ranking_eligibility": {
            "eligible": ranking["eligible"],
            "reason_codes": ranking_reasons,
            "artifact_hash": ranking["artifact_hash"],
        },
        "risk_eligibility": {
            "eligible": risk["eligible"],
            "allowed_quantity": quantity,
            "reason_codes": risk_reasons,
            "artifact_hash": risk["artifact_hash"],
        },
        "operator_approval": {
            "approved": operator["approved"],
            "approval_id": approval_id,
            "artifact_hash": operator["artifact_hash"],
        },
        "routing_eligibility": {"eligible": routing_eligible, "reason_codes": routing_reasons},
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
