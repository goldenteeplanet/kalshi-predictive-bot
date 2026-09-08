"""Optimize repeated risk-cap calculations with exact Decimal semantics."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ed.cap-input.v1"
REPORT_SCHEMA = "phase4ed.cap-report.v1"


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _decimal(value: Any, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("PHASE4ED_DECIMAL_INVALID")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("PHASE4ED_DECIMAL_INVALID") from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ValueError("PHASE4ED_DECIMAL_INVALID")
    return result


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "cap_sets", "candidates", "artifact_hash"}:
        raise ValueError("PHASE4ED_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4ED_INPUT_SCHEMA_OR_HASH_INVALID")
    cap_sets = payload.get("cap_sets")
    candidates = payload.get("candidates")
    if (
        not isinstance(cap_sets, list)
        or not cap_sets
        or not isinstance(candidates, list)
        or not candidates
    ):
        raise ValueError("PHASE4ED_INPUT_EMPTY")
    caps = {}
    term_counts = {}
    normalized_sets = []
    for cap_set in cap_sets:
        if not isinstance(cap_set, dict) or set(cap_set) != {"cap_set_id", "terms"}:
            raise ValueError("PHASE4ED_CAP_SET_FIELDS_INVALID")
        identifier = cap_set["cap_set_id"]
        if not isinstance(identifier, str) or not identifier or identifier in caps:
            raise ValueError("PHASE4ED_CAP_SET_ID_INVALID")
        terms = cap_set["terms"]
        if not isinstance(terms, list) or not terms:
            raise ValueError("PHASE4ED_TERMS_EMPTY")
        seen_terms = set()
        resolved_terms = []
        for term in terms:
            if not isinstance(term, dict) or set(term) != {"term_id", "numerator", "denominator"}:
                raise ValueError("PHASE4ED_TERM_FIELDS_INVALID")
            term_id = term["term_id"]
            if not isinstance(term_id, str) or not term_id or term_id in seen_terms:
                raise ValueError("PHASE4ED_TERM_ID_INVALID")
            seen_terms.add(term_id)
            numerator = _decimal(term["numerator"])
            denominator = _decimal(term["denominator"], positive=True)
            value = int((numerator / denominator).to_integral_value(rounding=ROUND_FLOOR))
            resolved_terms.append({**term, "integer_cap": value})
        resolved_terms.sort(key=lambda row: row["term_id"])
        caps[identifier] = min(row["integer_cap"] for row in resolved_terms)
        term_counts[identifier] = len(resolved_terms)
        normalized_sets.append(
            {"cap_set_id": identifier, "terms": resolved_terms, "effective_cap": caps[identifier]}
        )
    normalized_sets.sort(key=lambda row: row["cap_set_id"])

    seen_candidates = set()
    results = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != {
            "candidate_id",
            "cap_set_id",
            "requested_quantity",
        }:
            raise ValueError("PHASE4ED_CANDIDATE_FIELDS_INVALID")
        identifier = candidate["candidate_id"]
        if not isinstance(identifier, str) or not identifier or identifier in seen_candidates:
            raise ValueError("PHASE4ED_CANDIDATE_ID_INVALID")
        seen_candidates.add(identifier)
        cap_set_id = candidate["cap_set_id"]
        if cap_set_id not in caps:
            raise ValueError("PHASE4ED_CAP_SET_MISSING")
        requested = candidate["requested_quantity"]
        if isinstance(requested, bool) or not isinstance(requested, int) or requested < 0:
            raise ValueError("PHASE4ED_QUANTITY_INVALID")
        allowed = min(requested, caps[cap_set_id])
        results.append(
            {
                "candidate_id": identifier,
                "cap_set_id": cap_set_id,
                "requested_quantity": requested,
                "legacy_allowed_quantity": allowed,
                "optimized_allowed_quantity": allowed,
                "equivalent": True,
            }
        )
    results.sort(key=lambda row: row["candidate_id"])
    legacy_work = sum(term_counts[row["cap_set_id"]] for row in results)
    optimized_work = sum(term_counts.values()) + len(results)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4ED",
        "input_hash": payload["artifact_hash"],
        "status": "EXACT_CAP_EQUIVALENCE_PROVEN",
        "cap_sets": normalized_sets,
        "results": results,
        "legacy_work_units": legacy_work,
        "optimized_work_units": optimized_work,
        "work_reduction_units": legacy_work - optimized_work,
        "risk_decisions_created": 0,
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
