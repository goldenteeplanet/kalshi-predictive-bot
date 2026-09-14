"""Bounded weather computation using existing model/services; never paper admission.

Original envelopes survive normalization. Every result is diagnostic until the
independent rule/model/release qualification is supplied by the coordinator.
The caller owns its SQLite transaction; this module never commits or creates an
order, fill or risk reservation, and accepts no transport or executable callback.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.advanced_risk.engine import (
    AdvancedRiskConfig,
    AdvancedRiskDecision,
    AdvancedRiskEngine,
    AdvancedRiskRequest,
)
from kalshi_predictor.advanced_risk.repository import insert_advanced_risk_decision
from kalshi_predictor.advanced_risk.service import advanced_risk_request_for_paper_decision
from kalshi_predictor.config import Settings
from kalshi_predictor.data.repositories import decode_json, insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import Market, MarketSnapshot, WeatherForecast
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.forecasting.weather_v2 import WeatherV2Forecaster
from kalshi_predictor.opportunities.scoring import score_liquidity
from kalshi_predictor.overnight_paper.books import qualify_book
from kalshi_predictor.overnight_paper.qualification import (
    PUBLIC_BASE,
    EvidenceReference,
    compute_net_ev,
)
from kalshi_predictor.overnight_paper.source_health import aware, classify_source
from kalshi_predictor.paper.models import BUY_NO, BUY_YES, PaperDecision
from kalshi_predictor.position_sizing.service import size_paper_decision
from kalshi_predictor.position_sizing.sizer import PositionSizingDecision
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.features import calculate_weather_features
from kalshi_predictor.weather.ingestion import store_weather_fetch_result
from kalshi_predictor.weather.linker import detect_weather_market
from kalshi_predictor.weather.providers import WeatherFetchResult, parse_noaa_hourly_forecast
from kalshi_predictor.weather.repository import insert_weather_features, insert_weather_market_link
from kalshi_predictor.weather.temperature_contracts import (
    parse_point_temperature_ticker,
    validate_point_temperature_market,
)


@dataclass(frozen=True)
class WeatherPreparationResult:
    state: str
    ticker: str
    source_envelopes: tuple[EvidenceReference, ...]
    blockers: tuple[str, ...]
    records: dict[str, Any]
    decision: PaperDecision | None = None
    phase3m: PositionSizingDecision | None = None
    phase3n: AdvancedRiskDecision | None = None
    risk_request: AdvancedRiskRequest | None = None
    forecast_output: ForecastOutput | None = None


def _latest_snapshot_id(session: Session, ticker: str, *, at: datetime | None = None) -> int | None:
    query = select(MarketSnapshot).where(MarketSnapshot.ticker == ticker)
    if at is not None:
        query = query.where(MarketSnapshot.captured_at <= at)
    row = session.scalar(
        query.order_by(MarketSnapshot.captured_at.desc(), MarketSnapshot.id.desc()).limit(1)
    )
    return None if row is None else row.id


def _source_rows(envelopes: tuple[EvidenceReference, ...], now: datetime) -> dict[str, dict]:
    if not 1 <= len(envelopes) <= 12:
        raise ValueError("BOUNDED_ORIGINAL_ENVELOPES_REQUIRED")
    rows = {}
    for source in envelopes:
        if not source.valid() or len(source.payload) > 1_000_000:
            raise ValueError("ORIGINAL_ENVELOPE_INTEGRITY")
        row = json.loads(source.payload)
        url = row["url"]
        if url in rows:
            raise ValueError("DUPLICATE_SOURCE_URL")
        receipt = aware(row["received_at"])
        if not 0 <= (now - receipt).total_seconds() <= 60:
            raise ValueError("STALE_OR_FUTURE_RECEIPT")
        if not isinstance(row["body"], dict):
            raise ValueError("ORIGINAL_SOURCE_BODY_REQUIRED")
        rows[url] = row
    return rows


def prepare_weather_candidate(
    session: Session,
    *,
    ticker: str,
    source_envelopes: tuple[EvidenceReference, ...],
    settings: Settings,
    slippage_allowance: Decimal,
    uncertainty_buffer: Decimal,
    fee_evidence: dict[str, Any] | None = None,
) -> WeatherPreparationResult:
    """Compute one real-model diagnostic from original captured public inputs.

    Retains true execution/availability times instead of the legacy forecaster's
    snapshot-time label. Rejection never implies source/rule/model certification.
    """
    records: dict[str, Any] = {}
    try:
        if type(settings) is not Settings:
            raise ValueError("CONCRETE_SETTINGS_REQUIRED")
        if (
            settings.execution_enabled
            or not settings.execution_dry_run
            or not settings.execution_kill_switch
            or settings.execution_gateway_mode != "disabled"
            or settings.autopilot_enabled
        ):
            raise ValueError("LOCAL_ONLY_SETTINGS_REQUIRED")
        if any(
            not value.is_finite() or value < 0 for value in (slippage_allowance, uncertainty_buffer)
        ):
            raise ValueError("FINITE_NONNEGATIVE_COST_ALLOWANCES_REQUIRED")
        if slippage_allowance < settings.advanced_risk_estimated_slippage_per_contract:
            raise ValueError("SLIPPAGE_BELOW_RISK_CONFIGURATION")
        if uncertainty_buffer < settings.advanced_risk_gap_tail_buffer_per_contract:
            raise ValueError("UNCERTAINTY_BELOW_RISK_CONFIGURATION")
        if session.get_bind().dialect.name != "sqlite":
            raise ValueError("SQLITE_PREPARATION_REQUIRED")
        started = utc_now()
        rows = _source_rows(source_envelopes, started)
        market = rows[f"{PUBLIC_BASE}/markets/{ticker}"]["body"]["market"]
        if market["ticker"] != ticker or market["status"] not in {"open", "active"}:
            raise ValueError("CURRENT_EXACT_MARKET_REQUIRED")
        close = aware(market["close_time"])
        if close <= started:
            raise ValueError("MARKET_CLOSED")
        event = rows[f"{PUBLIC_BASE}/events/{market['event_ticker']}"]["body"]["event"]
        series = rows[f"{PUBLIC_BASE}/series/{event['series_ticker']}"]["body"]["series"]
        if (
            event["event_ticker"] != market["event_ticker"]
            or series["ticker"] != event["series_ticker"]
            or series["category"] != "Climate and Weather"
        ):
            raise ValueError("CATALOG_IDENTITY_MISMATCH")
        if "(for coordinates KNYC)" not in market.get("rules_primary", ""):
            raise ValueError("UNSUPPORTED_STATION_IDENTITY")
        station = rows["https://api.weather.gov/stations/KNYC"]["body"]
        if station["properties"]["stationIdentifier"] != "KNYC":
            raise ValueError("STATION_IDENTITY_MISMATCH")
        lon, lat = station["geometry"]["coordinates"][:2]
        hourly_url = rows[f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"]["body"][
            "properties"
        ]["forecastHourly"]
        if not re.fullmatch(
            r"https://api\.weather\.gov/gridpoints/OKX/\d+,\d+/forecast/hourly", hourly_url
        ):
            raise ValueError("UNSUPPORTED_FORECAST_ROUTE")
        hourly = rows[hourly_url]["body"]
        properties = hourly["properties"]
        # Validate both provider clocks before the parser's legacy fallback.
        if not properties.get("generatedAt") or not properties.get("updateTime"):
            raise ValueError("PROVIDER_CLOCK_REQUIRED")
        generated, updated = aware(properties["generatedAt"]), aware(properties["updateTime"])
        if generated > aware(rows[hourly_url]["received_at"]) or updated > generated:
            raise ValueError("INCONSISTENT_PROVIDER_VISIBILITY")
        contract = parse_point_temperature_ticker(ticker)
        if contract is None or contract.contract_kind != "ABOVE":
            raise ValueError("EXACT_TEMPERATURE_CONTRACT_REQUIRED")
        metadata = validate_point_temperature_market(
            contract, market, series_scope=series["ticker"]
        )
        if not metadata.passed:
            raise ValueError("TEMPERATURE_METADATA_MISMATCH:" + ",".join(metadata.blockers))
        # Ticker grammar supplies the local observation hour. Independently bind
        # that hour to the primary rule, never to occurrence/expiration fields.
        local = contract.target_local_time
        wording = re.findall(
            r"\bfor\s+([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{4})\s+"
            r"(\d{1,2})\s+(AM|PM)\s+(EDT|EST)\s+as reported by\b",
            market.get("rules_primary", ""),
            flags=re.IGNORECASE,
        )
        expected = (
            local.strftime("%b").lower(),
            str(local.day),
            str(local.year),
            str(local.hour % 12 or 12),
            "am" if local.hour < 12 else "pm",
            str(local.tzname()).lower(),
        )
        if len(wording) != 1 or tuple(part.lower() for part in wording[0]) != expected:
            raise ValueError("PRIMARY_RULE_OBSERVATION_TIME_MISMATCH")
        target = contract.target_utc_time
        if event.get("strike_date") is not None and aware(event["strike_date"]) != target:
            raise ValueError("EVENT_OBSERVATION_TIME_MISMATCH")
        records["analytical_observation_evidence"] = {
            "basis": "EXACT_TEMPERATURE_TICKER_AND_PRIMARY_RULE",
            "observation_time": target.isoformat(),
            "local_observation_time": local.isoformat(),
            "close_time": market["close_time"],
            "occurrence_datetime": market.get("occurrence_datetime"),
            "expected_expiration_time": market.get("expected_expiration_time"),
            "settlement_certified": False,
        }
        periods = [
            period for period in properties["periods"] if aware(period["startTime"]) == target
        ]
        if len(periods) != 1:
            raise ValueError("EXACT_TARGET_FORECAST_PERIOD_REQUIRED")
        period = periods[0]
        health = classify_source(
            generated_at=generated,
            updated_at=updated,
            valid_from=period["startTime"],
            valid_to=period["endTime"],
            target_start=target,
            target_end=target + timedelta(seconds=1),
            now=started,
            payload_hash=next(
                s.sha256 for s in source_envelopes if json.loads(s.payload)["url"] == hourly_url
            ),
        )
        if not health.eligible:
            raise ValueError(health.reason)
        book_row = rows[f"{PUBLIC_BASE}/markets/{ticker}/orderbook"]
        receipt = aware(book_row["received_at"])
        latest = session.scalar(
            select(MarketSnapshot)
            .where(MarketSnapshot.ticker == ticker)
            .order_by(MarketSnapshot.captured_at.desc(), MarketSnapshot.id.desc())
            .limit(1)
        )
        if (
            latest is not None
            and aware(
                latest.captured_at
                if latest.captured_at.tzinfo is not None
                else latest.captured_at.replace(tzinfo=UTC)
            )
            > receipt
        ):
            raise ValueError("SNAPSHOT_SELECTION_MISMATCH")
        liquidity = score_liquidity(
            volume=market.get("volume_fp"),
            open_interest=market.get("open_interest_fp"),
            liquidity=market.get("liquidity_dollars"),
        )
        book = qualify_book(
            book_row["body"],
            received_at=receipt,
            now=started,
            max_spread=settings.opportunity_max_spread,
            liquidity_score=liquidity,
            price_ranges=market.get("price_ranges"),
        )
        if not book["executable"]:
            raise ValueError("NO_EXECUTABLE_BOOK")
        normalized_market = dict(market, series_ticker=series["ticker"])
        snapshot = insert_market_snapshot(session, normalized_market, book_row["body"], receipt)
        records.update(
            snapshot_id=snapshot.id,
            book=book_row["body"],
            book_qualification=book,
            source_health=asdict(health),
            analytical_target=target.isoformat(),
        )
        parsed = parse_noaa_hourly_forecast(
            location_key="new_york",
            latitude=lat,
            longitude=lon,
            payload={"properties": dict(properties, periods=[period])},
        )
        store_weather_fetch_result(session, WeatherFetchResult("noaa", parsed, []))
        stored = session.scalar(
            select(WeatherForecast)
            .where(
                WeatherForecast.location_key == "new_york",
                WeatherForecast.source == "noaa",
                WeatherForecast.forecast_time == target,
                WeatherForecast.forecast_generated_at == generated,
            )
            .order_by(WeatherForecast.id.desc())
            .limit(1)
        )
        if stored is None or decode_json(stored.raw_json) != period:
            raise ValueError("NORMALIZED_SOURCE_LINEAGE_MISMATCH")
        feature_time = utc_now()
        features = calculate_weather_features(stored, generated_at=feature_time)
        feature = insert_weather_features(
            session,
            location_key="new_york",
            source="stored_forecasts",
            generated_at=feature_time,
            target_time=target,
            features=features,
            raw_json=features,
        )
        feature_available_at = utc_now()
        market_record = session.get(Market, ticker)
        if market_record is None:
            raise ValueError("PERSISTED_MARKET_REQUIRED")
        detection = detect_weather_market(market_record)
        if (
            not detection.point_forecast_eligible
            or detection.station_id != "KNYC"
            or detection.weather_metric != "TEMPERATURE"
            or detection.target_value is None
        ):
            raise ValueError("EXISTING_WEATHER_LINKER_UNSUPPORTED")
        link = insert_weather_market_link(
            session,
            ticker=ticker,
            location_key=detection.location_key,
            weather_metric=detection.weather_metric,
            target_operator=detection.target_operator,
            target_value=detection.target_value,
            target_time=target,
            confidence=detection.confidence,
            reason=detection.reason,
            raw_json={
                "ticker": ticker,
                "raw_market": normalized_market,
                "station_id": detection.station_id,
                "point_forecast_eligible": True,
                "classification": detection.classification,
            },
        )
        records["weather_link_id"] = link.id
        output = WeatherV2Forecaster(settings).forecast(session, snapshot)
        if output is None:
            raise ValueError("EXISTING_WEATHER_MODEL_SKIPPED")
        if output.feature_json.get("weather_feature_id") != feature.id:
            raise ValueError("MODEL_FEATURE_SELECTION_MISMATCH")
        generated_at = utc_now()
        output = replace(output, forecasted_at=generated_at)
        forecast = insert_forecast(
            session, output, market_snapshot_id=snapshot.id, attribution_enabled=True
        )
        available_at = utc_now()
        records.update(
            feature_id=feature.id,
            source_forecast_id=stored.id,
            features=decode_json(feature.raw_json),
            feature_generated_at=feature_time.isoformat(),
            feature_available_at=feature_available_at.isoformat(),
            forecast_id=forecast.id,
            forecast=asdict(output),
            forecast_generated_at=generated_at.isoformat(),
            forecast_available_at=available_at.isoformat(),
        )
        if (
            _latest_snapshot_id(session, ticker, at=generated_at) != snapshot.id
            or _latest_snapshot_id(session, ticker) != snapshot.id
        ):
            raise ValueError("SNAPSHOT_SELECTION_MISMATCH")
        from kalshi_predictor.paper.fees import CONTRACT_KEY, build_fee_quote

        alternatives = []
        for side, key, probability in (
            (BUY_YES, "YES", output.yes_probability),
            (BUY_NO, "NO", 1 - output.yes_probability),
        ):
            if side == BUY_NO and not settings.paper_allow_buy_no:
                continue
            if book["sides"][key]["executable"]:
                quote = (
                    None
                    if fee_evidence is None
                    else build_fee_quote(
                        evidence=fee_evidence,
                        ticker=ticker,
                        side=side,
                        price=Decimal(book["sides"][key]["ask"]),
                        simulator_floor=settings.paper_default_fee_per_contract,
                        now=utc_now(),
                    )
                )
                ev = compute_net_ev(
                    model_probability=probability,
                    executable_price=Decimal(book["sides"][key]["ask"]),
                    estimated_fee=settings.paper_default_fee_per_contract
                    if quote is None
                    else quote.charge,
                    slippage_allowance=slippage_allowance,
                    uncertainty_buffer=uncertainty_buffer,
                )
                alternatives.append((ev.net_ev, side, ev, quote))
        if not alternatives:
            raise ValueError("NO_ALLOWED_EXECUTABLE_SIDE")
        _, side, ev, fee_quote = max(alternatives, key=lambda item: item[0])
        decision = PaperDecision(
            ticker,
            forecast.id,
            output.model_name,
            side,
            output.yes_probability,
            ev.executable_price,
            ev.executable_price,
            ev.gross_edge,
            1,
            "Computed diagnostic; independent release gates required",
            {} if fee_quote is None else {CONTRACT_KEY: fee_quote.decode()},
        )
        decision_at = utc_now()
        sizing = size_paper_decision(
            session, decision=decision, settings=settings, decision_timestamp=decision_at
        )
        request = advanced_risk_request_for_paper_decision(
            session,
            decision=decision,
            settings=settings,
            phase_3m_decision=sizing.decision,
            decision_timestamp=decision_at,
        )
        risk = AdvancedRiskEngine(AdvancedRiskConfig.from_settings(settings)).decide(request)
        risk_log = insert_advanced_risk_decision(
            session,
            risk,
            request,
            ticker=ticker,
            position_sizing_decision_id=sizing.record_id,
            raw={}
            if fee_quote is None
            else {
                "guarded_fee_quote_sha256": fee_quote.sha256,
                "estimated_round_trip_fees": str(request.estimated_round_trip_fees),
            },
        )
        if (
            _latest_snapshot_id(session, ticker, at=generated_at) != snapshot.id
            or _latest_snapshot_id(session, ticker) != snapshot.id
            or request.market_snapshot.captured_at != receipt
        ):
            raise ValueError("SNAPSHOT_SELECTION_MISMATCH")
        records.update(
            decision_at=decision_at.isoformat(),
            ev=asdict(ev),
            sizing=sizing.decision.as_dict(),
            sizing_id=sizing.record_id,
            sizing_evidence=sizing.evidence,
            risk=risk.as_dict(),
            risk_id=risk_log.id,
            risk_request=asdict(request),
            fee_contract=None if fee_quote is None else fee_quote.decode(),
            model_kind="fixed_heuristic",
        )
        blockers = [
            "SETTLEMENT_RULE_QUALIFICATION_REQUIRED",
            "MODEL_LINEAGE_AND_EVALUATION_REQUIRED",
        ]
        if fee_quote is None:
            blockers.append("FEE_EVIDENCE_REQUIRED_LEGACY_CONFIGURED_DIAGNOSTIC")
        if ev.net_ev <= settings.paper_min_edge:
            blockers.append("POSITIVE_NET_EV")
        if sizing.decision.live_candidate_contracts < 1:
            blockers.append("PHASE_3M_NONZERO")
        if risk.action.value != "ALLOW" or risk.hard_blocks:
            blockers.append("PHASE_3N_ALLOW")
        finished = utc_now()
        if (finished - receipt).total_seconds() > 60:
            blockers.append("STALE_ORDERBOOK")
        return WeatherPreparationResult(
            "COMPUTED_UNQUALIFIED",
            ticker,
            source_envelopes,
            tuple(blockers),
            records,
            decision,
            sizing.decision,
            risk,
            request,
            output,
        )
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        return WeatherPreparationResult("BLOCKED", ticker, source_envelopes, (str(exc),), records)
