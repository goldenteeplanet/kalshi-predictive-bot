"""Pure, separately declared recent level evidence; never process/window evidence.

Short contiguous originals are permitted only for price-only contract selection.
Existing exact-count decoders and event selectors remain unchanged. Hashes and
local clocks bind custody, not external authentication or timestamp attestation.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from kalshi_predictor.crypto.cf_process_inputs import INDEX, _json, _request, _time, digest
from kalshi_predictor.crypto.settlement_target import aware

VERSION = "CURRENT_CF_LEVEL_SELECTION_V1"
PROFILE = "CF_RECENT_LEVEL_ONLY_SELECTION_V1"
PURPOSE = "PRICE_ONLY_CONTRACT_SELECTION"
MAX_BYTES = 500_000


@dataclass(frozen=True)
class CFSelectionLevel:
    version: str
    profile: str
    purpose: str
    asset: str
    index_id: str
    event_ticker: str
    source_commit_binding: str
    level: Decimal
    observed_at: datetime
    available_at: datetime
    assessed_at: datetime
    first_observed_at: datetime
    observation_count: int
    elapsed_seconds: int
    source_sha256: str
    receipt_sha256: str
    protocol_sha256: str
    body_original: bytes
    receipt_original: bytes
    protocol_original: bytes
    forecast_input_eligible: bool = False
    complete_forecast_grid: bool = False
    volatility_estimated: bool = False
    source_commit_attested: bool = False
    external_timestamp_attestation: bool = False
    paper_eligible: bool = False
    execution_authority: bool = False


def prepare_cf_selection_level(
    *,
    body: bytes,
    receipt: bytes,
    protocol_original: bytes,
    protocol_sha256: str,
    expected_asset: str,
    expected_event_ticker: str,
    expected_source_commit: str,
    as_of: datetime,
) -> CFSelectionLevel:
    """Validate all rows but derive only latest level, never variance or returns.

    Expected event/source identities are caller context bindings. A controller
    must independently verify actual event metadata, source checkout and receipt
    custody; this function neither discovers markets nor makes network requests.
    """
    aware(as_of)
    if (
        type(expected_asset) is not str
        or expected_asset not in INDEX
        or type(expected_event_ticker) is not str
        or not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,199}", expected_event_ticker)
        or type(expected_source_commit) is not str
        or not re.fullmatch(r"[0-9a-f]{40}", expected_source_commit)
    ):
        raise ValueError("LEVEL_EXPECTED_CONTEXT")
    if type(protocol_original) is not bytes or not 0 < len(protocol_original) <= 10_000:
        raise ValueError("LEVEL_PROTOCOL_SIZE")
    plan = _json(protocol_original, protocol_sha256)
    if set(plan) != {
        "version",
        "profile",
        "purpose",
        "asset",
        "index_id",
        "event_ticker",
        "source_commit",
        "declared_at",
        "not_before",
        "not_after",
        "max_cf_gets",
        "retries",
        "paper_eligible",
        "execution_authority",
    } or (
        plan["version"] != VERSION
        or plan["profile"] != PROFILE
        or plan["purpose"] != PURPOSE
        or plan["asset"] != expected_asset
        or plan["index_id"] != INDEX[expected_asset]
        or plan["event_ticker"] != expected_event_ticker
        or plan["source_commit"] != expected_source_commit
        or type(plan["max_cf_gets"]) is not int
        or plan["max_cf_gets"] != 1
        or type(plan["retries"]) is not int
        or plan["retries"] != 0
        or plan["paper_eligible"] is not False
        or plan["execution_authority"] is not False
    ):
        raise ValueError("LEVEL_PROTOCOL_IDENTITY")
    declared, begin, end = (_time(plan[k]) for k in ("declared_at", "not_before", "not_after"))
    if not declared <= begin <= as_of < end or not timedelta(0) < end - begin <= timedelta(
        seconds=180
    ):
        raise ValueError("LEVEL_PROTOCOL_CLOCK")
    if type(body) is not bytes or not 0 < len(body) <= MAX_BYTES:
        raise ValueError("LEVEL_BODY_SIZE")
    if type(receipt) is not bytes or not 0 < len(receipt) <= 10_000:
        raise ValueError("LEVEL_RECEIPT_SIZE")
    source_hash = digest(body)
    receipt_hash = digest(receipt)
    rec = _json(receipt, receipt_hash)
    if set(rec) != {
        "schema",
        "profile",
        "purpose",
        "index_id",
        "protocol_sha256",
        "method",
        "url",
        "http_status",
        "original_complete",
        "source_sha256",
        "requested_at",
        "received_at",
        "recorded_at",
    } or (
        rec["schema"] != "cf-level-selection-receipt-v1"
        or rec["profile"] != PROFILE
        or rec["purpose"] != PURPOSE
        or rec["index_id"] != plan["index_id"]
        or rec["protocol_sha256"] != protocol_sha256
        or rec["method"] != "GET"
        or type(rec["http_status"]) is not int
        or rec["http_status"] != 200
        or rec["original_complete"] is not True
        or rec["source_sha256"] != source_hash
    ):
        raise ValueError("LEVEL_RECEIPT_BINDING")
    # Share request authority validation only; do not invoke/alter the old decoder.
    _request(rec["url"], plan["index_id"], "LATEST_1HZ")
    requested, received, recorded = (
        _time(rec[k]) for k in ("requested_at", "received_at", "recorded_at")
    )
    if not declared <= begin <= requested <= received <= recorded <= as_of:
        raise ValueError("LEVEL_RECEIPT_CLOCK")
    data = _json(body, source_hash)
    if (
        set(data) != {"data"}
        or type(data["data"]) is not dict
        or set(data["data"]) != {"serverTime", "payload"}
    ):
        raise ValueError("CF_ENVELOPE_SCHEMA")
    server = _time(data["data"]["serverTime"])
    rows = data["data"]["payload"]
    if type(rows) is not list or not 1 <= len(rows) <= 3600:
        raise ValueError("LEVEL_BOUNDED_ROW_COUNT")
    timestamps: list[int] = []
    values: list[Decimal] = []
    for row in rows:
        if type(row) is not dict or set(row) != {"time", "value"}:
            raise ValueError("CF_ROW_SCHEMA")
        stamp, raw_value = row["time"], row["value"]
        if (
            type(stamp) is not int
            or stamp < 0
            or stamp % 1000
            or stamp > int(server.timestamp() * 1000)
        ):
            raise ValueError("CF_ROW_TIME")
        if timestamps and stamp != timestamps[-1] + 1000:
            raise ValueError("CF_CADENCE_OR_DUPLICATE")
        if (
            type(raw_value) is not str
            or len(raw_value) > 100
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw_value)
        ):
            raise ValueError("CF_DECIMAL_VALUE")
        value = Decimal(raw_value)
        if value <= 0 or not math.isfinite(float(value)):
            raise ValueError("CF_POSITIVE_VALUE_REQUIRED")
        timestamps.append(stamp)
        values.append(value)
    observed = datetime.fromtimestamp(timestamps[-1] / 1000, tz=as_of.tzinfo)
    first = datetime.fromtimestamp(timestamps[0] / 1000, tz=as_of.tzinfo)
    if not observed <= server <= received or not timedelta(0) <= as_of - observed <= timedelta(
        seconds=60
    ):
        raise ValueError("CF_FUTURE_OR_STALE_INPUT")
    # Latest original row only: no mean, return, volatility or complete-hour claim.
    return CFSelectionLevel(
        VERSION,
        PROFILE,
        PURPOSE,
        expected_asset,
        plan["index_id"],
        expected_event_ticker,
        expected_source_commit,
        values[-1],
        observed,
        recorded,
        as_of,
        first,
        len(rows),
        (timestamps[-1] - timestamps[0]) // 1000,
        source_hash,
        receipt_hash,
        protocol_sha256,
        body,
        receipt,
        protocol_original,
    )


def replay_cf_selection_level(
    evidence: CFSelectionLevel,
    *,
    expected_asset: str,
    expected_event_ticker: str,
    expected_source_commit: str,
    as_of: datetime,
) -> CFSelectionLevel:
    """Reject forged typed fields and independently recheck current freshness."""
    if type(evidence) is not CFSelectionLevel:
        raise ValueError("EXACT_SELECTION_LEVEL_REQUIRED")
    aware(as_of)
    if as_of < evidence.assessed_at:
        raise ValueError("LEVEL_REPLAY_VISIBILITY")

    def replay(clock: datetime) -> CFSelectionLevel:
        return prepare_cf_selection_level(
            body=evidence.body_original,
            receipt=evidence.receipt_original,
            protocol_original=evidence.protocol_original,
            protocol_sha256=evidence.protocol_sha256,
            expected_asset=expected_asset,
            expected_event_ticker=expected_event_ticker,
            expected_source_commit=expected_source_commit,
            as_of=clock,
        )

    original = replay(evidence.assessed_at)
    if original != evidence:
        raise ValueError("LEVEL_DERIVED_FIELDS_MISMATCH")
    return replay(as_of)
