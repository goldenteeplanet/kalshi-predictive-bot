"""Simulate paper routing deterministically without creating orders or fills."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4el.simulator-input.v1"
REPORT_SCHEMA = "phase4el.simulator-report.v1"
STAGES = ("duplicate_check", "quantity_check", "price_check", "fill_model")


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _positive_int(value: Any, error: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(error)
    return value


def _digest(value: Any, error: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(error)
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(error) from exc


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "policy", "stage_latency_ms", "intents", "artifact_hash"}:
        raise ValueError("PHASE4EL_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EL_INPUT_SCHEMA_OR_HASH_INVALID")
    policy = payload.get("policy")
    if not isinstance(policy, dict) or set(policy) != {
        "max_quantity",
        "min_price_cents",
        "max_price_cents",
    }:
        raise ValueError("PHASE4EL_POLICY_FIELDS_INVALID")
    maximum_quantity = _positive_int(policy["max_quantity"], "PHASE4EL_MAX_QUANTITY_INVALID")
    minimum_price = _positive_int(policy["min_price_cents"], "PHASE4EL_PRICE_BOUND_INVALID")
    maximum_price = _positive_int(policy["max_price_cents"], "PHASE4EL_PRICE_BOUND_INVALID")
    if minimum_price > maximum_price or maximum_price > 99:
        raise ValueError("PHASE4EL_PRICE_BOUND_INVALID")
    latency = payload.get("stage_latency_ms")
    if not isinstance(latency, dict) or set(latency) != set(STAGES):
        raise ValueError("PHASE4EL_LATENCY_FIELDS_INVALID")
    for stage in STAGES:
        _positive_int(latency[stage], "PHASE4EL_LATENCY_INVALID", allow_zero=True)
    intents = payload.get("intents")
    if not isinstance(intents, list) or not intents:
        raise ValueError("PHASE4EL_INTENTS_EMPTY")
    seen_ids = set()
    groups: dict[str, list[dict[str, Any]]] = {}
    required = {
        "intent_id",
        "idempotency_key",
        "handoff_artifact_hash",
        "quantity",
        "price_cents",
        "available_fill_quantity",
    }
    for intent in intents:
        if not isinstance(intent, dict) or set(intent) != required:
            raise ValueError("PHASE4EL_INTENT_FIELDS_INVALID")
        identifier = intent["intent_id"]
        key = intent["idempotency_key"]
        if not isinstance(identifier, str) or not identifier or identifier in seen_ids:
            raise ValueError("PHASE4EL_INTENT_ID_INVALID")
        seen_ids.add(identifier)
        if not isinstance(key, str) or not key:
            raise ValueError("PHASE4EL_IDEMPOTENCY_KEY_INVALID")
        _digest(intent["handoff_artifact_hash"], "PHASE4EL_HANDOFF_HASH_INVALID")
        _positive_int(intent["quantity"], "PHASE4EL_QUANTITY_INVALID")
        _positive_int(intent["price_cents"], "PHASE4EL_PRICE_INVALID")
        _positive_int(
            intent["available_fill_quantity"], "PHASE4EL_FILL_QUANTITY_INVALID", allow_zero=True
        )
        groups.setdefault(key, []).append(intent)

    primary_by_key = {
        key: min(group, key=lambda row: row["intent_id"])["intent_id"]
        for key, group in groups.items()
    }
    results = []
    for intent in sorted(intents, key=lambda row: row["intent_id"]):
        stages_run = ["duplicate_check"]
        reasons = []
        if intent["intent_id"] != primary_by_key[intent["idempotency_key"]]:
            reasons.append("DUPLICATE_INTENT")
        else:
            stages_run.append("quantity_check")
            if intent["quantity"] > maximum_quantity:
                reasons.append("QUANTITY_LIMIT_EXCEEDED")
            else:
                stages_run.append("price_check")
                if not minimum_price <= intent["price_cents"] <= maximum_price:
                    reasons.append("PRICE_OUT_OF_BOUNDS")
                else:
                    stages_run.append("fill_model")
        filled = 0
        fill_status = "NOT_MODELED"
        if stages_run[-1] == "fill_model":
            filled = min(intent["quantity"], intent["available_fill_quantity"])
            if filled == 0:
                fill_status = "UNFILLED"
            elif filled == intent["quantity"]:
                fill_status = "FULL_FILL"
            else:
                fill_status = "PARTIAL_FILL"
        results.append(
            {
                "intent_id": intent["intent_id"],
                "idempotency_key": intent["idempotency_key"],
                "status": "SIMULATED" if not reasons else "REFUSE",
                "reasons": reasons,
                "stages_run": stages_run,
                "simulated_latency_ms": sum(latency[stage] for stage in stages_run),
                "requested_quantity": intent["quantity"],
                "simulated_filled_quantity": filled,
                "simulated_remaining_quantity": intent["quantity"] - filled,
                "fill_status": fill_status,
                "price_cents": intent["price_cents"],
                "handoff_artifact_hash": intent["handoff_artifact_hash"],
            }
        )
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EL",
        "input_hash": payload["artifact_hash"],
        "results": results,
        "simulated_count": sum(row["status"] == "SIMULATED" for row in results),
        "refused_count": sum(row["status"] == "REFUSE" for row in results),
        "duplicate_count": sum("DUPLICATE_INTENT" in row["reasons"] for row in results),
        "real_paper_orders_created": 0,
        "real_paper_fills_created": 0,
        "database_writes": 0,
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
