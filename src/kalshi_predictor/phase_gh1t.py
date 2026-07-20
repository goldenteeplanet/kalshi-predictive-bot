from __future__ import annotations

from pathlib import Path
from decimal import Decimal
from typing import Any, Callable

import httpx
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.crypto.linker import link_crypto_markets
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import CryptoFeature, WeatherFeature, WeatherForecast
from kalshi_predictor.forecasting.registry import get_forecaster
from kalshi_predictor.phase_gh1h import PRODUCTION_PUBLIC_REST_URL
from kalshi_predictor.phase_gh1p import DEFAULT_CRYPTO_SERIES
from kalshi_predictor.phase_gh1o import evaluate_independent_forecast
from kalshi_predictor.utils.time import parse_datetime
from kalshi_predictor.weather.features import calculate_weather_features
from kalshi_predictor.weather.repository import insert_weather_features
from kalshi_predictor.weather.temperature_contracts import (
    parse_point_temperature_ticker,
    validate_point_temperature_market,
)
from kalshi_predictor.weather.repository import insert_weather_market_link
from kalshi_predictor.utils.time import utc_now


def run_atomic_activation(
    *, session_factory: Callable[[], Session], settings: Settings, verified_backup: Path,
    max_markets_per_category: int = 20,
    writer_monitor_fn: Callable[[], dict[str, Any]] | None = None,
    rest_base_url: str = PRODUCTION_PUBLIC_REST_URL,
    minimum_close_minutes: int = 0, immediate_edge_evaluation: bool = False,
    phase_name: str = "GH-1T",
) -> dict[str, Any]:
    if not verified_backup.is_file() or verified_backup.stat().st_size == 0:
        return _blocked("VERIFIED_BACKUP_MISSING")
    writer = (writer_monitor_fn or (lambda: db_writer_monitor(settings=settings)))()
    if writer.get("safe_to_start_write") is not True:
        return {**_blocked("ACTIVE_WRITER"), "writer_monitor": writer}
    pinned = _fetch_pinned(
        rest_base_url, max_markets_per_category, minimum_close_minutes=minimum_close_minutes
    )
    print("GH1T_STAGE_PINNED", flush=True)
    result: dict[str, Any] = {
        "phase": phase_name, "status": "COMPLETE", "execution_enabled": False,
        "backup_path": str(verified_backup), "pinned_tickers": {},
        "snapshots_inserted": 0, "links_written": 0, "features_inserted": 0,
        "forecasts_inserted": 0, "forecasts_skipped": 0,
    }
    with session_factory() as session:
        try:
            snapshots: dict[str, list[Any]] = {"crypto_v2": [], "weather_v2": []}
            for model, rows in pinned.items():
                result["pinned_tickers"][model] = [row[0]["ticker"] for row in rows]
                for market, book in rows:
                    snapshot = insert_market_snapshot(
                        session, market_json=market, orderbook_json=book, captured_at=utc_now()
                    )
                    snapshots[model].append(snapshot)
                    result["snapshots_inserted"] += 1
            print("GH1T_STAGE_SNAPSHOTS", flush=True)

            crypto_tickers = result["pinned_tickers"]["crypto_v2"]
            linked = link_crypto_markets(session, tickers=crypto_tickers, limit=len(crypto_tickers))
            if (linked.ambiguous_markets or linked.unsupported_markets
                    or linked.links_created + linked.already_linked != len(crypto_tickers)):
                raise ValueError("PINNED_CRYPTO_LINK_SET_NOT_EXACT")
            result["links_written"] += linked.links_created
            print("GH1T_STAGE_CRYPTO_LINKS", flush=True)

            weather_targets = set()
            for market, _ in pinned["weather_v2"]:
                contract = parse_point_temperature_ticker(str(market.get("ticker") or ""))
                if contract is None:
                    raise ValueError("PINNED_WEATHER_TICKER_PARSE_FAILED")
                validation = validate_point_temperature_market(
                    contract, market, series_scope="KXTEMPNYCH"
                )
                if not validation.passed:
                    raise ValueError("PINNED_WEATHER_METADATA_NOT_EXACT")
                insert_weather_market_link(
                    session, ticker=contract.ticker, location_key=contract.location_key,
                    weather_metric="TEMPERATURE", target_operator=contract.contract_kind,
                    target_value=contract.discrete_threshold_f, target_time=contract.target_utc_time,
                    confidence="1.0", reason="GH-1T atomic exact metadata validation",
                    raw_json={"phase": "GH-1T", "pinned": True},
                )
                weather_targets.add(contract.target_utc_time)
                result["links_written"] += 1
            print("GH1T_STAGE_WEATHER_LINKS", flush=True)

            if session.scalar(
                select(CryptoFeature.id).where(CryptoFeature.symbol == "BTC")
                .order_by(CryptoFeature.generated_at.desc()).limit(1)
            ) is None:
                raise ValueError("REQUIRED_BTC_FEATURE_MISSING")
            for target in sorted(weather_targets):
                result["features_inserted"] += _ensure_exact_weather_feature(session, target)
            print("GH1T_STAGE_FEATURES", flush=True)

            evaluations: list[dict[str, Any]] = []
            pinned_by_ticker = {
                market["ticker"]: (market, book)
                for rows in pinned.values() for market, book in rows
            }
            for model, model_snapshots in snapshots.items():
                forecaster = get_forecaster(model)
                begin = getattr(forecaster, "begin_forecast_run", None)
                end = getattr(forecaster, "end_forecast_run", None)
                if callable(begin):
                    begin()
                restore_weather_lookup = None
                if model == "weather_v2":
                    import kalshi_predictor.forecasting.weather_v2 as weather_model
                    restore_weather_lookup = weather_model.get_latest_weather_features
                    weather_model.get_latest_weather_features = _get_exact_weather_feature
                try:
                    for snapshot in model_snapshots:
                        forecast = forecaster.forecast(session, snapshot)
                        if forecast is None:
                            result["forecasts_skipped"] += 1
                        else:
                            insert_forecast(
                                session, forecast, market_snapshot_id=snapshot.id
                            )
                            result["forecasts_inserted"] += 1
                            if immediate_edge_evaluation:
                                market, book = pinned_by_ticker[snapshot.ticker]
                                evaluations.append(evaluate_independent_forecast(
                                    forecast={
                                        "ticker": forecast.ticker,
                                        "model_name": forecast.model_name,
                                        "forecasted_at": forecast.forecasted_at.isoformat(),
                                        "yes_probability": str(forecast.yes_probability),
                                    },
                                    market=market, orderbook=book, settings=settings,
                                    max_forecast_age_minutes=Decimal("120"),
                                ))
                finally:
                    if restore_weather_lookup is not None:
                        weather_model.get_latest_weather_features = restore_weather_lookup
                    if callable(end):
                        end()
            print("GH1T_STAGE_FORECASTS", flush=True)
            if immediate_edge_evaluation:
                result["immediate_evaluations"] = evaluations
                result["edge_summary"] = {
                    "evaluated": len(evaluations),
                    "positive_executable_edge": sum(
                        Decimal(row["executable_edge"] or "0") > 0 for row in evaluations
                    ),
                    "advanced_candidates": sum(bool(row["advance"]) for row in evaluations),
                }
            session.commit()
        except Exception:
            session.rollback()
            raise
    result["run_gh1o"] = result["forecasts_inserted"] > 0
    return result


def _fetch_pinned(
    base_url: str, bound: int, *, minimum_close_minutes: int = 0
) -> dict[str, list[tuple[dict[str, Any], dict[str, Any]]]]:
    selected = {"crypto_v2": [], "weather_v2": []}
    with httpx.Client(base_url=base_url, timeout=15.0) as client:
        for model, series_group in (("crypto_v2", DEFAULT_CRYPTO_SERIES), ("weather_v2", ("KXTEMPNYCH",))):
            for series in series_group:
                if len(selected[model]) >= bound:
                    break
                response = client.get("/markets", params={"limit": 100, "status": "open", "series_ticker": series})
                response.raise_for_status()
                markets = response.json().get("markets", [])
                eligible = [market for market in markets if _has_required_lead_time(
                    market, minimum_close_minutes=minimum_close_minutes
                )]
                if not eligible:
                    continue
                earliest = min(parse_datetime(market.get("close_time")) for market in eligible)
                window = [market for market in eligible if parse_datetime(market.get("close_time")) == earliest]
                for market in window:
                    if len(selected[model]) >= bound:
                        break
                    book = client.get(f"/markets/{market['ticker']}/orderbook", params={"depth": 5})
                    book.raise_for_status()
                    selected[model].append((market, book.json()))
    return selected


def _has_required_lead_time(market: dict[str, Any], *, minimum_close_minutes: int) -> bool:
    close = parse_datetime(market.get("close_time"))
    if close is None:
        return False
    return (close - utc_now()).total_seconds() / 60 >= minimum_close_minutes


def _ensure_exact_weather_feature(session: Session, target: Any) -> int:
    existing = session.scalar(select(WeatherFeature).where(
        WeatherFeature.location_key == "new_york", WeatherFeature.target_time == target
    ).limit(1))
    if existing is not None:
        return 0
    forecast = session.scalar(select(WeatherForecast).where(
        WeatherForecast.location_key == "new_york", WeatherForecast.forecast_time == target
    ).order_by(WeatherForecast.forecast_generated_at.desc()).limit(1))
    if forecast is None:
        raise ValueError("EXACT_WEATHER_FORECAST_INPUT_MISSING")
    features = calculate_weather_features(forecast, generated_at=utc_now())
    insert_weather_features(
        session, location_key="new_york", source="stored_forecasts", generated_at=utc_now(),
        target_time=target, features=features, raw_json=features,
    )
    return 1


def _get_exact_weather_feature(
    session: Session, location_key: str, *, target_time: Any = None
) -> WeatherFeature | None:
    if target_time is None:
        return session.scalar(
            select(WeatherFeature).where(WeatherFeature.location_key == location_key)
            .order_by(desc(WeatherFeature.generated_at), desc(WeatherFeature.id)).limit(1)
        )
    return session.scalar(
        select(WeatherFeature).where(
            WeatherFeature.location_key == location_key,
            WeatherFeature.target_time == target_time,
        ).order_by(desc(WeatherFeature.generated_at), desc(WeatherFeature.id)).limit(1)
    )


def _blocked(reason: str) -> dict[str, Any]:
    return {"phase": "GH-1T", "status": "BLOCKED", "reason": reason,
            "database_writes": 0, "execution_enabled": False}
