"""Compress synthetic review evidence into a minimal non-authorizing operator packet."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4eq.packet-input.v1"
REPORT_SCHEMA = "phase4eq.packet-report.v1"


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


def _integer(value: Any, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(code)
    return value


def _reasons(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) != len(set(value))
        or any(not isinstance(item, str) or not item or item.strip() != item for item in value)
    ):
        raise ValueError("PHASE4EQ_REASON_CODES_INVALID")
    return sorted(value)


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "candidate_id",
        "decision",
        "provenance",
        "provenance_manifest_hash",
        "artifact_hash",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("PHASE4EQ_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EQ_INPUT_SCHEMA_OR_HASH_INVALID")
    candidate_id = _text(payload["candidate_id"], "PHASE4EQ_CANDIDATE_ID_INVALID")

    decision = payload["decision"]
    decision_fields = {
        "market_ticker",
        "side",
        "quantity",
        "limit_price_cents",
        "expected_edge_bps",
        "expires_at_utc",
        "eligible",
        "reason_codes",
        "binding_cap",
        "operator_review_required",
    }
    if not isinstance(decision, dict) or set(decision) != decision_fields:
        raise ValueError("PHASE4EQ_DECISION_FIELDS_INVALID")
    market = _text(decision["market_ticker"], "PHASE4EQ_MARKET_INVALID")
    if decision["side"] not in {"YES", "NO"}:
        raise ValueError("PHASE4EQ_SIDE_INVALID")
    quantity = _integer(decision["quantity"], 0, 1_000_000, "PHASE4EQ_QUANTITY_INVALID")
    price = _integer(decision["limit_price_cents"], 1, 99, "PHASE4EQ_PRICE_INVALID")
    edge = _integer(decision["expected_edge_bps"], -10_000, 10_000, "PHASE4EQ_EDGE_INVALID")
    expires = _text(decision["expires_at_utc"], "PHASE4EQ_EXPIRATION_INVALID")
    if not expires.endswith("Z"):
        raise ValueError("PHASE4EQ_EXPIRATION_INVALID")
    if not isinstance(decision["eligible"], bool) or not isinstance(
        decision["operator_review_required"], bool
    ):
        raise ValueError("PHASE4EQ_STATUS_INVALID")
    reasons = _reasons(decision["reason_codes"])
    binding_cap = decision["binding_cap"]
    if binding_cap is not None:
        binding_cap = _text(binding_cap, "PHASE4EQ_BINDING_CAP_INVALID")
    if decision["eligible"] and quantity == 0:
        raise ValueError("PHASE4EQ_ELIGIBILITY_QUANTITY_INCONSISTENT")
    if not decision["eligible"] and quantity != 0:
        raise ValueError("PHASE4EQ_ELIGIBILITY_QUANTITY_INCONSISTENT")

    provenance = payload["provenance"]
    if not isinstance(provenance, list) or not provenance:
        raise ValueError("PHASE4EQ_PROVENANCE_INVALID")
    normalized: list[dict[str, str]] = []
    roles: set[str] = set()
    for item in provenance:
        if not isinstance(item, dict) or set(item) != {"role", "artifact_hash", "locator"}:
            raise ValueError("PHASE4EQ_PROVENANCE_FIELDS_INVALID")
        role = _text(item["role"], "PHASE4EQ_PROVENANCE_ROLE_INVALID")
        if role in roles:
            raise ValueError("PHASE4EQ_PROVENANCE_ROLE_DUPLICATE")
        roles.add(role)
        normalized.append(
            {
                "role": role,
                "artifact_hash": _digest(item["artifact_hash"], "PHASE4EQ_PROVENANCE_HASH_INVALID"),
                "locator": _text(item["locator"], "PHASE4EQ_PROVENANCE_LOCATOR_INVALID"),
            }
        )
    normalized.sort(key=lambda item: item["role"])
    supplied_manifest = _digest(
        payload["provenance_manifest_hash"], "PHASE4EQ_MANIFEST_HASH_INVALID"
    )
    calculated_manifest = canonical_hash(normalized)
    if supplied_manifest != calculated_manifest:
        raise ValueError("PHASE4EQ_MANIFEST_HASH_MISMATCH")

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EQ",
        "input_hash": payload["artifact_hash"],
        "candidate_id": candidate_id,
        "decision_critical": {
            "market_ticker": market,
            "side": decision["side"],
            "quantity": quantity,
            "limit_price_cents": price,
            "expected_edge_bps": edge,
            "expires_at_utc": expires,
            "eligible": decision["eligible"],
            "reason_codes": reasons,
            "binding_cap": binding_cap,
            "operator_review_required": decision["operator_review_required"],
        },
        "full_provenance": {
            "artifact_count": len(normalized),
            "manifest_hash": calculated_manifest,
            "artifacts": normalized,
        },
        "packet_field_count": len(decision_fields),
        "operator_authorization_recorded": False,
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
