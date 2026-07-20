from __future__ import annotations

import json

from sqlalchemy import desc, func, select

from kalshi_predictor.config import get_settings
from kalshi_predictor.crypto.features import build_crypto_features
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import (
    RuntimeProvenanceEvent,
    WeatherForecast,
    WeatherMarketLink,
)
from kalshi_predictor.forecasting.registry import latest_snapshots_for_model, run_forecast_models
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.features import calculate_weather_features
from kalshi_predictor.weather.repository import insert_weather_features

settings = get_settings()
if settings.execution_enabled:
    raise RuntimeError("PROV-14 refuses to run while execution is enabled")

factory = get_session_factory(init_db())
with factory() as session:
    boundary = int(session.scalar(select(func.max(RuntimeProvenanceEvent.id))) or 0)
    # Pin the exact current weather set before creating any feature rows.  A
    # certification with no eligible weather book must fail closed and leave
    # the transaction untouched.
    weather_snapshots = latest_snapshots_for_model(
        session, model_name="weather_v2", limit=10
    ) or []
    if not weather_snapshots:
        raise RuntimeError("No exact current weather snapshots are eligible")

    crypto = build_crypto_features(session, symbols=("BTC",))
    generated_at = utc_now()
    weather_features = []
    weather_forecast_ids = []
    weather_targets = []
    aligned_keys = set()
    for snapshot in weather_snapshots:
        link = session.scalar(
            select(WeatherMarketLink)
            .where(WeatherMarketLink.ticker == snapshot.ticker)
            .order_by(desc(WeatherMarketLink.detected_at), desc(WeatherMarketLink.id))
            .limit(1)
        )
        if link is None or link.target_time is None:
            raise RuntimeError(f"Pinned weather ticker lacks exact target time: {snapshot.ticker}")
        key = (link.location_key, link.target_time)
        if key in aligned_keys:
            continue
        forecast = session.scalar(
            select(WeatherForecast)
            .where(
                WeatherForecast.location_key == link.location_key,
                WeatherForecast.forecast_time == link.target_time,
            )
            .order_by(desc(WeatherForecast.forecast_generated_at), desc(WeatherForecast.id))
            .limit(1)
        )
        if forecast is None:
            raise RuntimeError(
                "No exact weather forecast for pinned target "
                f"{link.location_key}/{link.target_time.isoformat()}"
            )
        weather_values = calculate_weather_features(forecast, generated_at=generated_at)
        feature = insert_weather_features(
            session,
            location_key=link.location_key,
            source="stored_forecasts_prov14_bounded_exact_target",
            generated_at=generated_at,
            target_time=link.target_time,
            features=weather_values,
            raw_json=weather_values,
        )
        weather_features.append(feature)
        weather_forecast_ids.append(forecast.id)
        weather_targets.append(link.target_time.isoformat())
        aligned_keys.add(key)
    summaries = {}
    tickers = {}
    for model in ("crypto_v2", "weather_v2"):
        snapshots = (
            weather_snapshots
            if model == "weather_v2"
            else latest_snapshots_for_model(session, model_name=model, limit=10) or []
        )
        tickers[model] = [snapshot.ticker for snapshot in snapshots]
        summary = run_forecast_models(session, model_name=model, snapshots=snapshots)
        summaries[model] = {
            "snapshots_scanned": summary.snapshots_scanned,
            "forecasts_inserted": summary.forecasts_inserted,
            "skipped": summary.skipped,
        }
    session.commit()

print(json.dumps({
    "after_event_id": boundary,
    "crypto_features_inserted": crypto.features_inserted,
    "weather_features_inserted": len(weather_features),
    "weather_feature_ids": [feature.id for feature in weather_features],
    "weather_forecast_ids": weather_forecast_ids,
    "weather_target_times": weather_targets,
    "tickers": tickers,
    "summaries": summaries,
}, sort_keys=True))
