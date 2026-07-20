from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    CryptoFeature,
    CryptoPrice,
    Forecast,
    MarketSnapshot,
    RuntimeProvenanceEvent,
    WeatherFeature,
    WeatherForecast,
)
from kalshi_predictor.utils.time import parse_datetime, utc_now

DEFAULT_OUTPUT_DIR = Path("reports/phase_prov13")
FEATURE_MODELS = {
    "crypto_features": CryptoFeature,
    "weather_features": WeatherFeature,
}


def build_prov13_repair_preview(
    session: Session,
    *,
    limit: int = 200,
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    events = list(session.scalars(
        select(RuntimeProvenanceEvent)
        .where(
            (RuntimeProvenanceEvent.source_observation_ref_json.is_(None))
            | (RuntimeProvenanceEvent.market_snapshot_id.is_(None))
        )
        .order_by(desc(RuntimeProvenanceEvent.id))
        .limit(limit)
    ))
    rows = []
    statuses: Counter[str] = Counter()
    for event in events:
        observation_missing = not _json_dict(event.source_observation_ref_json)
        snapshot_missing = event.market_snapshot_id is None
        observation = (
            _exact_observation_preview(session, event) if observation_missing else None
        )
        snapshot = _exact_snapshot_preview(session, event) if snapshot_missing else None
        blockers = []
        if observation_missing and observation is None:
            blockers.append("EXACT_OBSERVATION_NOT_UNIQUE_OR_MISSING")
        if snapshot_missing and snapshot is None:
            blockers.append("EXACT_SNAPSHOT_NOT_UNIQUE_OR_MISSING")
        status = "SAFE_EXACT_PREVIEW" if not blockers else "BLOCKED"
        statuses[status] += 1
        rows.append({
            "event_id": event.id,
            "event_key": event.event_key,
            "stage": event.stage,
            "ticker": event.ticker,
            "model_name": event.model_name,
            "forecast_id": event.forecast_id,
            "ranking_id": event.ranking_id,
            "observation_missing": observation_missing,
            "snapshot_missing": snapshot_missing,
            "proposed_source_observation_ref": observation,
            "proposed_market_snapshot_id": snapshot,
            "status": status,
            "blockers": blockers,
        })
    return {
        "phase": "PROV-13",
        "generated_at": utc_now().isoformat(),
        "mode": "EXACT_ATTRIBUTION_REPAIR_NO_WRITE_PREVIEW",
        "read_only": True,
        "database_writes": 0,
        "existing_provenance_rows_modified": 0,
        "summary": {
            "rows_examined": len(rows),
            "safe_exact_preview_rows": statuses["SAFE_EXACT_PREVIEW"],
            "blocked_rows": statuses["BLOCKED"],
            "observation_missing_rows": sum(row["observation_missing"] for row in rows),
            "snapshot_missing_rows": sum(row["snapshot_missing"] for row in rows),
        },
        "rows": rows,
        "guardrails": {
            "historical_backfill_applied": False,
            "fuzzy_matching_used": False,
            "thresholds_changed": False,
            "execution_enabled": False,
        },
        "next_action": (
            "Rebuild future feature rows so exact source references flow into new provenance; "
            "do not mutate historical events without a separate approved append-only policy."
        ),
    }


def write_prov13_repair_preview(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    limit: int = 200,
) -> Path:
    payload = build_prov13_repair_preview(session, limit=limit)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "prov13_exact_attribution_repair_preview.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return path


def _exact_observation_preview(
    session: Session,
    event: RuntimeProvenanceEvent,
) -> dict[str, Any] | None:
    model = FEATURE_MODELS.get(str(event.feature_source_table or ""))
    if model is None or event.feature_source_id is None:
        return None
    feature = session.get(model, event.feature_source_id)
    if feature is None:
        return None
    raw = decode_json(feature.raw_json)
    explicit = raw.get("source_observation_ref")
    if isinstance(explicit, dict) and explicit.get("id") is not None:
        return dict(explicit)
    if isinstance(feature, CryptoFeature):
        observed_at = parse_datetime(raw.get("source_latest_observed_at"))
        if observed_at is None:
            return None
        statement = select(CryptoPrice).where(
            CryptoPrice.symbol == feature.symbol,
            CryptoPrice.observed_at == observed_at,
        )
        source = raw.get("price_source")
        if source and source != "stored_prices":
            statement = statement.where(CryptoPrice.source == str(source))
        matches = list(session.scalars(statement.order_by(CryptoPrice.id).limit(2)))
        if len(matches) != 1:
            return None
        row = matches[0]
        return {
            "table": "crypto_prices", "id": row.id, "symbol": row.symbol,
            "source": row.source, "observed_at": row.observed_at.isoformat(),
        }
    generated_at = parse_datetime(raw.get("forecast_generated_at"))
    target_time = parse_datetime(raw.get("target_time"))
    if generated_at is None or target_time is None:
        return None
    matches = list(session.scalars(
        select(WeatherForecast).where(
            WeatherForecast.location_key == feature.location_key,
            WeatherForecast.forecast_generated_at == generated_at,
            WeatherForecast.forecast_time == target_time,
        ).order_by(WeatherForecast.id).limit(2)
    ))
    if len(matches) != 1:
        return None
    row = matches[0]
    return {
        "table": "weather_forecasts", "id": row.id,
        "location_key": row.location_key, "source": row.source,
        "forecast_generated_at": row.forecast_generated_at.isoformat(),
        "forecast_time": row.forecast_time.isoformat(),
    }


def _exact_snapshot_preview(
    session: Session,
    event: RuntimeProvenanceEvent,
) -> int | None:
    peer_ids = set(session.scalars(
        select(RuntimeProvenanceEvent.market_snapshot_id).where(
            RuntimeProvenanceEvent.forecast_id == event.forecast_id,
            RuntimeProvenanceEvent.market_snapshot_id.is_not(None),
        )
    ))
    if len(peer_ids) == 1:
        snapshot_id = next(iter(peer_ids))
        if session.get(MarketSnapshot, snapshot_id) is not None:
            return int(snapshot_id)
    forecast = session.get(Forecast, event.forecast_id)
    if forecast is None:
        return None
    matches = list(session.scalars(
        select(MarketSnapshot).where(
            MarketSnapshot.ticker == forecast.ticker,
            MarketSnapshot.captured_at == forecast.forecasted_at,
        ).order_by(MarketSnapshot.id).limit(2)
    ))
    return int(matches[0].id) if len(matches) == 1 else None


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
