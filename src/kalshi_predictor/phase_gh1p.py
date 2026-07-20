from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.forecasting.registry import run_forecast_models
from kalshi_predictor.crypto.repository import get_latest_crypto_link_for_ticker
from kalshi_predictor.crypto.semantics import EXACT_LINK, terms_from_link_payload, select_compatible_crypto_feature
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.weather.repository import get_latest_weather_features, get_latest_weather_link_for_ticker
from kalshi_predictor.forecasting.weather_v2 import _forecast_age_hours, _weather_adjustment
from kalshi_predictor.phase_gh1h import PRODUCTION_PUBLIC_REST_URL
from kalshi_predictor.utils.time import utc_now

DEFAULT_CRYPTO_SERIES = ("KXBTC", "KXETH", "KXSOLE", "KXXRP", "KXDOGE")
DEFAULT_WEATHER_SERIES = ("KXTEMPNYCH", "KXTEMPCHI", "KXTEMPMIA", "KXTEMPAUS", "KXTEMPLAX")


def apply_gh1p_refresh(
    *, session_factory: Callable[[], Session], settings: Settings,
    max_markets_per_category: int, writer_monitor_fn: Callable[[], dict[str, Any]] | None = None,
    rest_base_url: str = PRODUCTION_PUBLIC_REST_URL,
    eligibility_fn: Callable[[Session, str, dict[str, Any]], tuple[bool, str]] | None = None,
) -> dict[str, Any]:
    writer = (writer_monitor_fn or (lambda: db_writer_monitor(settings=settings)))()
    if not writer.get("safe_to_start_write", False):
        return {"status": "BLOCKED_ACTIVE_WRITER", "database_writes": 0,
                "execution_enabled": False, "writer_monitor": writer}
    selected: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {"crypto_v2": [], "weather_v2": []}
    with httpx.Client(base_url=rest_base_url, timeout=15.0) as client:
        for model, series_group in (("crypto_v2", DEFAULT_CRYPTO_SERIES), ("weather_v2", DEFAULT_WEATHER_SERIES)):
            for series in series_group:
                if len(selected[model]) >= max_markets_per_category:
                    break
                response = client.get("/markets", params={"limit": 20, "status": "open", "series_ticker": series})
                response.raise_for_status()
                for market in response.json().get("markets", []):
                    if len(selected[model]) >= max_markets_per_category:
                        break
                    ticker = str(market.get("ticker") or "")
                    if not ticker:
                        continue
                    book_response = client.get(f"/markets/{ticker}/orderbook", params={"depth": 5})
                    book_response.raise_for_status()
                    selected[model].append((market, book_response.json()))
    eligibility = eligibility_fn or (
        lambda session, model, market: _candidate_eligibility(
            session, model, market, settings=settings
        )
    )
    ineligible: dict[str, list[dict[str, str]]] = {"crypto_v2": [], "weather_v2": []}
    eligible: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {
        "crypto_v2": [], "weather_v2": []
    }
    # This session performs SELECTs only and closes before the sole writer session opens.
    with session_factory() as session:
        for model, payloads in selected.items():
            for market, orderbook in payloads:
                ok, reason = eligibility(session, model, market)
                if ok:
                    eligible[model].append((market, orderbook))
                else:
                    ineligible[model].append({"ticker": str(market.get("ticker") or ""), "reason": reason})
        session.rollback()

    snapshots_inserted = forecasts_inserted = forecasts_skipped = 0
    refreshed_tickers: dict[str, list[str]] = {"crypto_v2": [], "weather_v2": []}
    with session_factory() as session:
        try:
            for model, payloads in eligible.items():
                snapshots = []
                for market, orderbook in payloads:
                    snapshot = insert_market_snapshot(
                        session, market_json=market, orderbook_json=orderbook, captured_at=utc_now()
                    )
                    snapshots.append(snapshot)
                    snapshots_inserted += 1
                    refreshed_tickers[model].append(snapshot.ticker)
                summary = run_forecast_models(session, model_name=model, snapshots=snapshots)
                forecasts_inserted += summary.forecasts_inserted
                forecasts_skipped += summary.skipped
            session.commit()
        except Exception:
            session.rollback()
            raise
    return {
        "status": "COMPLETE", "execution_enabled": False, "orders_submitted": 0,
        "single_writer_session_count": 1, "snapshots_inserted": snapshots_inserted,
        "forecasts_inserted": forecasts_inserted, "forecasts_skipped": forecasts_skipped,
        "refreshed_tickers": refreshed_tickers, "writer_monitor": writer,
        "eligibility": {
            "eligible_counts": {model: len(rows) for model, rows in eligible.items()},
            "ineligible_counts": {model: len(rows) for model, rows in ineligible.items()},
            "ineligible_rows": ineligible,
        },
    }


def _candidate_eligibility(
    session: Session, model: str, market: dict[str, Any], *, settings: Settings | None = None
) -> tuple[bool, str]:
    active_settings = settings or Settings()
    ticker = str(market.get("ticker") or "")
    if model == "crypto_v2":
        link = get_latest_crypto_link_for_ticker(session, ticker)
        if link is None:
            return False, "NO_EXACT_CRYPTO_LINK"
        confidence = to_decimal(link.confidence)
        if confidence is None or confidence < active_settings.crypto_v2_min_link_confidence:
            return False, "CRYPTO_LINK_CONFIDENCE_TOO_LOW"
        terms = terms_from_link_payload(link.symbol, link.raw_json)
        if terms is None or terms.status != EXACT_LINK or not terms.component_symbols:
            return False, "CRYPTO_TERMS_NOT_EXACT"
        for symbol in terms.component_symbols:
            compatible = select_compatible_crypto_feature(
                session, symbol=symbol, terms=terms, forecast_cutoff=utc_now()
            )
            if not compatible.ok:
                return False, f"CRYPTO_FEATURE_{compatible.reason.upper()}"
        return True, "ELIGIBLE"
    if model == "weather_v2":
        link = get_latest_weather_link_for_ticker(session, ticker)
        if link is None:
            return False, "NO_EXACT_WEATHER_LINK"
        confidence = to_decimal(link.confidence)
        if confidence is None or confidence < active_settings.weather_v2_min_link_confidence:
            return False, "WEATHER_LINK_CONFIDENCE_TOO_LOW"
        location = (
            link.location_key if link.location_key != "unknown"
            else active_settings.weather_v2_default_location_key
        )
        features = get_latest_weather_features(session, location, target_time=link.target_time)
        if features is None:
            return False, "NO_EXACT_WEATHER_FEATURE"
        if _forecast_age_hours(features) > active_settings.weather_v2_max_forecast_age_hours:
            return False, "WEATHER_FEATURE_STALE"
        if _weather_adjustment(
            link=link, features=features, max_adjustment=active_settings.weather_v2_max_adjustment
        ) is None:
            return False, "WEATHER_FEATURE_METRIC_INCOMPATIBLE"
        return True, "ELIGIBLE"
    return False, "UNSUPPORTED_MODEL"
