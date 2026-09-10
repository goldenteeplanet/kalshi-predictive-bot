"""Pure Miami research evidence binding; never qualification or paper preparation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from kalshi_predictor.overnight_paper.provenance import Artifact, canonical_hash
from kalshi_predictor.weather.miami_forecast import empirical_probability, forecast_miami_prior_day
from kalshi_predictor.weather.miami_index import decode_miami_index

BASE = "https://external-api.kalshi.com/trade-api/v2"
INDEX = BASE + "/live_data/weather/miami"
MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()


@dataclass(frozen=True)
class MiamiOriginal:
    """Exact response bytes and local capture metadata, not external attestation."""

    artifact: Artifact
    url: str
    received_at: datetime


@dataclass(frozen=True)
class MiamiBoundForecast:
    ticker: str
    event_ticker: str
    target_at: datetime
    threshold_f: Decimal
    probability_yes: float
    forecast_sha256: str
    recording_receipt_sha256: str
    source_hashes: tuple[str, ...]
    source_receipts: tuple[tuple[str, str, datetime], ...]
    configuration_versions: tuple[str, ...]
    rule_document_hashes: tuple[str, ...]
    historical_header_mismatches: tuple[str, ...]
    recorded_at: datetime
    decision_at: datetime
    status: str = "UNQUALIFIED"
    historical_public_availability: str = "UNKNOWN"
    paper_eligible: bool = False
    execution_authority: bool = False
    blockers: tuple[str, ...] = (
        "MODEL_RELEASE_AND_CODE_PROVENANCE_UNVERIFIED",
        "CONTRACT_RULES_AND_SETTLEMENT_HORIZON_UNCERTIFIED",
        "FEES_BOOK_UNCERTAINTY_SIZING_RISK_NOT_EVALUATED",
        "MIAMI_GUARDED_PREPARATION_NOT_CONNECTED",
    )


def _at(value: str | datetime) -> datetime:
    at = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("MIAMI_BINDING_AWARE_CLOCK_REQUIRED")
    return at.astimezone(UTC)


def _decode(artifact: Artifact) -> dict:
    artifact.decode()  # Verify the exact byte hash before interpreting JSON.

    def unique(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("MIAMI_BINDING_DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError("MIAMI_BINDING_NONFINITE_JSON")

    return json.loads(artifact.payload, object_pairs_hook=unique, parse_constant=reject_constant)


def _original(source: MiamiOriginal, now: datetime) -> dict:
    if not 0 < len(source.artifact.payload) <= 4_000_000:
        raise ValueError("MIAMI_BINDING_ORIGINAL_BUDGET")
    if _at(source.received_at) > now:
        raise ValueError("MIAMI_BINDING_FUTURE_RECEIPT")
    return _decode(source.artifact)


def bind_miami_forecast_to_contract(
    *,
    frozen_prediction: Artifact,
    recording_receipt: Artifact,
    captures: tuple[tuple[MiamiOriginal, MiamiOriginal], ...],
    market: MiamiOriginal,
    event: MiamiOriginal,
    series: MiamiOriginal,
    rule_documents: tuple[Artifact, ...],
    now: datetime,
) -> MiamiBoundForecast:
    """Replay existing frozen output and bind only exact ABOVE Miami semantics.

    Original receipt clocks are supplied evidence, not certified publication times.
    No supplied calibration/PASS flag grants authority. No I/O is performed.
    """
    decision = _at(now)
    if not 1 <= len(captures) <= 12 or not 1 <= len(rule_documents) <= 4:
        raise ValueError("MIAMI_BINDING_INPUT_BUDGET")
    if len(frozen_prediction.payload) > 8_000_000 or len(recording_receipt.payload) > 16_000:
        raise ValueError("MIAMI_BINDING_PREDICTION_BUDGET")
    saved, receipt = _decode(frozen_prediction), _decode(recording_receipt)
    if (
        saved.get("schema") != "frozen-research-prediction-v1"
        or receipt.get("schema") != "prediction-recording-receipt-v1"
        or receipt.get("prediction_sha256") != frozen_prediction.sha256
        or receipt.get("clock_authority") != "POST_FSYNC_LOCAL_CLOCK_NOT_EXTERNAL_ATTESTATION"
    ):
        raise ValueError("MIAMI_BINDING_FREEZE_RECEIPT")
    cutoff = _at(saved["model_input_as_of"])
    recorded = _at(receipt["prediction_recorded_at"])
    if not _at(saved["model_committed_at"]) <= cutoff <= recorded <= decision:
        raise ValueError("MIAMI_BINDING_FREEZE_CHRONOLOGY")
    m, e, s = (_original(item, decision) for item in (market, event, series))
    m, e, s = m["market"], e["event"], s["series"]
    ticker = m["ticker"]
    match = re.fullmatch(r"KXTEMPMIAH-(\d{2})([A-Z]{3})(\d{2})(\d{2})-T(-?\d+(?:\.\d+)?)", ticker)
    if match is None or match[2] not in MONTHS:
        raise ValueError("MIAMI_BINDING_ABOVE_TICKER_REQUIRED")
    local = datetime(
        2000 + int(match[1]),
        MONTHS.index(match[2]) + 1,
        int(match[3]),
        int(match[4]),
        tzinfo=ZoneInfo("America/New_York"),
    )
    target, threshold = local.astimezone(UTC), Decimal(match[5])
    event_id = ticker.rsplit("-", 1)[0]
    if (
        m.get("event_ticker") != event_id
        or e.get("event_ticker") != event_id
        or e.get("series_ticker") != "KXTEMPMIAH"
        or s.get("ticker") != "KXTEMPMIAH"
        or market.url != BASE + "/markets/" + ticker
        or event.url != BASE + "/events/" + event_id
        or series.url != BASE + "/series/KXTEMPMIAH"
        or _at(m["close_time"]) != target
        or decision >= target
        or m.get("strike_type") != "greater"
        or m.get("market_type") != "binary"
        or m.get("cap_strike") is not None
        or Decimal(str(m.get("floor_strike"))) != threshold
    ):
        raise ValueError("MIAMI_BINDING_CONTRACT_IDENTITY_OR_TARGET")
    # Exact observed wording avoids interpreting arbitrary prose as a certificate.
    date_label = f"{local.strftime('%b')} {local.day}, {local.year}"
    hour_label = f"{local.hour % 12 or 12} {'PM' if local.hour >= 12 else 'AM'} {local.tzname()}"
    expected = (
        f"If the temperature recorded at Miami, FL for {date_label} at {hour_label} "
        f"as reported by Synoptic Data, is above {match[5]}°, then the market resolves to Yes."
    )
    if m.get("rules_primary") != expected or "Kalshi Weather Index Methodology" not in m.get(
        "rules_secondary", ""
    ):
        raise ValueError("MIAMI_BINDING_UNSUPPORTED_RULE_SEMANTICS")
    forecasts = saved["prediction"]["forecasts"]
    if not isinstance(forecasts, list) or not 1 <= len(forecasts) <= 2:
        raise ValueError("MIAMI_BINDING_FORECAST_BUDGET")
    earliest_target = min(_at(f["target_at"]) for f in forecasts)
    if _at(saved["target_at"]) != earliest_target or recorded >= earliest_target:
        raise ValueError("MIAMI_BINDING_EARLIEST_TARGET_FREEZE")
    matching = [f for f in forecasts if _at(f["target_at"]) == target]
    if len(matching) != 1:
        raise ValueError("MIAMI_BINDING_EXACT_FORECAST_TARGET")
    forecast = matching[0]
    origin = _at(forecast["origin_at"])
    historical_mismatches: list[str] = []
    decoded, sources = [], [market, event, series]
    for index, calibration in captures:
        _original(index, decision)
        _original(calibration, decision)
        url = urlsplit(index.url)
        if (
            url.scheme + "://" + url.netloc + url.path != INDEX
            or url.fragment
            or calibration.url != INDEX + "/calibrations"
        ):
            raise ValueError("MIAMI_BINDING_CANONICAL_SOURCE_URL")
        capture = decode_miami_index(
            index.artifact.payload,
            calibration.artifact.payload,
            index_received_at=index.received_at,
            calibrations_received_at=calibration.received_at,
            index_units="fahrenheit",
        )
        if capture.latest_config_matches_last_point is False:
            if any(p.event_at >= origin for p in capture.points):
                raise ValueError("MIAMI_BINDING_CONFIGURATION_HEADER_MISMATCH")
            historical_mismatches.append(capture.index_sha256)
        decoded.append(capture)
        sources.extend((index, calibration))
    if max(c.available_at for c in decoded) != _at(saved["input_received_at"]):
        raise ValueError("MIAMI_BINDING_INPUT_RECEIPT_MISMATCH")
    replay = forecast_miami_prior_day(
        decoded,
        origin_at=_at(forecast["origin_at"]),
        model_input_as_of=cutoff,
        horizon_minutes=forecast["horizon_minutes"],
    )
    if canonical_hash(forecast) != canonical_hash(replay):
        raise ValueError("MIAMI_BINDING_FORECAST_REPLAY_MISMATCH")
    samples = replay["models"]["prior_day_increment_empirical"]["samples_f"]
    # Canonical hundredths + canonical hundredths increments need no rounding change.
    if any(Decimal(str(v)) != Decimal(str(v)).quantize(Decimal(".01")) for v in samples):
        raise ValueError("MIAMI_BINDING_NONCANONICAL_SAMPLE_PRECISION")
    probability = empirical_probability(samples, comparator="ABOVE", threshold_f=float(threshold))
    for document in rule_documents:
        if (
            not 0 < len(document.payload) <= 4_000_000
            or hashlib.sha256(document.payload).hexdigest() != document.sha256
        ):
            raise ValueError("MIAMI_BINDING_RULE_DOCUMENT_HASH")
    return MiamiBoundForecast(
        ticker,
        event_id,
        target,
        threshold,
        probability,
        frozen_prediction.sha256,
        recording_receipt.sha256,
        tuple(sorted({o.artifact.sha256 for o in sources})),
        tuple(sorted({(o.url, o.artifact.sha256, _at(o.received_at)) for o in sources})),
        tuple(sorted({p["config_version"] for p in replay["lineage"]})),
        tuple(d.sha256 for d in rule_documents),
        tuple(sorted(set(historical_mismatches))),
        recorded,
        decision,
    )
