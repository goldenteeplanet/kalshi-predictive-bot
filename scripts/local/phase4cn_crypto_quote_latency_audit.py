"""Audit supplied crypto quote freshness, coherence, and temporal alignment offline."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cn.crypto-quote-input.v1"
REPORT_SCHEMA = "phase4cn.crypto-quote-report.v1"
MAX_QUOTES = 20_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4CN_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4CN_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("PHASE4CN_TIMESTAMP_INVALID")
    return parsed


def _ms(later: datetime, earlier: datetime) -> int:
    delta = later - earlier
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _nonnegative_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"PHASE4CN_{field.upper()}_INVALID")
    return value


def _decimal(value: Any, error: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(error) from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(error)
    return parsed


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "schema",
        "evaluated_at",
        "expected_symbols",
        "max_quote_age_ms",
        "max_source_skew_ms",
        "midpoint_tolerance",
        "quotes",
        "artifact_hash",
    }
    if set(payload) != fields:
        raise ValueError("PHASE4CN_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CN_INPUT_SCHEMA_OR_HASH_INVALID")
    evaluated_at = _time(payload["evaluated_at"])
    max_age = _nonnegative_int(payload, "max_quote_age_ms")
    max_skew = _nonnegative_int(payload, "max_source_skew_ms")
    tolerance = _decimal(payload["midpoint_tolerance"], "PHASE4CN_TOLERANCE_INVALID")
    expected = payload.get("expected_symbols")
    if (
        not isinstance(expected, list)
        or not expected
        or any(not isinstance(symbol, str) or not symbol for symbol in expected)
        or len(set(expected)) != len(expected)
    ):
        raise ValueError("PHASE4CN_EXPECTED_SYMBOLS_INVALID")
    quotes = payload.get("quotes")
    if not isinstance(quotes, list) or not quotes or len(quotes) > MAX_QUOTES:
        raise ValueError("PHASE4CN_QUOTE_COUNT_INVALID")

    identifiers: set[str] = set()
    groups: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in expected}
    rows = []
    for quote in quotes:
        required = {"quote_id", "source", "symbol", "available", "captured_at", "bid", "ask"}
        if not isinstance(quote, dict) or set(quote) != required:
            raise ValueError("PHASE4CN_QUOTE_FIELDS_INVALID")
        identifier = quote["quote_id"]
        source = quote["source"]
        symbol = quote["symbol"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4CN_QUOTE_ID_INVALID")
        if not isinstance(source, str) or not source or symbol not in groups:
            raise ValueError("PHASE4CN_SOURCE_OR_SYMBOL_INVALID")
        if not isinstance(quote["available"], bool):
            raise ValueError("PHASE4CN_AVAILABILITY_INVALID")
        captured = _time(quote["captured_at"])
        age = _ms(evaluated_at, captured)
        if age < 0:
            raise ValueError("PHASE4CN_FUTURE_QUOTE_INVALID")
        bid = _decimal(quote["bid"], "PHASE4CN_PRICE_INVALID")
        ask = _decimal(quote["ask"], "PHASE4CN_PRICE_INVALID")
        coherent = bid <= ask
        identifiers.add(identifier)
        row = {
            "quote_id": identifier,
            "source": source,
            "symbol": symbol,
            "available": quote["available"],
            "captured_at": quote["captured_at"],
            "age_ms": age,
            "freshness_status": "PASS" if age <= max_age else "STALE",
            "book_status": "PASS" if coherent else "CROSSED",
            "midpoint": str((bid + ask) / 2),
        }
        rows.append(row)
        groups[symbol].append(row)

    symbols = []
    for symbol in expected:
        available = [row for row in groups[symbol] if row["available"]]
        if not available:
            status, skew, spread = "MISSING_SYMBOL", None, None
        elif any(row["book_status"] != "PASS" for row in available):
            status, skew, spread = "INCOHERENT_QUOTE", None, None
        else:
            times = [_time(row["captured_at"]) for row in available]
            skew_value = _ms(max(times), min(times))
            midpoints = [Decimal(row["midpoint"]) for row in available]
            spread_value = max(midpoints) - min(midpoints)
            skew, spread = skew_value, str(spread_value)
            if any(row["freshness_status"] != "PASS" for row in available):
                status = "STALE_SYMBOL"
            elif skew_value > max_skew:
                status = "TEMPORALLY_MISALIGNED"
            elif len(available) > 1 and spread_value > tolerance:
                status = "PRICE_DIVERGENT"
            else:
                status = "PASS"
        symbols.append(
            {
                "symbol": symbol,
                "status": status,
                "available_source_count": len(available),
                "source_skew_ms": skew,
                "midpoint_spread": spread,
            }
        )

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CN",
        "input_hash": payload["artifact_hash"],
        "quotes": rows,
        "symbols": symbols,
        "network_calls_performed": 0,
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
    parser.add_argument("--quotes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.quotes.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
