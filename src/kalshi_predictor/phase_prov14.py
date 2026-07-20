from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import (
    CryptoFeature,
    CryptoPrice,
    MarketSnapshot,
    RuntimeProvenanceEvent,
    WeatherFeature,
    WeatherForecast,
)
from kalshi_predictor.utils.time import utc_now

DEFAULT_OUTPUT_DIR = Path("reports/phase_prov14")
FEATURE_MODELS = {
    "crypto_features": CryptoFeature,
    "weather_features": WeatherFeature,
}
OBSERVATION_MODELS = {
    "crypto_prices": CryptoPrice,
    "weather_forecasts": WeatherForecast,
}


def build_prov14_certification_report(
    session: Session,
    *,
    after_event_id: int,
    expected_models: Iterable[str] = ("crypto_v2", "weather_v2"),
    limit: int = 200,
) -> dict[str, Any]:
    """Certify only newly appended provenance without mutating runtime state."""
    if after_event_id < 0:
        raise ValueError("after_event_id must be non-negative")
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    expected = tuple(dict.fromkeys(str(model) for model in expected_models))
    events = list(session.scalars(
        select(RuntimeProvenanceEvent)
        .where(RuntimeProvenanceEvent.id > after_event_id)
        .order_by(RuntimeProvenanceEvent.id)
        .limit(limit)
    ))
    rows = [_certify_event(session, event) for event in events]
    model_counts = Counter(row["model_name"] for row in rows if row["passed"])
    missing_models = [model for model in expected if model_counts[model] == 0]
    truncated = bool(events and len(events) == limit and session.scalar(
        select(RuntimeProvenanceEvent.id)
        .where(RuntimeProvenanceEvent.id > events[-1].id)
        .limit(1)
    ) is not None)
    failures = [row for row in rows if not row["passed"]]
    passed = bool(rows) and not failures and not missing_models and not truncated
    return {
        "phase": "PROV-14",
        "generated_at": utc_now().isoformat(),
        "mode": "BOUNDED_FUTURE_ATTRIBUTION_CERTIFICATION",
        "read_only_analysis": True,
        "database_writes_by_analyzer": 0,
        "boundary": {"after_event_id": after_event_id, "limit": limit},
        "summary": {
            "certification_passed": passed,
            "events_examined": len(rows),
            "events_passed": len(rows) - len(failures),
            "events_failed": len(failures),
            "model_counts": dict(sorted(model_counts.items())),
            "missing_expected_models": missing_models,
            "result_truncated": truncated,
        },
        "rows": rows,
        "guardrails": {
            "historical_events_modified": False,
            "fuzzy_matching_used": False,
            "thresholds_changed": False,
            "execution_enabled": False,
        },
        "next_action": (
            "Proceed to retention monitoring only after certification_passed=true."
            if passed else
            "Preserve execution disablement and repair only the exact failed future-write path."
        ),
    }


def write_prov14_certification_report(
    session: Session,
    *,
    after_event_id: int,
    expected_models: Iterable[str] = ("crypto_v2", "weather_v2"),
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    limit: int = 200,
) -> Path:
    payload = build_prov14_certification_report(
        session, after_event_id=after_event_id,
        expected_models=expected_models, limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "prov14_guarded_future_attribution_certification.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return path


def _certify_event(session: Session, event: RuntimeProvenanceEvent) -> dict[str, Any]:
    failures: list[str] = []
    observation = _json_dict(event.source_observation_ref_json)
    observation_table = str(observation.get("table") or "")
    observation_id = _positive_int(observation.get("id"))
    observation_model = OBSERVATION_MODELS.get(observation_table)
    if observation_model is None or observation_id is None:
        failures.append("OBSERVATION_REFERENCE_INVALID")
    elif session.get(observation_model, observation_id) is None:
        failures.append("OBSERVATION_ROW_MISSING")

    if event.market_snapshot_id is None:
        failures.append("SNAPSHOT_REFERENCE_MISSING")
    elif session.get(MarketSnapshot, event.market_snapshot_id) is None:
        failures.append("SNAPSHOT_ROW_MISSING")

    feature_model = FEATURE_MODELS.get(str(event.feature_source_table or ""))
    if feature_model is None or event.feature_source_id is None:
        failures.append("FEATURE_REFERENCE_INVALID")
    elif session.get(feature_model, event.feature_source_id) is None:
        failures.append("FEATURE_ROW_MISSING")

    raw = _json_dict(event.raw_json)
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    if not raw or hashlib.sha256(canonical).hexdigest() != event.provenance_digest:
        failures.append("PROVENANCE_DIGEST_INVALID")
    if raw.get("source_observation_ref") != observation:
        failures.append("OBSERVATION_RAW_COLUMN_MISMATCH")
    if raw.get("market_snapshot_id") != event.market_snapshot_id:
        failures.append("SNAPSHOT_RAW_COLUMN_MISMATCH")
    return {
        "event_id": event.id,
        "event_key": event.event_key,
        "stage": event.stage,
        "ticker": event.ticker,
        "model_name": event.model_name,
        "forecast_id": event.forecast_id,
        "ranking_id": event.ranking_id,
        "source_observation_ref": observation,
        "market_snapshot_id": event.market_snapshot_id,
        "feature_source_table": event.feature_source_table,
        "feature_source_id": event.feature_source_id,
        "passed": not failures,
        "failures": failures,
    }


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
