"""Pure CF original decoding and uncalibrated, cadence-specific process estimation.

A request binds the index because CF rows do not repeat it. Local receipt bytes
are internal provenance, not external timestamp attestation or rule authority.
Old originals without per-response receipts may be decoded offline but cannot
construct operational process inputs. No sampling conversion is performed.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

from kalshi_predictor.crypto.settlement_average_model import BenchmarkProcessInputs
from kalshi_predictor.crypto.settlement_target import SettlementBenchmarkTarget, aware

MAX_BYTES = 3_000_000
PROFILE = {"LATEST_1HZ": (1000, 3600), "HOUR_5HZ": (200, 18000)}
INDEX = {
    "BTC": "BRTI",
    "ETH": "ETHUSD_RTI",
    "SOL": "SOLUSD_RTI",
    "XRP": "XRPUSD_RTI",
    "DOGE": "DOGEUSD_RTI",
}


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(raw: bytes, sha256: str) -> dict:
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES or digest(raw) != sha256:
        raise ValueError("ORIGINAL_BYTES_OR_HASH")

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("DUPLICATE_JSON_KEY")
            out[key] = value
        return out

    def bad(_):
        raise ValueError("NONFINITE_JSON")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=bad, parse_float=bad)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("INVALID_JSON") from exc
    if type(value) is not dict:
        raise ValueError("JSON_OBJECT_REQUIRED")
    return value


def _time(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("AWARE_TIME_REQUIRED")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        aware(parsed)
    except (ValueError, TypeError) as exc:
        raise ValueError("AWARE_TIME_REQUIRED") from exc
    return parsed.astimezone(UTC)


def _request(url: str, index_id: str, profile: str) -> datetime | None:
    if (
        type(url) is not str
        or type(profile) is not str
        or type(index_id) is not str
        or profile not in PROFILE
        or index_id not in INDEX.values()
    ):
        raise ValueError("REQUEST_PROFILE")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "external-api.kalshi.com" or parts.fragment:
        raise ValueError("REQUEST_AUTHORITY")
    pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    params = dict(pairs)
    if len(params) != len(pairs) or params.get("id") != index_id:
        raise ValueError("REQUEST_INDEX")
    if profile == "LATEST_1HZ":
        if parts.path != "/trade-api/v2/cfbenchmarks/values" or set(params) != {"id"}:
            raise ValueError("LATEST_REQUEST")
        return None
    if (
        parts.path != "/trade-api/v2/cfbenchmarks/history/values"
        or set(params) != {"id", "timespan", "timestamp"}
        or params["timespan"] != "HOUR"
    ):
        raise ValueError("HISTORY_REQUEST")
    start = _time(params["timestamp"])
    if start.minute or start.second or start.microsecond:
        raise ValueError("EXACT_HOUR_REQUIRED")
    return start


@dataclass(frozen=True)
class DecodedCFOriginal:
    index_id: str
    profile: str
    cadence_ms: int
    source_sha256: str
    server_time: datetime
    timestamps_ms: tuple[int, ...]
    values: tuple[Decimal, ...]


def decode_cf_original(
    raw: bytes, *, sha256: str, request_url: str, index_id: str, profile: str
) -> DecodedCFOriginal:
    """Offline schema/coverage validation; no availability or settlement assertion."""
    start = _request(request_url, index_id, profile)
    body = _json(raw, sha256)
    if (
        set(body) != {"data"}
        or type(body["data"]) is not dict
        or set(body["data"]) != {"serverTime", "payload"}
    ):
        raise ValueError("CF_ENVELOPE_SCHEMA")
    data = body["data"]
    server = _time(data["serverTime"])
    cadence, count = PROFILE[profile]
    rows = data["payload"]
    if type(rows) is not list or len(rows) != count:
        raise ValueError("EXACT_PROFILE_COVERAGE_REQUIRED")
    times: list[int] = []
    values: list[Decimal] = []
    for row in rows:
        if type(row) is not dict or set(row) != {"time", "value"}:
            raise ValueError("CF_ROW_SCHEMA")
        time_ms, value = row["time"], row["value"]
        if (
            type(time_ms) is not int
            or time_ms < 0
            or time_ms % cadence
            or time_ms > int(server.timestamp() * 1000)
        ):
            raise ValueError("CF_ROW_TIME")
        if (
            type(value) is not str
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value)
            or len(value) > 100
        ):
            raise ValueError("CF_DECIMAL_VALUE")
        price = Decimal(value)
        if price <= 0 or not math.isfinite(float(price)):
            raise ValueError("CF_POSITIVE_VALUE_REQUIRED")
        if times and time_ms != times[-1] + cadence:
            raise ValueError("CF_CADENCE_OR_DUPLICATE")
        times.append(time_ms)
        values.append(price)
    if start is not None and times[0] != int(start.timestamp() * 1000):
        raise ValueError("HISTORY_WINDOW_MISMATCH")
    return DecodedCFOriginal(
        index_id, profile, cadence, sha256, server, tuple(times), tuple(values)
    )


@dataclass(frozen=True)
class CFProcessEstimate:
    process: BenchmarkProcessInputs
    evidence: dict


def estimate_cf_process(
    raw: bytes,
    receipt_raw: bytes,
    *,
    source_sha256: str,
    receipt_sha256: str,
    target: SettlementBenchmarkTarget,
    as_of: datetime,
) -> CFProcessEstimate:
    """Estimate from every contiguous raw tick; no imputation or tick selection."""
    aware(as_of)
    if type(target) is not SettlementBenchmarkTarget:
        raise ValueError("EXACT_TARGET_REQUIRED")
    target.validate(as_of=as_of)
    receipt = _json(receipt_raw, receipt_sha256)
    coverage_receipt = "schema" not in receipt
    original_receipt = dict(receipt)
    if coverage_receipt:
        coverage_keys = {
            "index",
            "classification",
            "request_count",
            "request_started_at",
            "received_at",
            "http_status",
            "url",
            "sha256",
            "observations",
            "duplicates",
            "monotonic",
            "step_counts_ms",
            "first_timestamp",
            "last_timestamp",
            "parse_errors",
            "terminal",
        }
        if set(receipt) != coverage_keys or receipt["classification"] != "ACCESSIBLE":
            raise ValueError("CF_COVERAGE_RECEIPT_SCHEMA")
        receipt = dict(
            schema="cf-response-receipt-v1",
            method="GET",
            url=receipt["url"],
            index_id=receipt["index"],
            profile="LATEST_1HZ",
            http_status=receipt["http_status"],
            source_sha256=receipt["sha256"],
            requested_at=receipt["request_started_at"],
            received_at=receipt["received_at"],
            recorded_at=None,
        )
    required = {
        "schema",
        "method",
        "url",
        "index_id",
        "profile",
        "http_status",
        "source_sha256",
        "requested_at",
        "received_at",
        "recorded_at",
    }
    if (
        set(receipt) != required
        or receipt["schema"] != "cf-response-receipt-v1"
        or receipt["method"] != "GET"
        or type(receipt["http_status"]) is not int
        or receipt["http_status"] != 200
        or receipt["source_sha256"] != source_sha256
    ):
        raise ValueError("CF_RECEIPT_SCHEMA_OR_BINDING")
    requested, received = (_time(receipt[k]) for k in ("requested_at", "received_at"))
    recorded = None if coverage_receipt else _time(receipt["recorded_at"])
    if not requested <= received <= as_of or (
        recorded is not None and not received <= recorded <= as_of
    ):
        raise ValueError("CF_RECEIPT_CLOCK")
    if (
        INDEX.get(target.symbol) != target.rules.index_id
        or receipt["index_id"] != target.rules.index_id
    ):
        raise ValueError("CF_TARGET_INDEX")
    decoded = decode_cf_original(
        raw,
        sha256=source_sha256,
        request_url=receipt["url"],
        index_id=receipt["index_id"],
        profile=receipt["profile"],
    )
    if coverage_receipt:
        expected = dict(
            request_count=1,
            observations=len(decoded.values),
            duplicates=0,
            monotonic=True,
            step_counts_ms={str(decoded.cadence_ms): len(decoded.values) - 1},
            parse_errors=0,
            terminal=True,
        )
        actual = {k: original_receipt[k] for k in expected}
        if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
            raise ValueError("CF_RECEIPT_COVERAGE_BINDING")
        for key, time_ms in (
            ("first_timestamp", decoded.timestamps_ms[0]),
            ("last_timestamp", decoded.timestamps_ms[-1]),
        ):
            if _time(original_receipt[key]) != datetime.fromtimestamp(time_ms / 1000, UTC):
                raise ValueError("CF_RECEIPT_COVERAGE_TIME")
    observed = datetime.fromtimestamp(decoded.timestamps_ms[-1] / 1000, UTC)
    if not observed <= decoded.server_time <= received or not timedelta(
        0
    ) <= as_of - observed <= timedelta(seconds=60):
        raise ValueError("CF_FUTURE_OR_STALE_INPUT")
    logs = [math.log(float(p)) for p in decoded.values]
    returns = [b - a for a, b in zip(logs, logs[1:], strict=False)]
    mean = math.fsum(returns) / len(returns)
    variance = math.fsum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    sigma = math.sqrt(variance / (decoded.cadence_ms / 60000))
    if not math.isfinite(sigma) or (
        all(r == 0 for r in returns) and any(p != decoded.values[0] for p in decoded.values)
    ):
        raise ValueError("VOLATILITY_NUMERIC_RANGE")
    process = BenchmarkProcessInputs(
        target.symbol,
        decoded.index_id,
        decoded.values[-1],
        observed,
        received,
        raw,
        source_sha256,
        "DECLARED_CF_OBSERVATION_UNVERIFIED",
        sigma,
        observed,
        received,
        raw,
        source_sha256,
    )
    process.validate(target, as_of)
    return CFProcessEstimate(
        process,
        dict(
            schema="cf-process-estimate-v1",
            source_sha256=source_sha256,
            receipt_sha256=receipt_sha256,
            estimator="CF_SAMPLE_LOG_VARIANCE_PER_MINUTE_" + decoded.profile + "_V1",
            cadence_ms=decoded.cadence_ms,
            prices=len(decoded.values),
            returns=len(returns),
            elapsed_minutes=(decoded.timestamps_ms[-1] - decoded.timestamps_ms[0]) / 60000,
            request_url=receipt["url"],
            requested_at=requested.isoformat(),
            received_at=received.isoformat(),
            recorded_at=recorded.isoformat() if recorded is not None else None,
            server_time=decoded.server_time.isoformat(),
            model_input_as_of=as_of.isoformat(),
            provider_index_binding="REQUEST_PARAMETER_NOT_ROW_FIELD",
            raw_value_semantics="CF_OBSERVATIONS_NOT_CERTIFIED_SETTLEMENT_TICKS",
            downsampling=False,
            calibration_status="UNCALIBRATED_CADENCE_SPECIFIC_RESEARCH_ESTIMATOR",
            independent_returns=False,
            receipt_authority="INTERNAL_ORIGINAL_BINDING_NOT_EXTERNAL_ATTESTATION",
        ),
    )
