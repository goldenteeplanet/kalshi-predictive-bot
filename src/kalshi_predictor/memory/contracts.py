from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

SCHEMA_VERSION = 1
DATA_MODE_AS_OBSERVED = "AS_OBSERVED"
DATA_MODE_RECONCILED = "RECONCILED"
INGESTION_LIVE = "LIVE"
INGESTION_BACKFILL = "BACKFILL"
INGESTION_REPLAY = "REPLAY"

MARKET_SNAPSHOT_TYPES = {
    "DECISION",
    "OPPORTUNITY",
    "RISK_CHECK",
    "ENTRY_INTENT",
    "ENTRY_FILL",
    "PERIODIC_POSITION",
    "EXIT_INTENT",
    "EXIT_FILL",
    "SETTLEMENT_PRELIMINARY",
    "SETTLEMENT_FINAL",
    "FORECAST_HORIZON",
    "CORRECTION",
}
FORECAST_EVENT_TYPES = {
    "FORECAST_REQUESTED",
    "FORECAST_CREATED",
    "FORECAST_FAILED",
    "OPPORTUNITY_SCORED",
    "OPPORTUNITY_REJECTED",
    "PHASE_3M_SIZED",
    "PHASE_3N_EVALUATED",
    "TRADE_SELECTED",
    "NO_TRADE_FINALIZED",
    "FORECAST_HORIZON_REACHED",
    "FORECAST_OUTCOME_FINALIZED",
    "FORECAST_OUTCOME_CORRECTED",
    "FORECAST_INVALIDATED",
}
TRADE_EVENT_TYPES = {
    "TRADE_INTENT_CREATED",
    "TRADE_INTENT_REJECTED",
    "ORDER_SUBMISSION_REQUESTED",
    "ORDER_ACCEPTED",
    "ORDER_REJECTED",
    "ORDER_CANCEL_REQUESTED",
    "ORDER_CANCELLED",
    "ORDER_EXPIRED",
    "PARTIAL_FILL",
    "ENTRY_FILLED",
    "POSITION_OPENED",
    "POSITION_MARKED",
    "STOP_UPDATED",
    "TARGET_UPDATED",
    "EXIT_REQUESTED",
    "EXIT_PARTIAL_FILL",
    "EXIT_FILLED",
    "POSITION_CLOSED",
    "SETTLEMENT_PRELIMINARY",
    "SETTLEMENT_FINAL",
    "SETTLEMENT_CORRECTED",
    "TRADE_OUTCOME_FINALIZED",
    "TRADE_OUTCOME_CORRECTED",
    "TRADE_INVALIDATED",
}
SECRET_KEYS = (
    "authorization",
    "api_key",
    "secret",
    "token",
    "cookie",
    "private_key",
    "password",
    "credential",
)
STORE_MARKET = "market_memory"
STORE_FORECAST = "forecast_memory"
STORE_TRADE = "trade_memory"


@dataclass(frozen=True)
class MemoryWriteReceipt:
    store: str
    status: str
    memory_event_id: str | None
    idempotency_key: str
    payload_hash: str
    message: str


def stable_id(*parts: Any) -> str:
    text = "|".join(str(part) for part in parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kalshi_predictor:phase_3o:{text}"))


def payload_hash(payload: Mapping[str, Any]) -> str:
    redacted = redact_payload(payload)
    encoded = json.dumps(
        _json_safe(redacted),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def raw_payload_hash(*payloads: str | None) -> str | None:
    joined = "\n".join(payload for payload in payloads if payload)
    if not joined:
        return None
    return f"sha256:{hashlib.sha256(joined.encode('utf-8')).hexdigest()}"


def redact_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(secret in key_text.lower() for secret in SECRET_KEYS):
                output[key_text] = "[REDACTED]"
            else:
                output[key_text] = redact_payload(item)
        return output
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_payload(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return encode_json(redact_payload(value))


def normalize_time(value: Any, *, field: str) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError(f"{field} is required.")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def optional_time(value: Any) -> datetime | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def decimal_string(value: Any) -> str | None:
    if value is None or value == "":
        return None
    decimal = to_decimal(value)
    if decimal is None:
        raise ValueError(f"Invalid decimal value: {value}")
    if not decimal.is_finite():
        raise ValueError(f"Invalid non-finite decimal value: {value}")
    return decimal_to_str(decimal)


def optional_non_negative_decimal(value: Any, *, field: str) -> str | None:
    result = decimal_string(value)
    if result is None:
        return None
    decimal = Decimal(result)
    if decimal < 0:
        raise ValueError(f"{field} cannot be negative.")
    return result


def optional_probability(value: Any, *, field: str) -> str | None:
    result = decimal_string(value)
    if result is None:
        return None
    decimal = Decimal(result)
    if decimal < 0 or decimal > 1:
        raise ValueError(f"{field} must be in [0, 1].")
    return result


def score_0_100_to_unit(value: Any) -> str | None:
    result = decimal_string(value)
    if result is None:
        return None
    decimal = Decimal(result)
    if decimal > 1:
        decimal = decimal / Decimal("100")
    return decimal_to_str(max(Decimal("0"), min(Decimal("1"), decimal)))


def quality_flags_for_quote(
    *,
    bid: Any = None,
    ask: Any = None,
    event_time: datetime | None = None,
    observed_at: datetime | None = None,
) -> list[str]:
    flags: list[str] = []
    bid_decimal = to_decimal(bid)
    ask_decimal = to_decimal(ask)
    if bid_decimal is None or ask_decimal is None:
        flags.append("MARKET_SOURCE_MISSING")
    elif ask_decimal < bid_decimal:
        flags.append("MARKET_CROSSED_QUOTE")
    if event_time is not None and observed_at is not None and observed_at < event_time:
        flags.append("MARKET_CLOCK_SKEW")
    return flags


def model_quality_flags(
    *,
    model_id: str | None,
    model_version: str | None,
    artifact_hash: str | None,
    feature_schema_version: str | None,
    code_commit_sha: str | None,
    feature_vector_hash: str | None,
) -> list[str]:
    flags: list[str] = []
    if not model_id:
        flags.append("FORECAST_MODEL_ID_MISSING")
    if not model_version or str(model_version).lower() == "latest":
        flags.append("FORECAST_MODEL_VERSION_MISSING")
    if not artifact_hash:
        flags.append("FORECAST_ARTIFACT_HASH_MISSING")
    if not feature_schema_version or not feature_vector_hash:
        flags.append("FORECAST_FEATURE_LINEAGE_MISSING")
    if not code_commit_sha:
        flags.append("FORECAST_CODE_COMMIT_MISSING")
    return flags


def local_code_commit() -> str | None:
    return os.environ.get("GIT_COMMIT_SHA") or os.environ.get("CODE_COMMIT_SHA")


def ensure_event_type(value: str, allowed: set[str], *, field: str = "event_type") -> str:
    normalized = value.strip().upper()
    if normalized not in allowed:
        raise ValueError(f"Unsupported {field}: {value}")
    return normalized


def ensure_data_mode(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in {DATA_MODE_AS_OBSERVED, DATA_MODE_RECONCILED}:
        raise ValueError("data_mode must be AS_OBSERVED or RECONCILED.")
    return normalized


def ensure_ingestion_mode(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in {INGESTION_LIVE, INGESTION_BACKFILL, INGESTION_REPLAY}:
        raise ValueError("ingestion_mode must be LIVE, BACKFILL, or REPLAY.")
    return normalized


def now_utc() -> datetime:
    return utc_now()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite Decimal values cannot be stored in memory.")
        return decimal_to_str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("NaN and infinity cannot be stored in memory.")
        return value
    try:
        if isinstance(value, str):
            Decimal(value)
    except InvalidOperation:
        return value
    return value
