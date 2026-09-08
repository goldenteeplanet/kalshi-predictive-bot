from __future__ import annotations

import hashlib
import json
import random
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import (
    CalibrationObservation,
    CryptoFeature,
    CryptoFeatureLineage,
    EventCalibrationMetric,
    EvidenceExpansionMember,
    Forecast,
)
from kalshi_predictor.phase4cd.domain import deterministic_id, settlement_value
from kalshi_predictor.utils.time import utc_now

FEATURE_LINEAGE_VERIFIED = "FEATURE_LINEAGE_VERIFIED"
FEATURE_RECONSTRUCTABLE_POINT_IN_TIME = "FEATURE_RECONSTRUCTABLE_POINT_IN_TIME"
FEATURE_PRESENT_TIMESTAMP_UNPROVEN = "FEATURE_PRESENT_TIMESTAMP_UNPROVEN"
FEATURE_SOURCE_AFTER_DECISION = "FEATURE_SOURCE_AFTER_DECISION"
FEATURE_UNAVAILABLE = "FEATURE_UNAVAILABLE"
FEATURE_CONFLICT = "FEATURE_CONFLICT"
LINEAGE_VERDICTS = (
    FEATURE_LINEAGE_VERIFIED,
    FEATURE_RECONSTRUCTABLE_POINT_IN_TIME,
    FEATURE_PRESENT_TIMESTAMP_UNPROVEN,
    FEATURE_SOURCE_AFTER_DECISION,
    FEATURE_UNAVAILABLE,
    FEATURE_CONFLICT,
)
KNOWN_VERSIONS = {
    "crypto_features_v2_point_in_time",
    "crypto_features_v3_interval_normalized",
}


def immutable_provenance_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def audit_crypto_feature_lineage(research: Session, source: Session) -> dict[str, Any]:
    forecasts = list(
        research.scalars(
            select(Forecast).where(Forecast.model_name == "crypto_v2").order_by(Forecast.id)
        )
    )
    counts: Counter[str] = Counter()
    for forecast in forecasts:
        record = make_crypto_lineage_record(forecast, source)
        verdict = record.verdict
        research.merge(record)
        counts[verdict] += 1
    research.commit()
    return {
        "audited": len(forecasts),
        "verdicts": {name: counts[name] for name in LINEAGE_VERDICTS},
        "recovered": counts[FEATURE_LINEAGE_VERIFIED]
        + counts[FEATURE_RECONSTRUCTABLE_POINT_IN_TIME],
        "fabricated_features": 0,
    }


def make_crypto_lineage_record(forecast: Forecast, source: Session) -> CryptoFeatureLineage:
    verdict, provenance = audit_forecast_lineage(forecast, source)
    digest = immutable_provenance_hash(provenance)
    return CryptoFeatureLineage(
        lineage_id=deterministic_id("crypto-lineage-v1", forecast.id),
        forecast_id=int(forecast.id),
        market_ticker=forecast.ticker,
        forecast_timestamp=forecast.forecasted_at,
        verdict=verdict,
        feature_ids_json=json.dumps(provenance.get("feature_ids", []), sort_keys=True),
        source_timestamps_json=json.dumps(provenance.get("source_timestamps", []), sort_keys=True),
        feature_versions_json=json.dumps(provenance.get("feature_versions", []), sort_keys=True),
        provenance_hash=digest,
        provenance_json=json.dumps(provenance, sort_keys=True),
        audited_at=utc_now(),
    )


def audit_forecast_lineage(forecast: Forecast, source: Session) -> tuple[str, dict[str, Any]]:
    try:
        embedded = json.loads(forecast.feature_json or "{}")
    except json.JSONDecodeError:
        return FEATURE_CONFLICT, {"reason": "invalid_feature_json"}
    mapping = embedded.get("component_feature_ids")
    if not isinstance(mapping, dict) or not mapping:
        primary = embedded.get("crypto_feature_id")
        symbol = embedded.get("symbol")
        if primary is None or not symbol:
            return FEATURE_UNAVAILABLE, {"reason": "no_feature_ids"}
        mapping = {str(symbol): primary}
    feature_ids: list[int] = []
    timestamps: list[str] = []
    versions: list[str] = []
    canonical: list[dict[str, Any]] = []
    cutoff = _as_utc(forecast.forecasted_at)
    after_decision = False
    timestamp_unproven = False
    conflict = False
    validation = embedded.get("point_in_time_validation") or {}
    for symbol, raw_id in sorted(mapping.items()):
        try:
            feature_id = int(raw_id)
        except (TypeError, ValueError):
            conflict = True
            continue
        feature = source.get(CryptoFeature, feature_id)
        if feature is None:
            timestamp_unproven = True
            continue
        raw = json.loads(feature.raw_json or "{}")
        generated = _as_utc(feature.generated_at)
        latest_source = _parse_time(
            raw.get("source_latest_observed_at")
            or raw.get("latest_price_observed_at")
            or raw.get("source_timestamp")
        )
        version = str(raw.get("feature_version") or "")
        embedded_row = validation.get(str(symbol)) if isinstance(validation, dict) else None
        embedded_generated = _parse_time(
            embedded_row.get("generated_at") if isinstance(embedded_row, dict) else None
        )
        if feature.symbol.upper() != str(symbol).upper():
            conflict = True
        if embedded_generated is not None and embedded_generated != generated:
            conflict = True
        if generated > cutoff or (latest_source is not None and latest_source > cutoff):
            after_decision = True
        if latest_source is None or version not in KNOWN_VERSIONS:
            timestamp_unproven = True
        feature_ids.append(feature_id)
        timestamps.extend(
            value.isoformat() for value in (generated, latest_source) if value is not None
        )
        versions.append(version)
        canonical.append(
            {
                "id": feature_id,
                "symbol": feature.symbol,
                "generated_at": generated.isoformat(),
                "source_latest_observed_at": (latest_source.isoformat() if latest_source else None),
                "version": version,
                "raw_hash": hashlib.sha256(feature.raw_json.encode()).hexdigest(),
            }
        )
    provenance = {
        "forecast_id": forecast.id,
        "forecast_timestamp": cutoff.isoformat(),
        "feature_ids": feature_ids,
        "source_timestamps": timestamps,
        "feature_versions": versions,
        "canonical_features": canonical,
        "reconstructed": False,
    }
    if conflict:
        return FEATURE_CONFLICT, provenance
    if after_decision:
        return FEATURE_SOURCE_AFTER_DECISION, provenance
    if timestamp_unproven or len(feature_ids) != len(mapping):
        return FEATURE_PRESENT_TIMESTAMP_UNPROVEN, provenance
    return FEATURE_LINEAGE_VERIFIED, provenance


def build_event_level_comparison(session: Session, *, model_runs: dict[str, str]) -> dict[str, Any]:
    by_model_event: dict[str, dict[str, list[CalibrationObservation]]] = {}
    for model, run_id in model_runs.items():
        grouped: dict[str, list[CalibrationObservation]] = defaultdict(list)
        for row in session.scalars(
            select(CalibrationObservation).where(
                CalibrationObservation.run_id == run_id,
                CalibrationObservation.model == model,
            )
        ):
            grouped[row.independent_event_id].append(row)
        by_model_event[model] = grouped
    event_sets = [set(grouped) for grouped in by_model_event.values()]
    matched = set.intersection(*event_sets) if event_sets else set()
    comparison_id = deterministic_id(
        "event-comparison-v1", json.dumps(model_runs, sort_keys=True), sorted(matched)
    )
    results: dict[str, Any] = {}
    for model, grouped in by_model_event.items():
        event_brier: list[Decimal] = []
        event_log: list[Decimal] = []
        calibration_pairs: list[tuple[Decimal, Decimal]] = []
        for event_id in sorted(matched):
            rows = grouped[event_id]
            mean_probability = sum(
                (Decimal(row.forecast_probability) for row in rows), Decimal("0")
            ) / len(rows)
            outcomes = [settlement_value(row.settlement_result) for row in rows]
            valid_outcomes = [value for value in outcomes if value is not None]
            if len(valid_outcomes) != len(outcomes):
                raise ValueError("settlement lineage conflict")
            outcome_rate = Decimal(sum(valid_outcomes)) / len(valid_outcomes)
            mean_brier = sum((Decimal(row.brier_contribution) for row in rows), Decimal("0")) / len(
                rows
            )
            mean_log = sum(
                (Decimal(row.log_loss_contribution) for row in rows), Decimal("0")
            ) / len(rows)
            event_brier.append(mean_brier)
            event_log.append(mean_log)
            calibration_pairs.append((mean_probability, outcome_rate))
            session.merge(
                EventCalibrationMetric(
                    metric_id=deterministic_id("event-metric-v1", comparison_id, model, event_id),
                    comparison_id=comparison_id,
                    model=model,
                    independent_event_id=event_id,
                    row_count=len(rows),
                    mean_probability=str(mean_probability),
                    outcome_rate=str(outcome_rate),
                    brier=str(mean_brier),
                    log_loss=str(mean_log),
                    aggregation_policy="MACRO_EVENT_MEAN_OF_ROW_SCORES_V1",
                    created_at=utc_now(),
                )
            )
        results[model] = {
            "matched_events": len(matched),
            "unmatched_events": len(set(grouped) - matched),
            "rows_on_matched_events": sum(len(grouped[event]) for event in matched),
            "event_macro_brier": _mean(event_brier),
            "event_macro_log_loss": _mean(event_log),
            "event_ece": _event_ece(calibration_pairs),
            "brier_bootstrap_95": _bootstrap_ci(event_brier, seed=model),
            "log_loss_bootstrap_95": _bootstrap_ci(event_log, seed=model + "-log"),
        }
    session.commit()
    return {
        "comparison_id": comparison_id,
        "aggregation_policy": "MACRO_EVENT_MEAN_OF_ROW_SCORES_V1",
        "matched_events": len(matched),
        "models": results,
    }


def build_exact_slice_comparison(
    session: Session,
    *,
    partition_id: str,
    market_run_id: str,
    crypto_run_id: str,
) -> dict[str, Any]:
    members = list(
        session.scalars(
            select(EvidenceExpansionMember).where(
                EvidenceExpansionMember.partition_id == partition_id
            )
        )
    )
    pairs = [row for row in members if row.paired_market_forecast_id is not None]
    metrics: dict[str, list[CalibrationObservation]] = {
        "market_implied_v1": [],
        "crypto_v2": [],
    }
    matched_events: set[str] = set()
    for member in pairs:
        crypto = session.scalar(
            select(CalibrationObservation).where(
                CalibrationObservation.run_id == crypto_run_id,
                CalibrationObservation.forecast_id == member.source_forecast_id,
            )
        )
        market = session.scalar(
            select(CalibrationObservation).where(
                CalibrationObservation.run_id == market_run_id,
                CalibrationObservation.forecast_id == member.paired_market_forecast_id,
            )
        )
        if crypto is None or market is None:
            continue
        if crypto.forecast_timestamp != market.forecast_timestamp:
            raise ValueError("identical-slice timestamp conflict")
        metrics["crypto_v2"].append(crypto)
        metrics["market_implied_v1"].append(market)
        matched_events.add(member.independent_event_id)
    return {
        "partition_id": partition_id,
        "exact_time_pairs": len(metrics["crypto_v2"]),
        "matched_independent_events": len(matched_events),
        "unmatched_crypto_rows": len(members) - len(metrics["crypto_v2"]),
        "models": {model: _row_metric_summary(rows) for model, rows in metrics.items()},
    }


def _row_metric_summary(rows: list[CalibrationObservation]) -> dict[str, Any]:
    if not rows:
        return {"rows": 0, "brier": None, "log_loss": None, "ece": None}
    return {
        "rows": len(rows),
        "brier": str(
            sum((Decimal(row.brier_contribution) for row in rows), Decimal("0")) / len(rows)
        ),
        "log_loss": str(
            sum((Decimal(row.log_loss_contribution) for row in rows), Decimal("0")) / len(rows)
        ),
        "ece": _event_ece(
            [
                (
                    Decimal(row.forecast_probability),
                    Decimal(settlement_value(row.settlement_result) or 0),
                )
                for row in rows
            ]
        ),
    }


def _event_ece(values: list[tuple[Decimal, Decimal]]) -> str | None:
    if not values:
        return None
    bins: dict[int, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    for probability, outcome in values:
        bins[min(int(probability * 10), 9)].append((probability, outcome))
    total = Decimal(len(values))
    return str(
        sum(
            Decimal(len(rows))
            / total
            * abs(
                sum((row[0] for row in rows), Decimal("0")) / len(rows)
                - sum((row[1] for row in rows), Decimal("0")) / len(rows)
            )
            for rows in bins.values()
        )
    )


def _bootstrap_ci(values: list[Decimal], *, seed: str) -> list[str] | None:
    if len(values) < 10:
        return None
    generator = random.Random(hashlib.sha256(seed.encode()).digest())
    samples = []
    for _ in range(2000):
        sample = [generator.choice(values) for _ in values]
        samples.append(sum(sample, Decimal("0")) / len(sample))
    samples.sort()
    return [str(samples[49]), str(samples[1949])]


def _mean(values: list[Decimal]) -> str | None:
    return str(sum(values, Decimal("0")) / len(values)) if values else None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
