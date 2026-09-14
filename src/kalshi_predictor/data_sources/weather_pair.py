"""Pure normalization of a live, receipted preparation into research originals.

This contrast compares the existing WeatherV2 model with the same book midpoint.
It is not a causal NWS ablation, a new forecast, or paper admission.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.data_sources.tournament import (
    PairedForecast,
    conservative_single_fill_fees,
    evaluate_tournament,
)
from kalshi_predictor.overnight_paper.preparation_runner import (
    FrozenWeatherExecution,
    PreparationCycle,
    _journal_value,
)
from kalshi_predictor.overnight_paper.provenance import Artifact, canonical_hash
from kalshi_predictor.overnight_paper.qualification import PUBLIC_BASE
from kalshi_predictor.overnight_paper.source_health import aware


def _artifact(row: dict[str, Any]) -> Artifact:
    raw = json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return Artifact(hashlib.sha256(raw).hexdigest(), raw)


def _build(
    *,
    cycle: PreparationCycle,
    frozen_execution: FrozenWeatherExecution,
    policy: Artifact,
    execution_policy: Artifact,
    fee_original: Artifact,
    decision_at: datetime,
    recorded_at: datetime,
    independent_event_id: str,
    event_window_start: datetime,
) -> PairedForecast:
    live = cycle.live_result
    if live is None or live.forecast_output is None:
        raise ValueError("LIVE_RECEIPTED_WEATHER_FORECAST_REQUIRED")
    record = cycle.record
    receipt = record["execution_receipt"]
    if not isinstance(receipt, dict):
        raise ValueError("LIVE_RECEIPTED_WEATHER_FORECAST_REQUIRED")
    execution, frozen = execution_policy.decode(), policy.decode()
    model, procedure = frozen_execution.model.decode(), frozen_execution.procedure.decode()
    decision, recorded = aware(decision_at), aware(recorded_at)
    if (
        record["execution_receipt_sha256"] != canonical_hash(receipt)
        or receipt["procedure_sha256"] != frozen_execution.procedure.sha256
        or receipt["model_artifact_sha256"] != frozen_execution.model.sha256
        or record["frozen_procedure_original"] != procedure
        or record["frozen_model_original"] != model
        or receipt["forecast_original"] != _journal_value(asdict(live.forecast_output))
        or record["records"] != _journal_value(live.records)
        or receipt["ticker"] != live.ticker
        or not aware(receipt["receipt_generated_at"]) <= decision <= recorded
        or frozen["kind"] != "weather-paired-source-policy-v1"
        or frozen["procedure_sha256"] != frozen_execution.procedure.sha256
        or frozen["execution_policy_sha256"] != execution_policy.sha256
        or execution["fee_rounding_original_sha256"] != fee_original.sha256
        or not isinstance(independent_event_id, str)
        or not independent_event_id.strip()
        or execution["side"] not in {"BUY_YES", "BUY_NO"}
    ):
        raise ValueError("WEATHER_PAIR_EXECUTION_BINDING_INVALID")
    captures = tuple(Artifact(s.sha256, s.payload) for s in live.source_envelopes)
    envelopes = {a.decode()["url"]: a for a in captures}
    if (
        len(envelopes) != len(captures)
        or receipt["source_envelope_hashes"] != [a.sha256 for a in captures]
        or record["original_sources"]
        != [
            {"artifact": s.artifact, "sha256": s.sha256, "original_utf8": s.payload.decode("utf-8")}
            for s in live.source_envelopes
        ]
    ):
        raise ValueError("WEATHER_PAIR_CAPTURE_BINDING_INVALID")

    def wrapper(url: str, kind: str, **extra: Any) -> Artifact:
        original = envelopes[url]
        row = original.decode()
        return _artifact(
            dict(
                kind=kind,
                request_url=url,
                received_at=row["received_at"],
                available_at=row["received_at"],
                provider_payload=row["body"],
                provider_payload_sha256=canonical_hash(row["body"]),
                capture_envelope_sha256=original.sha256,
                **extra,
            )
        )

    market = wrapper(f"{PUBLIC_BASE}/markets/{live.ticker}", "market-original-v1")
    market_row = market.decode()["provider_payload"]["market"]
    event_original = wrapper(
        f"{PUBLIC_BASE}/events/{market_row['event_ticker']}", "event-original-v1"
    )
    event = event_original.decode()["provider_payload"]["event"]
    series = wrapper(f"{PUBLIC_BASE}/series/{event['series_ticker']}", "series-original-v1")
    book_url = f"{PUBLIC_BASE}/markets/{live.ticker}/orderbook"
    book_capture = envelopes[book_url].decode()
    snapshot = wrapper(
        book_url,
        "book-original-v1",
        id=receipt["snapshot_id"],
        ticker=live.ticker,
        captured_at=book_capture["received_at"],
        clock_basis="public_rest_receipt",
    )
    station = envelopes["https://api.weather.gov/stations/KNYC"].decode()["body"]
    lon, lat = station["geometry"]["coordinates"][:2]
    route = envelopes[f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"].decode()
    hourly_url = route["body"]["properties"]["forecastHourly"]
    source = wrapper(hourly_url, "source-original-v1", source_id="NWS")
    feature = _artifact(
        dict(
            kind="feature-v1",
            source_id="NWS",
            source_original_sha256=source.sha256,
            observed_at=source.decode()["provider_payload"]["properties"]["updateTime"],
            generated_at=receipt["forecast_generated_at"],
            available_at=receipt["forecast_generated_at"],
            value=receipt["forecast_original"]["feature_json"],
        )
    )
    # Content identity of captured primary/secondary wording, not certification.
    rule_version = canonical_hash(
        {key: market_row.get(key) for key in ("rules_primary", "rules_secondary")}
    )
    rule = _artifact(
        dict(
            ticker=live.ticker,
            event_id=market_row["event_ticker"],
            rule_version=rule_version,
            series_ticker=event["series_ticker"],
            available_at=market.decode()["available_at"],
            market_original_sha256=market.sha256,
            series_original_sha256=series.sha256,
            event_original_sha256=event_original.sha256,
            settlement_certified=False,
            version_basis="CAPTURED_RULE_TEXT_SHA256",
        )
    )
    side = execution["side"]
    selected = live.records["book_qualification"]["sides"]["YES" if side == "BUY_YES" else "NO"]
    price = Decimal(selected["ask"])
    fees = conservative_single_fill_fees(
        price, Decimal(str(series.decode()["provider_payload"]["series"]["fee_multiplier"]))
    )
    receipt_original = _artifact(receipt)
    anchor = _artifact(
        dict(
            event_id=market_row["event_ticker"],
            independent_event_id=independent_event_id,
            ticker=live.ticker,
            snapshot_id=receipt["snapshot_id"],
            snapshot_sha256=snapshot.sha256,
            decision_at=decision.isoformat(),
            model_name=model["name"],
            model_version=model["version"],
            model_kind=model["model_kind"],
            training_cutoff=model["training_cutoff"],
            model_frozen_at=model["frozen_at"],
            model_artifact_sha256=frozen_execution.model.sha256,
            rule_version=rule_version,
            rule_sha256=rule.sha256,
            execution_policy_sha256=execution_policy.sha256,
            side=side,
            executable_price=str(price),
            estimated_fee=str(fees["estimated_fee"]),
            trade_fee=str(fees["trade_fee"]),
            rounding_allowance=str(fees["rounding_allowance"]),
            slippage=execution["slippage"],
            uncertainty=execution["uncertainty"],
            event_window_start=aware(event_window_start).isoformat(),
            event_window_end=live.records["analytical_target"],
            procedure_sha256=frozen_execution.procedure.sha256,
            execution_receipt_sha256=receipt_original.sha256,
        )
    )
    common = anchor.decode() | dict(
        kind="paired-forecast-v1",
        anchor_sha256=anchor.sha256,
        decision_id=canonical_hash(anchor.decode()),
        source_id="NWS",
        recorded_at=recorded.isoformat(),
    )
    off = _artifact(
        common
        | dict(
            source_enabled=False,
            probability=receipt["source_off_probability"],
            feature_hashes=[],
            generated_at=receipt["source_off_generated_at"],
        )
    )
    on = _artifact(
        common
        | dict(
            source_enabled=True,
            probability=receipt["source_on_probability"],
            feature_hashes=[feature.sha256],
            generated_at=receipt["forecast_generated_at"],
        )
    )
    pair = PairedForecast(
        anchor,
        off,
        on,
        None,
        (feature,),
        (
            snapshot,
            market,
            series,
            event_original,
            rule,
            source,
            execution_policy,
            fee_original,
            frozen_execution.procedure,
            frozen_execution.model,
            receipt_original,
            Artifact(
                hashlib.sha256(frozen_execution.model_code).hexdigest(), frozen_execution.model_code
            ),
            *captures,
        ),
    )
    if (
        evaluate_tournament(policy=policy, pairs=(pair,), as_of=recorded).status
        == "INVALID_EVIDENCE"
    ):
        raise ValueError("WEATHER_PAIR_TOURNAMENT_REJECTED")
    return pair


def build_weather_pair(
    *,
    cycle: PreparationCycle,
    frozen_execution: FrozenWeatherExecution,
    policy: Artifact,
    execution_policy: Artifact,
    fee_original: Artifact,
    decision_at: datetime,
    recorded_at: datetime,
    independent_event_id: str,
    event_window_start: datetime,
) -> PairedForecast:
    """Build pending-outcome research evidence or refuse; performs no I/O.

    The caller supplies a precommitted side in execution_policy and an explicit
    dependency cluster/window start. Forecast and baseline clocks are preserved.
    """
    try:
        return _build(
            cycle=cycle,
            frozen_execution=frozen_execution,
            policy=policy,
            execution_policy=execution_policy,
            fee_original=fee_original,
            decision_at=decision_at,
            recorded_at=recorded_at,
            independent_event_id=independent_event_id,
            event_window_start=event_window_start,
        )
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, RecursionError):
        pass
    raise ValueError("WEATHER_PAIR_ORIGINALS_INVALID")
