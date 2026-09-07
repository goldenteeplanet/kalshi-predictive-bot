"""Audit synthetic forecast batch calculations against a scalar Decimal baseline."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dd.vectorization-input.v1"
REPORT_SCHEMA = "phase4dd.vectorization-report.v1"
MAX_ROWS = 100_000
MAX_BATCH_SIZE = 10_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("PHASE4DD_DECIMAL_INVALID")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("PHASE4DD_DECIMAL_INVALID") from exc
    if not parsed.is_finite():
        raise ValueError("PHASE4DD_DECIMAL_INVALID")
    return parsed


def _render(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _score(row: dict[str, Any]) -> str:
    probability = _decimal(row["probability"])
    market_price = _decimal(row["market_price"])
    weight = _decimal(row["weight"])
    if not (Decimal(0) <= probability <= Decimal(1)) or not (
        Decimal(0) <= market_price <= Decimal(1)
    ):
        raise ValueError("PHASE4DD_PROBABILITY_BOUNDARY_INVALID")
    return _render((probability - market_price) * weight)


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "batch_size", "rows", "artifact_hash"}:
        raise ValueError("PHASE4DD_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DD_INPUT_SCHEMA_OR_HASH_INVALID")
    batch_size = payload["batch_size"]
    rows = payload["rows"]
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or not (
        1 <= batch_size <= MAX_BATCH_SIZE
    ):
        raise ValueError("PHASE4DD_BATCH_SIZE_INVALID")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
        raise ValueError("PHASE4DD_ROW_COUNT_INVALID")
    identifiers: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "row_id",
            "probability",
            "market_price",
            "weight",
        }:
            raise ValueError("PHASE4DD_ROW_FIELDS_INVALID")
        identifier = row["row_id"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4DD_ROW_ID_INVALID")
        identifiers.add(identifier)

    scalar = [{"row_id": row["row_id"], "score": _score(row)} for row in rows]
    batches = [rows[start : start + batch_size] for start in range(0, len(rows), batch_size)]
    vectorized = [
        {"row_id": row["row_id"], "score": score}
        for batch in batches
        for row, score in zip(batch, map(_score, batch), strict=True)
    ]
    if vectorized != scalar:
        raise ValueError("PHASE4DD_VECTOR_EQUIVALENCE_FAILED")
    batch_boundaries = []
    offset = 0
    for index, batch in enumerate(batches):
        batch_boundaries.append(
            {
                "batch_index": index,
                "start_offset": offset,
                "end_offset_exclusive": offset + len(batch),
                "first_row_id": batch[0]["row_id"],
                "last_row_id": batch[-1]["row_id"],
            }
        )
        offset += len(batch)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DD",
        "input_hash": payload["artifact_hash"],
        "row_count": len(rows),
        "batch_size": batch_size,
        "batch_count": len(batches),
        "batch_boundaries": batch_boundaries,
        "results": vectorized,
        "scalar_results_hash": canonical_hash(scalar),
        "vectorized_results_hash": canonical_hash(vectorized),
        "semantic_equivalence": True,
        "scalar_operation_count": len(rows),
        "vectorized_operation_count": len(rows),
        "synthetic_benchmark_only": True,
        "forecast_records_created": 0,
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
