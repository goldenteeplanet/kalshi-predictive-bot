from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import (
    Forecast,
    CryptoFeature,
    MarketRanking,
    MarketSnapshot,
    RuntimeProvenanceEvent,
    SportsFeature,
    WeatherFeature,
)
from kalshi_predictor.utils.time import utc_now

DEFAULT_PROV10_REPORT = Path(
    "reports/phase_prov10/prov10_full_scheduler_cycle_certification.json"
)
DEFAULT_OUTPUT_DIR = Path("reports/phase_prov11")
DEFAULT_EVENT_LIMIT = 250
FEATURE_MODELS = {
    "crypto_features": CryptoFeature,
    "weather_features": WeatherFeature,
    "sports_features": SportsFeature,
}


def build_provenance_diagnostics(
    session: Session,
    *,
    event_limit: int = DEFAULT_EVENT_LIMIT,
    prov10_report: Path = DEFAULT_PROV10_REPORT,
    execution_enabled: bool = False,
) -> dict[str, Any]:
    if event_limit < 1 or event_limit > 1000:
        raise ValueError("event_limit must be between 1 and 1000")

    grouped = session.execute(
        select(
            RuntimeProvenanceEvent.model_name,
            RuntimeProvenanceEvent.stage,
            func.count(RuntimeProvenanceEvent.id),
        ).group_by(RuntimeProvenanceEvent.model_name, RuntimeProvenanceEvent.stage)
    ).all()
    events = list(
        session.scalars(
            select(RuntimeProvenanceEvent)
            .order_by(desc(RuntimeProvenanceEvent.id))
            .limit(event_limit)
        )
    )
    forecast_ids = {row.forecast_id for row in events}
    ranking_ids = {row.ranking_id for row in events if row.ranking_id is not None}
    snapshot_ids = {
        row.market_snapshot_id for row in events if row.market_snapshot_id is not None
    }
    existing_forecasts = _existing_ids(session, Forecast, forecast_ids)
    existing_rankings = _existing_ids(session, MarketRanking, ranking_ids)
    existing_snapshots = _existing_ids(session, MarketSnapshot, snapshot_ids)
    previous_values = {
        row.previous_digest for row in events if row.previous_digest != "GENESIS"
    }
    existing_previous = set()
    if previous_values:
        existing_previous = set(
            session.scalars(
                select(RuntimeProvenanceEvent.provenance_digest).where(
                    RuntimeProvenanceEvent.provenance_digest.in_(previous_values)
                )
            )
        )

    failures: Counter[str] = Counter()
    model_versions: dict[str, set[str]] = {}
    latest_event_at = None
    for row in events:
        model_versions.setdefault(row.model_name, set()).add(row.model_version)
        latest_event_at = max(latest_event_at, row.event_at) if latest_event_at else row.event_at
        if not _digest_valid(row):
            failures["DIGEST_INVALID"] += 1
        if row.previous_digest != "GENESIS" and row.previous_digest not in existing_previous:
            failures["PREVIOUS_DIGEST_MISSING"] += 1
        if row.forecast_id not in existing_forecasts:
            failures["FORECAST_REFERENCE_MISSING"] += 1
        if row.ranking_id is not None and row.ranking_id not in existing_rankings:
            failures["RANKING_REFERENCE_MISSING"] += 1
        if row.market_snapshot_id is not None and row.market_snapshot_id not in existing_snapshots:
            failures["SNAPSHOT_REFERENCE_MISSING"] += 1
        if not row.model_version:
            failures["MODEL_VERSION_MISSING"] += 1

    per_model: dict[str, dict[str, Any]] = {}
    total_events = 0
    for model_name, stage, count in grouped:
        model = per_model.setdefault(
            str(model_name), {"events": 0, "forecast_events": 0, "ranking_events": 0}
        )
        value = int(count)
        model["events"] += value
        total_events += value
        if stage == "FORECAST_CREATED":
            model["forecast_events"] += value
        elif stage == "RANKING_CREATED":
            model["ranking_events"] += value
    for model_name, values in per_model.items():
        values["versions_in_sample"] = sorted(model_versions.get(model_name, set()))

    scheduler = _load_prov10(prov10_report)
    chain_valid = not failures
    status = "HEALTHY" if chain_valid and not execution_enabled else "BLOCKED"
    return {
        "phase": "PROV-11",
        "generated_at": utc_now().isoformat(),
        "mode": "READ_ONLY_PROVENANCE_DIAGNOSTICS_PREVIEW",
        "status": status,
        "read_only": True,
        "database_writes": 0,
        "execution_enabled": execution_enabled,
        "summary": {
            "total_events": total_events,
            "events_verified": len(events),
            "event_limit": event_limit,
            "latest_event_at": latest_event_at.isoformat() if latest_event_at else None,
            "models": sorted(per_model),
            "chain_valid": chain_valid,
            "failure_count": sum(failures.values()),
            "failures": dict(sorted(failures.items())),
        },
        "models": per_model,
        "scheduler_certification": scheduler,
        "guardrails": {
            "thresholds_changed": False,
            "paper_trading_enabled": False,
            "live_trading_enabled": False,
            "mutation_endpoints_added": False,
        },
        "next_action": (
            "Continue shadow provenance monitoring; no execution activation is authorized."
            if status == "HEALTHY"
            else "Investigate provenance or execution guard failures before advancing."
        ),
    }


def write_provenance_diagnostics_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    event_limit: int = DEFAULT_EVENT_LIMIT,
    prov10_report: Path = DEFAULT_PROV10_REPORT,
    execution_enabled: bool = False,
) -> Path:
    payload = build_provenance_diagnostics(
        session,
        event_limit=event_limit,
        prov10_report=prov10_report,
        execution_enabled=execution_enabled,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "prov11_provenance_diagnostics.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return path


def build_market_decision_trace(
    session: Session,
    ticker: str,
    *,
    event_limit: int = 20,
    stale_after_minutes: int = 60,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized = str(ticker or "").strip().upper()
    if not normalized or len(normalized) > 128:
        raise ValueError("ticker must be between 1 and 128 characters")
    if event_limit < 1 or event_limit > 100:
        raise ValueError("event_limit must be between 1 and 100")
    current = _as_utc(now or utc_now())
    events = list(session.scalars(
        select(RuntimeProvenanceEvent)
        .where(RuntimeProvenanceEvent.ticker == normalized)
        .order_by(desc(RuntimeProvenanceEvent.id))
        .limit(event_limit)
    ))
    events.reverse()
    if not events:
        return {
            "phase": "PROV-12", "ticker": normalized, "status": "NOT_FOUND",
            "read_only": True, "database_writes": 0, "alerts": ["TRACE_MISSING"],
            "stages": [], "generated_at": current.isoformat(),
        }

    forecast_ids = {event.forecast_id for event in events}
    ranking_ids = {event.ranking_id for event in events if event.ranking_id is not None}
    snapshot_ids = {
        event.market_snapshot_id for event in events if event.market_snapshot_id is not None
    }
    forecasts = _rows_by_id(session, Forecast, forecast_ids)
    rankings = _rows_by_id(session, MarketRanking, ranking_ids)
    snapshots = _rows_by_id(session, MarketSnapshot, snapshot_ids)
    feature_refs = _feature_rows(session, events)
    event_stages = {(event.forecast_id, event.stage) for event in events}
    stages = []
    alerts: set[str] = set()
    for event in events:
        forecast = forecasts.get(event.forecast_id)
        ranking = rankings.get(event.ranking_id) if event.ranking_id is not None else None
        snapshot = snapshots.get(event.market_snapshot_id) if event.market_snapshot_id else None
        feature_key = (event.feature_source_table, event.feature_source_id)
        feature = feature_refs.get(feature_key)
        observation = _json_dict(event.source_observation_ref_json)
        stage_alerts = _event_alerts(
            event,
            current=current,
            stale_after_minutes=stale_after_minutes,
            forecast_exists=forecast is not None,
            ranking_exists=(ranking is not None if event.ranking_id is not None else True),
            snapshot_exists=(snapshot is not None if event.market_snapshot_id is not None else False),
            feature_exists=feature is not None,
            observation_exists=bool(observation),
            ranking_stage_exists=(event.forecast_id, "RANKING_CREATED") in event_stages,
        )
        alerts.update(stage_alerts)
        stages.append({
            "event_id": event.id,
            "stage": event.stage,
            "event_at": _as_utc(event.event_at).isoformat(),
            "forecast_id": event.forecast_id,
            "forecast_probability": getattr(forecast, "yes_probability", None),
            "model_name": event.model_name,
            "model_version": event.model_version,
            "observation": observation,
            "feature_source_table": event.feature_source_table,
            "feature_source_id": event.feature_source_id,
            "feature_exists": feature is not None,
            "snapshot_id": event.market_snapshot_id,
            "snapshot_at": (
                _as_utc(snapshot.captured_at).isoformat() if snapshot is not None else None
            ),
            "ranking_id": event.ranking_id,
            "opportunity_score": getattr(ranking, "opportunity_score", None),
            "estimated_edge": getattr(ranking, "estimated_edge", None),
            "previous_digest": event.previous_digest,
            "digest": event.provenance_digest,
            "digest_valid": _digest_valid(event),
            "alerts": stage_alerts,
        })
    latest = events[-1]
    return {
        "phase": "PROV-12", "ticker": normalized,
        "status": "HEALTHY" if not alerts else "DRIFT_ALERT",
        "read_only": True, "database_writes": 0,
        "generated_at": current.isoformat(),
        "latest_event_at": _as_utc(latest.event_at).isoformat(),
        "model_name": latest.model_name, "model_version": latest.model_version,
        "stale_after_minutes": stale_after_minutes,
        "alerts": sorted(alerts), "stages": stages,
        "guardrails": {"thresholds_changed": False, "execution_enabled": False},
    }


def build_provenance_drift_alerts(
    session: Session,
    *,
    ticker_limit: int = 50,
    stale_after_minutes: int = 60,
    now: datetime | None = None,
) -> dict[str, Any]:
    if ticker_limit < 1 or ticker_limit > 250:
        raise ValueError("ticker_limit must be between 1 and 250")
    current = _as_utc(now or utc_now())
    ranked = select(
        RuntimeProvenanceEvent.id.label("event_id"),
        func.row_number().over(
            partition_by=RuntimeProvenanceEvent.ticker,
            order_by=desc(RuntimeProvenanceEvent.id),
        ).label("row_number"),
    ).subquery()
    events = list(session.scalars(
        select(RuntimeProvenanceEvent)
        .join(ranked, RuntimeProvenanceEvent.id == ranked.c.event_id)
        .where(ranked.c.row_number == 1)
        .order_by(desc(RuntimeProvenanceEvent.id))
        .limit(ticker_limit)
    ))
    feature_refs = _feature_rows(session, events)
    rows = []
    counts: Counter[str] = Counter()
    for event in events:
        age_minutes = max(
            0, int((current - _as_utc(event.event_at)).total_seconds() // 60)
        )
        alerts = []
        if age_minutes > stale_after_minutes:
            alerts.append("TRACE_STALE")
        if event.stage != "RANKING_CREATED":
            alerts.append("RANKING_STAGE_MISSING")
        if not _json_dict(event.source_observation_ref_json):
            alerts.append("OBSERVATION_REF_MISSING")
        if event.feature_source_id is None or not event.feature_source_table:
            alerts.append("FEATURE_REF_MISSING")
        elif (event.feature_source_table, event.feature_source_id) not in feature_refs:
            alerts.append("FEATURE_ROW_MISSING")
        if event.market_snapshot_id is None:
            alerts.append("SNAPSHOT_REF_MISSING")
        if not _digest_valid(event):
            alerts.append("DIGEST_INVALID")
        counts.update(alerts)
        rows.append({
            "ticker": event.ticker, "model_name": event.model_name,
            "model_version": event.model_version, "latest_stage": event.stage,
            "latest_event_at": _as_utc(event.event_at).isoformat(),
            "age_minutes": age_minutes,
            "status": "HEALTHY" if not alerts else "DRIFT_ALERT",
            "alerts": sorted(alerts),
        })
    return {
        "phase": "PROV-12", "generated_at": current.isoformat(),
        "mode": "READ_ONLY_PROVENANCE_DRIFT_ALERT_PREVIEW",
        "read_only": True, "database_writes": 0,
        "summary": {
            "tickers_checked": len(rows),
            "healthy_tickers": sum(row["status"] == "HEALTHY" for row in rows),
            "alert_tickers": sum(row["status"] != "HEALTHY" for row in rows),
            "alert_counts": dict(sorted(counts.items())),
            "stale_after_minutes": stale_after_minutes,
        },
        "rows": rows,
        "guardrails": {"thresholds_changed": False, "execution_enabled": False},
    }


def _existing_ids(session: Session, model: type[Any], ids: set[int]) -> set[int]:
    if not ids:
        return set()
    return set(session.scalars(select(model.id).where(model.id.in_(ids))))


def _rows_by_id(session: Session, model: type[Any], ids: set[int]) -> dict[int, Any]:
    if not ids:
        return {}
    return {row.id: row for row in session.scalars(select(model).where(model.id.in_(ids)))}


def _feature_rows(
    session: Session, events: list[RuntimeProvenanceEvent]
) -> dict[tuple[str | None, int | None], Any]:
    result: dict[tuple[str | None, int | None], Any] = {}
    for table, model in FEATURE_MODELS.items():
        ids = {
            event.feature_source_id for event in events
            if event.feature_source_table == table and event.feature_source_id is not None
        }
        for row_id, row in _rows_by_id(session, model, ids).items():
            result[(table, row_id)] = row
    return result


def _event_alerts(
    event: RuntimeProvenanceEvent,
    *,
    current: datetime,
    stale_after_minutes: int,
    forecast_exists: bool,
    ranking_exists: bool,
    snapshot_exists: bool,
    feature_exists: bool,
    observation_exists: bool,
    ranking_stage_exists: bool,
) -> list[str]:
    alerts = []
    age_minutes = (current - _as_utc(event.event_at)).total_seconds() / 60
    if age_minutes > stale_after_minutes:
        alerts.append("TRACE_STALE")
    if not _digest_valid(event):
        alerts.append("DIGEST_INVALID")
    if not forecast_exists:
        alerts.append("FORECAST_REFERENCE_MISSING")
    if not ranking_exists:
        alerts.append("RANKING_REFERENCE_MISSING")
    if not snapshot_exists:
        alerts.append("SNAPSHOT_REF_MISSING")
    if event.feature_source_id is None or not event.feature_source_table:
        alerts.append("FEATURE_REF_MISSING")
    elif not feature_exists:
        alerts.append("FEATURE_ROW_MISSING")
    if not observation_exists:
        alerts.append("OBSERVATION_REF_MISSING")
    if event.stage == "FORECAST_CREATED" and not ranking_stage_exists:
        alerts.append("RANKING_STAGE_MISSING")
    return sorted(set(alerts))


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _digest_valid(row: RuntimeProvenanceEvent) -> bool:
    try:
        raw = json.loads(row.raw_json)
    except (TypeError, json.JSONDecodeError):
        return False
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return digest == row.provenance_digest


def _load_prov10(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "MISSING", "cycles_passed": 0, "path": str(path)}
    return {
        "status": str(payload.get("status") or "UNKNOWN"),
        "cycles_passed": int(payload.get("cycles_passed") or 0),
        "cycles_required": int(payload.get("cycles_required") or 0),
        "new_oom": any(bool(row.get("new_oom")) for row in payload.get("cycles", [])),
        "path": str(path),
    }
