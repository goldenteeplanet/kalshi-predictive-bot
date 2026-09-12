"""Bounded CPI vintage originals, separate from forecasts and settlement labels."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from kalshi_predictor.research.fred import FREDError, FREDResearchClient, FREDResponse


def normalize_cpi_vintage(
    response: FREDResponse, *, observation_start: str, observation_end: str,
    as_of: str, output_type: int,
) -> list[dict[str, Any]]:
    """Provider vintage dates never become intraday BLS publication timestamps."""
    raw = response.original.payload
    if hashlib.sha256(raw).hexdigest() != response.original.sha256:
        raise ValueError("CPI_ORIGINAL_HASH_MISMATCH")
    if json.loads(raw) != response.data or response.series_id != "CPIAUCSL":
        raise ValueError("CPI_ORIGINAL_IDENTITY_MISMATCH")
    if type(output_type) is not int or output_type not in {1, 4}:
        raise ValueError("CPI_OUTPUT_TYPE_INVALID")
    query = parse_qs(urlsplit(response.original.url).query)
    expected = {
        "series_id": "CPIAUCSL", "observation_start": observation_start,
        "observation_end": observation_end, "realtime_end": as_of,
        "realtime_start": "1776-07-04" if output_type == 4 else as_of,
        "output_type": str(output_type), "units": "lin", "offset": "0",
    }
    if any(query.get(k) != [v] for k, v in expected.items()):
        raise ValueError("CPI_REQUEST_BINDING_MISMATCH")
    start, end = date.fromisoformat(observation_start), date.fromisoformat(observation_end)
    count = (end.year - start.year) * 12 + end.month - start.month + 1
    if start.day != 1 or end.day != 1 or not 1 <= count <= 24:
        raise ValueError("CPI_MONTH_WINDOW_INVALID")
    body = response.data
    rows = body.get("observations")
    if (
        type(body.get("output_type")) is not int or body["output_type"] != output_type
        or body.get("units") != "lin" or type(body.get("count")) is not int
        or body["count"] != count or type(body.get("offset")) is not int
        or body["offset"] != 0 or not isinstance(rows, list) or len(rows) != count
    ):
        raise ValueError("CPI_INCOMPLETE_OR_WRONG_MODE")
    received = response.original.received_at
    if received.utcoffset() is None or date.fromisoformat(as_of) > received.astimezone(UTC).date():
        raise ValueError("CPI_RECEIPT_CLOCK_INVALID")
    result = []
    for index, row in enumerate(rows):
        month_index = start.year * 12 + start.month - 1 + index
        expected_period = date(month_index // 12, month_index % 12 + 1, 1)
        period = date.fromisoformat(row["date"])
        first = date.fromisoformat(row["realtime_start"])
        last = date.fromisoformat(row["realtime_end"])
        if period != expected_period or first > last or first > date.fromisoformat(as_of):
            raise ValueError("CPI_PERIOD_OR_VINTAGE_INVALID")
        if output_type == 1 and date.fromisoformat(as_of) > last:
            raise ValueError("CPI_AS_OF_VINTAGE_EXPIRED")
        if not isinstance(row["value"], str):
            raise ValueError("CPI_VALUE_INVALID")
        value = None if row["value"] == "." else Decimal(row["value"])
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("CPI_VALUE_INVALID")
        result.append({
            "series_id": "CPIAUCSL", "observation_date": period.isoformat(),
            "value": None if value is None else str(value),
            "provider_realtime_start": first.isoformat(),
            "provider_realtime_end": last.isoformat(),
            "evidence_type": "FRED_INITIAL_RELEASE_ONLY" if output_type == 4
            else "FRED_AS_OF_VINTAGE",
            "revision_status": "INITIAL_IN_FRED_HISTORY" if output_type == 4
            else "REVISION_STATUS_NOT_INFERRED",
            "received_at": received.isoformat(), "available_at": received.isoformat(),
            "release_timestamp": None, "bls_original_first_release_certified": False,
            "original_sha256": response.original.sha256,
        })
    return result


def capture_cpi_vintage_pair(
    client: FREDResearchClient, output: Path, *, observation_start: str,
    observation_end: str, as_of: str,
) -> dict[str, Any]:
    """Two GETs maximum, no retry, no database writes or forecast mutation."""
    output.mkdir(exist_ok=False)
    protocol = {
        "schema": "cpi-vintage-pair-v1", "series_id": "CPIAUCSL", "as_of": as_of,
        "observation_start": observation_start, "observation_end": observation_end,
        "maximum_requests": 2, "forecast_mutation": False, "paper_authority": False,
    }
    (output / "protocol.json").write_text(json.dumps(protocol, indent=2))
    receipts: list[dict[str, Any]] = []
    for name, mode in (("initial", 4), ("asof", 1)):
        receipt: dict[str, Any] = {"name": name, "output_type": mode, "status": "INCOMPLETE"}
        try:
            response = client.cpi_vintage(
                observation_start=observation_start, observation_end=observation_end,
                as_of=as_of, output_type=mode,
            )
            # Archive originals before schema validation, retaining rejected evidence.
            (output / (name + ".original.json")).write_bytes(response.original.payload)
            receipt.update(url=response.original.url, sha256=response.original.sha256,
                           received_at=response.original.received_at.isoformat())
            rows = normalize_cpi_vintage(
                response, observation_start=observation_start, observation_end=observation_end,
                as_of=as_of, output_type=mode,
            )
            (output / (name + ".normalized.json")).write_text(json.dumps(rows, indent=2))
            receipt.update(status="NORMALIZED_RESEARCH", rows=len(rows))
        except (FREDError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
            receipt["error_type"] = type(exc).__name__
        receipts.append(receipt)
        (output / "receipts.json").write_text(json.dumps(receipts, indent=2))
        if receipt["status"] != "NORMALIZED_RESEARCH":
            break
    complete = len(receipts) == 2 and all(r["status"] == "NORMALIZED_RESEARCH" for r in receipts)
    report = {
        "status": "CAPTURED_NOT_BLS_RELEASE_CERTIFICATION" if complete else "INCOMPLETE",
        "attempted_modes": len(receipts), "paper_authority": False, "forecast_mutated": False,
    }
    (output / "completion.json").write_text(json.dumps(report, indent=2))
    return report
