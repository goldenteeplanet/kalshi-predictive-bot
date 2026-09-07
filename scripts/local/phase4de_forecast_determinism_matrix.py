"""Build an offline forecast determinism matrix across declared execution contexts."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4de.matrix-input.v1"
REPORT_SCHEMA = "phase4de.matrix-report.v1"
MAX_CONTEXTS = 10_000
SUPPORTED_LOCALES = {"C", "en_US.UTF-8"}
SUPPORTED_TIMEZONES = {"UTC", "America/Chicago"}


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or value.strip() != value or not value:
        raise ValueError("PHASE4DE_DECIMAL_INVALID")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("PHASE4DE_DECIMAL_INVALID") from exc
    if not parsed.is_finite():
        raise ValueError("PHASE4DE_DECIMAL_INVALID")
    return parsed


def _render(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _evaluate(rows: list[dict[str, Any]], reverse_input: bool) -> list[dict[str, str]]:
    iterable = reversed(rows) if reverse_input else iter(rows)
    results = []
    for row in iterable:
        score = (_decimal(row["probability"]) - _decimal(row["market_price"])) * _decimal(
            row["weight"]
        )
        results.append({"row_id": row["row_id"], "score": _render(score)})
    return sorted(results, key=lambda result: result["row_id"])


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    required = {"schema", "rows", "contexts", "supported_dependencies", "artifact_hash"}
    if set(payload) != required:
        raise ValueError("PHASE4DE_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DE_INPUT_SCHEMA_OR_HASH_INVALID")
    rows, contexts, supported = (
        payload["rows"],
        payload["contexts"],
        payload["supported_dependencies"],
    )
    if not isinstance(rows, list) or not rows:
        raise ValueError("PHASE4DE_ROWS_INVALID")
    row_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "row_id",
            "probability",
            "market_price",
            "weight",
        }:
            raise ValueError("PHASE4DE_ROW_FIELDS_INVALID")
        if not isinstance(row["row_id"], str) or not row["row_id"] or row["row_id"] in row_ids:
            raise ValueError("PHASE4DE_ROW_ID_INVALID")
        row_ids.add(row["row_id"])
        for field in ("probability", "market_price", "weight"):
            _decimal(row[field])
    if (
        not isinstance(supported, dict)
        or not supported
        or any(
            not isinstance(name, str)
            or not name
            or not isinstance(versions, list)
            or not versions
            or len(set(versions)) != len(versions)
            or any(not isinstance(version, str) or not version for version in versions)
            for name, versions in supported.items()
        )
    ):
        raise ValueError("PHASE4DE_SUPPORTED_DEPENDENCIES_INVALID")
    if not isinstance(contexts, list) or not contexts or len(contexts) > MAX_CONTEXTS:
        raise ValueError("PHASE4DE_CONTEXT_COUNT_INVALID")
    context_ids: set[str] = set()
    results = []
    for context in contexts:
        if not isinstance(context, dict) or set(context) != {
            "context_id",
            "repeat",
            "process_count",
            "reverse_input",
            "locale",
            "timezone",
            "dependencies",
        }:
            raise ValueError("PHASE4DE_CONTEXT_FIELDS_INVALID")
        identifier = context["context_id"]
        if not isinstance(identifier, str) or not identifier or identifier in context_ids:
            raise ValueError("PHASE4DE_CONTEXT_ID_INVALID")
        context_ids.add(identifier)
        if (
            not isinstance(context["repeat"], int)
            or isinstance(context["repeat"], bool)
            or context["repeat"] < 1
        ):
            raise ValueError("PHASE4DE_REPEAT_INVALID")
        if (
            not isinstance(context["process_count"], int)
            or isinstance(context["process_count"], bool)
            or context["process_count"] < 1
        ):
            raise ValueError("PHASE4DE_PROCESS_COUNT_INVALID")
        if not isinstance(context["reverse_input"], bool):
            raise ValueError("PHASE4DE_INPUT_ORDER_INVALID")
        if (
            context["locale"] not in SUPPORTED_LOCALES
            or context["timezone"] not in SUPPORTED_TIMEZONES
        ):
            raise ValueError("PHASE4DE_ENVIRONMENT_UNSUPPORTED")
        dependencies = context["dependencies"]
        if (
            not isinstance(dependencies, dict)
            or set(dependencies) != set(supported)
            or any(version not in supported[name] for name, version in dependencies.items())
        ):
            raise ValueError("PHASE4DE_DEPENDENCY_VERSION_UNSUPPORTED")
        output = _evaluate(rows, context["reverse_input"])
        results.append(
            {"context_id": identifier, "result_hash": canonical_hash(output), "results": output}
        )
    hashes = {result["result_hash"] for result in results}
    if len(hashes) != 1:
        raise ValueError("PHASE4DE_DETERMINISM_FAILED")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DE",
        "input_hash": payload["artifact_hash"],
        "matrix_results": results,
        "canonical_result_hash": next(iter(hashes)),
        "deterministic": True,
        "host_environment_mutated": False,
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
