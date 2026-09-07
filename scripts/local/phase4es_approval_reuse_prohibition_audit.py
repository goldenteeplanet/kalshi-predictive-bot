"""Audit that synthetic operator approval cannot be reused across changed bound terms."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4es.audit-input.v1"
REPORT_SCHEMA = "phase4es.audit-report.v1"
DIMENSIONS = (
    "database_identity_hash",
    "forecast_hash",
    "limit_price_cents",
    "quantity",
    "risk_hash",
    "expires_at_utc",
)
TERM_FIELDS = {
    "database_identity_hash",
    "forecast_hash",
    "limit_price_cents",
    "quantity",
    "risk_hash",
    "expires_at_utc",
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


def _integer(value: Any, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(code)
    return value


def _utc(value: Any, code: str) -> str:
    text = _text(value, code)
    if not text.endswith("Z"):
        raise ValueError(code)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(code) from exc
    if (
        parsed.tzinfo != UTC
        or parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z") != text
    ):
        raise ValueError(code)
    return text


def _terms(value: Any, prefix: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != TERM_FIELDS:
        raise ValueError(f"PHASE4ES_{prefix}_FIELDS_INVALID")
    return {
        "database_identity_hash": _digest(
            value["database_identity_hash"], f"PHASE4ES_{prefix}_DATABASE_INVALID"
        ),
        "forecast_hash": _digest(value["forecast_hash"], f"PHASE4ES_{prefix}_FORECAST_INVALID"),
        "limit_price_cents": _integer(
            value["limit_price_cents"], 1, 99, f"PHASE4ES_{prefix}_PRICE_INVALID"
        ),
        "quantity": _integer(
            value["quantity"], 1, 1_000_000, f"PHASE4ES_{prefix}_QUANTITY_INVALID"
        ),
        "risk_hash": _digest(value["risk_hash"], f"PHASE4ES_{prefix}_RISK_INVALID"),
        "expires_at_utc": _utc(value["expires_at_utc"], f"PHASE4ES_{prefix}_EXPIRATION_INVALID"),
    }


def binding_hash(terms: dict[str, Any]) -> str:
    return canonical_hash({"schema": "phase4es.approval-binding.v1", "terms": terms})


def approval_matches(approval: dict[str, Any], candidate_terms: dict[str, Any]) -> bool:
    return (
        approval["binding_hash"] == binding_hash(candidate_terms)
        and approval["terms"] == candidate_terms
    )


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "approval",
        "probes",
        "artifact_hash",
    }:
        raise ValueError("PHASE4ES_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4ES_INPUT_SCHEMA_OR_HASH_INVALID")

    approval = payload["approval"]
    if not isinstance(approval, dict) or set(approval) != {
        "approval_id",
        "terms",
        "binding_hash",
        "artifact_hash",
    }:
        raise ValueError("PHASE4ES_APPROVAL_FIELDS_INVALID")
    approval_id = _text(approval["approval_id"], "PHASE4ES_APPROVAL_ID_INVALID")
    baseline = _terms(approval["terms"], "APPROVAL_TERMS")
    supplied_binding = _digest(approval["binding_hash"], "PHASE4ES_APPROVAL_BINDING_INVALID")
    if supplied_binding != binding_hash(baseline):
        raise ValueError("PHASE4ES_APPROVAL_BINDING_MISMATCH")
    if approval["artifact_hash"] != _hash(approval):
        raise ValueError("PHASE4ES_APPROVAL_HASH_INVALID")

    probes = payload["probes"]
    if not isinstance(probes, list) or len(probes) != len(DIMENSIONS):
        raise ValueError("PHASE4ES_PROBE_SET_INVALID")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for probe in probes:
        if not isinstance(probe, dict) or set(probe) != {"dimension", "candidate_terms"}:
            raise ValueError("PHASE4ES_PROBE_FIELDS_INVALID")
        dimension = probe["dimension"]
        if dimension not in DIMENSIONS or dimension in seen:
            raise ValueError("PHASE4ES_PROBE_DIMENSION_INVALID")
        seen.add(dimension)
        candidate = _terms(probe["candidate_terms"], "PROBE_TERMS")
        changed = sorted(key for key in DIMENSIONS if baseline[key] != candidate[key])
        if changed != [dimension]:
            raise ValueError("PHASE4ES_PROBE_MUTATION_SCOPE_INVALID")
        candidate_binding = binding_hash(candidate)
        reuse_allowed = approval_matches(
            {"binding_hash": supplied_binding, "terms": baseline}, candidate
        )
        results.append(
            {
                "dimension": dimension,
                "baseline_binding_hash": supplied_binding,
                "candidate_binding_hash": candidate_binding,
                "binding_changed": candidate_binding != supplied_binding,
                "reuse_allowed": reuse_allowed,
                "reason_code": f"APPROVAL_{dimension.upper()}_CHANGED",
            }
        )
    if seen != set(DIMENSIONS):
        raise ValueError("PHASE4ES_PROBE_SET_INVALID")
    results.sort(key=lambda item: DIMENSIONS.index(item["dimension"]))
    exact_replay_matches = approval_matches(
        {"binding_hash": supplied_binding, "terms": baseline}, baseline
    )
    all_changes_prohibit_reuse = all(
        item["binding_changed"] and not item["reuse_allowed"] for item in results
    )
    if not exact_replay_matches or not all_changes_prohibit_reuse:
        raise ValueError("PHASE4ES_REUSE_PROHIBITION_NOT_PROVEN")

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4ES",
        "input_hash": payload["artifact_hash"],
        "approval_id": approval_id,
        "approval_binding_hash": supplied_binding,
        "exact_replay_matches": exact_replay_matches,
        "probe_results": results,
        "covered_dimensions": list(DIMENSIONS),
        "all_changes_prohibit_reuse": all_changes_prohibit_reuse,
        "approval_reuse_authorized": False,
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
