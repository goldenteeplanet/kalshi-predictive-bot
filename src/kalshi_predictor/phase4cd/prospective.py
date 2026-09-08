"""Strict-causality, research-only paired forecast capture and reconciliation."""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from kalshi_predictor.crypto.assets import symbol_from_event_ticker
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    CryptoFeature,
    Market,
    MarketSnapshot,
    ProspectiveCaptureRejection,
    ProspectiveCaptureRun,
    ProspectivePairedCapture,
    ProspectivePairEvaluation,
    Settlement,
)
from kalshi_predictor.forecasting.base import ForecastInput
from kalshi_predictor.forecasting.crypto_v2 import CryptoV2Forecaster
from kalshi_predictor.forecasting.market_implied import MarketImpliedForecaster
from kalshi_predictor.phase4cd.range_comparator import audit_range_comparator
from kalshi_predictor.phase4cd.reconciliation_audit import (
    prospective_evaluation_values,
    settlement_lineage_hash,
)
from kalshi_predictor.utils.time import parse_datetime
from kalshi_predictor.weather.linker import WEATHER_TICKER_PREFIXES

ZERO = Decimal("0")
MIN_EXECUTABLE_EDGE = Decimal("0.05")
EXECUTABLE_MARKET_STATUSES = ("active", "open")
PAIRED_BOOTSTRAP_SEED = 404019
PAIRED_BOOTSTRAP_RESAMPLES = 5000


@dataclass(frozen=True)
class CaptureResult:
    run_id: str
    status: str
    scanned: int
    captured: int
    rejected: int
    rejection_counts: dict[str, int]
    independent_events: int
    deferred: int = 0


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _snapshot_payload(row: MarketSnapshot) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _candidate_category(market: Market) -> dict[str, Any]:
    """Classify from structured identifiers only; never inspect title or outcome fields."""
    raw = decode_json(market.raw_json)
    identifiers = {
        "series_ticker": market.series_ticker or raw.get("series_ticker"),
        "event_ticker": market.event_ticker or raw.get("event_ticker"),
        "ticker": market.ticker,
    }
    symbols = sorted(
        {
            symbol
            for value in identifiers.values()
            if (symbol := symbol_from_event_ticker(value)) is not None
        }
    )
    normalized_ids = [str(value or "").upper() for value in identifiers.values()]
    categories = [
        str(raw.get(key) or "").strip().lower()
        for key in ("category", "market_category", "series_category")
        if raw.get(key)
    ]
    if len(symbols) == 1:
        verdict = "EXACT_CRYPTO_CANDIDATE"
    elif len(symbols) > 1:
        verdict = "AMBIGUOUS_CRYPTO_CATEGORY"
    elif any(value.startswith(WEATHER_TICKER_PREFIXES) for value in normalized_ids):
        verdict = "CONFIRMED_NON_CRYPTO_CATEGORY"
    elif "crypto" in categories:
        verdict = "UNSUPPORTED_CRYPTO_ASSET"
    elif categories:
        verdict = "CONFIRMED_NON_CRYPTO_CATEGORY"
    else:
        verdict = "AMBIGUOUS_CATEGORY"
    payload = {
        "schema": "phase4w.candidate-category.v1",
        "verdict": verdict,
        "symbols": symbols,
        "structured_identifiers": identifiers,
        "structured_categories": sorted(set(categories)),
        "title_consulted": False,
        "settlement_consulted": False,
        "crypto_link_required": False,
    }
    payload["classification_hash"] = _hash(payload)
    return payload


def _comparator_lineage(feature_json: dict[str, Any]) -> dict[str, Any]:
    structured_terms = feature_json.get("structured_terms")
    components = (
        structured_terms.get("components", []) if isinstance(structured_terms, dict) else []
    )
    directions = [
        {
            "symbol": component.get("symbol"),
            "comparator": component.get("comparator"),
        }
        for component in components
        if isinstance(component, dict)
    ]
    anchor = Decimal(str(feature_json["market_price_anchor"]))
    final_probability = Decimal(str(feature_json["final_probability"]))
    raw_adjustment = Decimal(str(feature_json["adjustment"]))
    bounded_adjustment = final_probability - anchor
    return {
        "schema": "phase4t.comparator-lineage.v1",
        "raw_title_hash": _hash(feature_json.get("title", "")),
        "structured_terms_hash": _hash(structured_terms),
        "component_directions": directions,
        "fallback_direction": feature_json.get("direction_detected"),
        "signed_momentum": feature_json.get("momentum_score"),
        "raw_adjustment": str(raw_adjustment),
        "probability_bounds": feature_json.get("market_probability_bounds"),
        "market_price_anchor": str(anchor),
        "final_probability": str(final_probability),
        "final_bounded_adjustment": str(bounded_adjustment),
        "bound_clipped": bounded_adjustment != raw_adjustment,
    }


def _research_skip_recorder(
    records: list[dict[str, Any]],
    *,
    snapshot: MarketSnapshot,
) -> Any:
    def record(_session: Session, **payload: Any) -> None:
        available = payload.get("available_data") or {}
        required = payload.get("required_data") or {}
        records.append(
            {
                "model_name": payload.get("model_name"),
                "ticker": payload.get("ticker"),
                "reason": payload.get("reason"),
                "snapshot_id": snapshot.id,
                "snapshot_timestamp": _utc(snapshot.captured_at).isoformat(),
                "required_data_hash": _hash(required),
                "available_data_hash": _hash(available),
                "available_data": available,
            }
        )

    return record


def _observation_timestamps(value: Any) -> list[datetime]:
    found: list[datetime] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"observed_at", "source_timestamp", "captured_at", "timestamp"}:
                parsed = parse_datetime(item)
                if parsed is not None:
                    found.append(parsed)
            found.extend(_observation_timestamps(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_observation_timestamps(item))
    return found


def _strict_feature_bundle(
    source: Session, feature_json: dict[str, Any], cutoff: datetime
) -> tuple[dict[str, int], dict[str, str], dict[str, Any], datetime]:
    raw_ids = feature_json.get("component_feature_ids") or {}
    feature_ids = {str(k): int(v) for k, v in raw_ids.items() if v is not None}
    if not feature_ids and feature_json.get("crypto_feature_id") is not None:
        feature_ids = {"primary": int(feature_json["crypto_feature_id"])}
    if not feature_ids:
        raise ValueError("FEATURE_UNAVAILABLE")
    hashes: dict[str, str] = {}
    observations: dict[str, Any] = {}
    latest_generated: datetime | None = None
    for symbol, feature_id in sorted(feature_ids.items()):
        feature = source.get(CryptoFeature, feature_id)
        if feature is None:
            raise ValueError("FEATURE_UNAVAILABLE")
        generated = _utc(feature.generated_at)
        if generated > _utc(cutoff):
            raise ValueError("FEATURE_SOURCE_AFTER_CUTOFF")
        raw = decode_json(feature.raw_json)
        reference = raw.get("source_observation_ref")
        timestamps = _observation_timestamps(reference)
        if not timestamps:
            raise ValueError("SOURCE_TIMESTAMP_UNPROVEN")
        if any(_utc(item) > generated for item in timestamps):
            raise ValueError("SOURCE_AFTER_FEATURE")
        hashes[symbol] = _hash({"id": feature.id, "generated_at": generated, "raw": raw})
        observations[symbol] = reference
        latest_generated = max(latest_generated or generated, generated)
    assert latest_generated is not None
    return feature_ids, hashes, observations, latest_generated


def capture_prospective_pairs(
    research: Session,
    source: Session,
    *,
    limit: int = 1000,
    resume: bool = False,
    now: datetime | None = None,
    max_snapshot_age_seconds: int = 300,
    cycle_watermark: datetime | None = None,
    cycle_window_seconds: int = 5,
) -> CaptureResult:
    """Capture open-market pairs. The candidate query deliberately never references settlement."""
    config = {
        "lane": "PROSPECTIVE_RESEARCH",
        "zero_skew_seconds": 0,
        "max_snapshot_age_seconds": max_snapshot_age_seconds,
        "cycle_watermark": _utc(cycle_watermark).isoformat() if cycle_watermark else None,
        "cycle_window_seconds": cycle_window_seconds,
    }
    run_id = _hash({"kind": "phase4i", "config": config})
    run = research.get(ProspectiveCaptureRun, run_id)
    timestamp = _utc(now or datetime.now(UTC))
    if run is None:
        run = ProspectiveCaptureRun(
            run_id=run_id,
            status="RUNNING",
            scanned=0,
            captured=0,
            rejected=0,
            rejection_counts_json="{}",
            config_json=_json(config),
            started_at=timestamp,
            updated_at=timestamp,
        )
        run = research.merge(run)
        research.flush()
    elif resume and run.status == "EXHAUSTED":
        # A prospective source is not a frozen cohort: new events may sort before yesterday's
        # cursor. Rescan after exhaustion while immutable capture/rejection IDs keep retries
        # idempotent. CHECKPOINTED runs still resume strictly after the composite cursor.
        run.last_event_ticker = None
        run.last_snapshot_timestamp = None
        run.last_ticker = None
    query = (
        select(MarketSnapshot, Market)
        .join(Market, Market.ticker == MarketSnapshot.ticker)
        .where(
            func.lower(MarketSnapshot.status).in_(EXECUTABLE_MARKET_STATUSES),
            func.lower(Market.status).in_(EXECUTABLE_MARKET_STATUSES),
            MarketSnapshot.captured_at >= timestamp - timedelta(seconds=max_snapshot_age_seconds),
            MarketSnapshot.captured_at <= timestamp,
        )
        .order_by(Market.event_ticker, MarketSnapshot.captured_at, MarketSnapshot.ticker)
    )
    if cycle_watermark is not None:
        cycle_end = _utc(cycle_watermark)
        query = query.where(
            MarketSnapshot.captured_at >= cycle_end - timedelta(seconds=cycle_window_seconds),
            MarketSnapshot.captured_at <= cycle_end,
        )
    if cycle_watermark is None:
        query = query.limit(limit)
    if (
        cycle_watermark is None
        and run.last_event_ticker
        and run.last_snapshot_timestamp
        and run.last_ticker
    ):
        query = query.where(
            tuple_(Market.event_ticker, MarketSnapshot.captured_at, MarketSnapshot.ticker)
            > (run.last_event_ticker, run.last_snapshot_timestamp, run.last_ticker)
        )
    candidate_rows = list(source.execute(query))
    if cycle_watermark is not None:
        captured_ids = set(research.scalars(select(ProspectivePairedCapture.capture_id)))
        rejected_snapshot_ids = set(
            research.scalars(
                select(ProspectiveCaptureRejection.snapshot_id).where(
                    ProspectiveCaptureRejection.run_id == run_id
                )
            )
        )
        candidate_rows = [
            row
            for row in candidate_rows
            if _hash(
                [
                    row[1].event_ticker or row[1].ticker,
                    row[0].captured_at,
                    row[0].ticker,
                ]
            )
            not in captured_ids
            and row[0].id not in rejected_snapshot_ids
        ]
        rows = _event_round_robin(candidate_rows, limit)
    else:
        rows = candidate_rows
    deferred = max(len(candidate_rows) - len(rows), 0)
    counts = Counter(json.loads(run.rejection_counts_json))
    market_model = MarketImpliedForecaster()
    crypto_model = CryptoV2Forecaster(future_skew_seconds=0)
    crypto_model.begin_forecast_run()
    for snapshot, market in rows:
        event_ticker = market.event_ticker or market.ticker
        run.scanned += 1
        run.last_event_ticker, run.last_snapshot_timestamp, run.last_ticker = (
            event_ticker,
            snapshot.captured_at,
            snapshot.ticker,
        )
        capture_id = _hash([event_ticker, snapshot.captured_at, snapshot.ticker])
        if research.get(ProspectivePairedCapture, capture_id):
            continue
        reason: str | None = None
        details: dict[str, Any] = {}
        captured_skips: list[dict[str, Any]] = []
        try:
            category = _candidate_category(market)
            if category["verdict"] != "EXACT_CRYPTO_CANDIDATE":
                details = {"candidate_category": category}
                raise ValueError("MODEL_OR_CATEGORY_MISMATCH")
            input_row = ForecastInput(
                ticker=snapshot.ticker,
                captured_at=snapshot.captured_at,
                market_json=decode_json(snapshot.raw_market_json),
                orderbook_json=decode_json(snapshot.raw_orderbook_json),
            )
            market_output = market_model.forecast(input_row)
            with (
                source.no_autoflush,
                patch(
                    "kalshi_predictor.forecasting.crypto_v2.log_forecast_skip",
                    side_effect=_research_skip_recorder(captured_skips, snapshot=snapshot),
                ),
            ):
                crypto_output = crypto_model.forecast(source, snapshot)
            if market_output is None:
                raise ValueError("NO_VALID_EXECUTABLE_PRICE")
            if crypto_output is None:
                details = {
                    "snapshot_id": snapshot.id,
                    "snapshot_hash": _hash(_snapshot_payload(snapshot)),
                    "crypto_skip": captured_skips[-1] if captured_skips else None,
                    "skip_count": len(captured_skips),
                }
                raise ValueError("CRYPTO_MODEL_INPUT_INVALID")
            cutoff = _utc(snapshot.captured_at)
            if (
                _utc(market_output.forecasted_at) != cutoff
                or _utc(crypto_output.forecasted_at) != cutoff
            ):
                raise ValueError("PAIR_TIMESTAMP_MISMATCH")
            feature_ids, feature_hashes, observations, generated_at = _strict_feature_bundle(
                source, crypto_output.feature_json, cutoff
            )
            snapshot_payload = _snapshot_payload(snapshot)
            snapshot_hash = _hash(snapshot_payload)
            comparator_lineage = _comparator_lineage(crypto_output.feature_json)
            primary_feature_id = next(iter(feature_ids.values()), None)
            primary_feature = source.get(CryptoFeature, primary_feature_id)
            feature_payload = (
                {
                    column.name: getattr(primary_feature, column.name)
                    for column in primary_feature.__table__.columns
                }
                if primary_feature is not None
                else None
            )
            range_verdict = audit_range_comparator(
                raw_market=decode_json(snapshot.raw_market_json),
                structured_terms=crypto_output.feature_json.get("structured_terms"),
                feature=feature_payload,
                cutoff=cutoff,
                settlement_target=market.close_time,
            )
            persisted_at = timestamp
            latency = {
                "source_to_feature_ms": max(
                    int(
                        (
                            generated_at
                            - max(_utc(x) for x in _observation_timestamps(observations))
                        ).total_seconds()
                        * 1000
                    ),
                    0,
                ),
                "feature_to_snapshot_ms": int((cutoff - generated_at).total_seconds() * 1000),
                "snapshot_to_forecast_ms": 0,
                "capture_persistence_ms": max(
                    int((persisted_at - cutoff).total_seconds() * 1000), 0
                ),
            }
            bundle = {
                "ticker": snapshot.ticker,
                "event_ticker": event_ticker,
                "cutoff": cutoff,
                "snapshot_hash": snapshot_hash,
                "feature_hashes": feature_hashes,
                "market_probability": str(market_output.yes_probability),
                "crypto_probability": str(crypto_output.yes_probability),
                "comparator_lineage_hash": _hash(comparator_lineage),
                "range_comparator_verdict_hash": range_verdict["verdict_hash"],
                "models": [market_output.model_name, crypto_output.model_name],
            }
            research.add(
                ProspectivePairedCapture(
                    capture_id=capture_id,
                    run_id=run_id,
                    ticker=snapshot.ticker,
                    event_ticker=event_ticker,
                    series_ticker=market.series_ticker,
                    snapshot_id=snapshot.id,
                    snapshot_timestamp=cutoff,
                    snapshot_hash=snapshot_hash,
                    feature_ids_json=_json(feature_ids),
                    feature_hashes_json=_json(feature_hashes),
                    source_observations_json=_json(observations),
                    market_probability=str(market_output.yes_probability),
                    crypto_probability=str(crypto_output.yes_probability),
                    model_versions_json=_json(
                        {"market": market_output.model_name, "crypto": crypto_output.model_name}
                    ),
                    comparator_lineage_json=_json(comparator_lineage),
                    range_comparator_verdict_json=_json(range_verdict),
                    best_yes_bid=snapshot.best_yes_bid,
                    best_yes_ask=snapshot.best_yes_ask,
                    spread=snapshot.spread,
                    liquidity=market.liquidity_dollars,
                    settlement_target=market.close_time,
                    bundle_hash=_hash(bundle),
                    latency_json=_json(latency),
                    persisted_at=persisted_at,
                )
            )
            run.captured += 1
        except ValueError as exc:
            reason = str(exc)
            details = details | {"zero_skew_seconds": 0}
        finally:
            for pending in list(source.new):
                source.expunge(pending)
        if reason:
            rejection_id = _hash([capture_id, reason])
            if research.get(ProspectiveCaptureRejection, rejection_id) is None:
                research.add(
                    ProspectiveCaptureRejection(
                        rejection_id=rejection_id,
                        run_id=run_id,
                        snapshot_id=snapshot.id,
                        ticker=snapshot.ticker,
                        event_ticker=event_ticker,
                        snapshot_timestamp=snapshot.captured_at,
                        reason=reason,
                        details_json=_json(details),
                        created_at=timestamp,
                    )
                )
                run.rejected += 1
                counts[reason] += 1
        run.updated_at = timestamp
        run.rejection_counts_json = _json(dict(counts))
        research.commit()
    crypto_model.end_forecast_run()
    run.status = "CHECKPOINTED" if deferred else "EXHAUSTED"
    run.updated_at = timestamp
    research.commit()
    independent = (
        research.scalar(
            select(
                __import__("sqlalchemy").func.count(
                    __import__("sqlalchemy").distinct(ProspectivePairedCapture.event_ticker)
                )
            )
        )
        or 0
    )
    return CaptureResult(
        run_id,
        run.status,
        run.scanned,
        run.captured,
        run.rejected,
        dict(counts),
        independent,
        deferred,
    )


def _event_round_robin(rows: list[Any], limit: int) -> list[Any]:
    """Outcome-blind, deterministic event coverage for a bounded committed cycle."""
    queues: dict[str, list[Any]] = {}
    for row in rows:
        market = row[1]
        queues.setdefault(market.event_ticker or market.ticker, []).append(row)
    selected: list[Any] = []
    event_ids = sorted(queues)
    while len(selected) < limit and event_ids:
        remaining: list[str] = []
        for event_id in event_ids:
            if len(selected) >= limit:
                remaining.append(event_id)
                continue
            selected.append(queues[event_id].pop(0))
            if queues[event_id]:
                remaining.append(event_id)
        event_ids = remaining
    return selected


def reconcile_prospective_pairs(
    research: Session,
    source: Session,
    *,
    limit: int = 5000,
    fees: Decimal = ZERO,
    slippage: Decimal = ZERO,
) -> dict[str, Any]:
    existing_evaluations = list(research.scalars(select(ProspectivePairEvaluation)))
    existing_captures = {
        row.capture_id: row for row in research.scalars(select(ProspectivePairedCapture))
    }
    reasons: Counter[str] = Counter()
    for evaluation in existing_evaluations:
        capture = existing_captures.get(evaluation.capture_id)
        settlement = source.get(Settlement, capture.ticker) if capture else None
        if settlement is not None and _settlement_hash(settlement) != evaluation.settlement_hash:
            reasons["SETTLEMENT_LINEAGE_CONFLICT"] += 1

    captures = list(
        research.scalars(
            select(ProspectivePairedCapture)
            .outerjoin(
                ProspectivePairEvaluation,
                ProspectivePairEvaluation.capture_id == ProspectivePairedCapture.capture_id,
            )
            .where(ProspectivePairEvaluation.capture_id.is_(None))
            .order_by(ProspectivePairedCapture.snapshot_timestamp, ProspectivePairedCapture.ticker)
            .limit(limit)
        )
    )
    for capture in captures:
        settlement = source.get(Settlement, capture.ticker)
        if settlement is None or settlement.settled_at is None or settlement.result is None:
            reasons["UNSETTLED"] += 1
            continue
        if _utc(settlement.settled_at) <= _utc(capture.snapshot_timestamp):
            reasons["SETTLEMENT_INVALID"] += 1
            continue
        values = prospective_evaluation_values(
            {column.name: getattr(capture, column.name) for column in capture.__table__.columns},
            {
                column.name: getattr(settlement, column.name)
                for column in settlement.__table__.columns
            },
            fees=fees,
            slippage=slippage,
            minimum_executable_edge=MIN_EXECUTABLE_EDGE,
        )
        research.add(
            ProspectivePairEvaluation(
                evaluation_id=values["evaluation_id"],
                capture_id=values["capture_id"],
                independent_event_id=values["independent_event_id"],
                settled_at=values["settled_at"],
                settlement_hash=values["settlement_hash"],
                settlement_updated_at=values["settlement_updated_at"],
                outcome=values["outcome"],
                market_brier=values["market_brier"],
                crypto_brier=values["model_brier"],
                market_log_loss=values["market_log_loss"],
                crypto_log_loss=values["model_log_loss"],
                probability_advantage=values["probability_advantage"],
                crossing_spread_cost=values["crossing_spread_cost"],
                fees=values["fees"],
                slippage=values["slippage"],
                gross_edge=values["gross_edge"],
                net_edge=values["net_edge"],
                terminal_reason=values["terminal_reason"],
                hypothetical_pnl=values["hypothetical_pnl"],
                created_at=datetime.now(UTC),
            )
        )
        reasons[values["terminal_reason"]] += 1
    research.commit()
    return prospective_status(research) | {"batch_reasons": dict(reasons)}


def watch_canonical_settlements(
    research: Session,
    source: Session,
    *,
    limit: int = 5000,
) -> dict[str, Any]:
    """Reconcile only immutable, previously captured tickers after capture selection ends."""
    captured_tickers = set(research.scalars(select(ProspectivePairedCapture.ticker)))
    if not captured_tickers:
        return {
            "scope": "PREVIOUSLY_CAPTURED_TICKERS_ONLY",
            "captured_tickers": 0,
            "canonical_settlements": 0,
            "result": prospective_status(research),
        }
    canonical = list(
        source.scalars(
            select(Settlement).where(
                Settlement.ticker.in_(captured_tickers),
                Settlement.settled_at.is_not(None),
                Settlement.result.is_not(None),
            )
        )
    )
    result = reconcile_prospective_pairs(research, source, limit=limit)
    if result.get("batch_reasons", {}).get("SETTLEMENT_LINEAGE_CONFLICT"):
        raise RuntimeError("SETTLEMENT_LINEAGE_CONFLICT")
    return {
        "scope": "PREVIOUSLY_CAPTURED_TICKERS_ONLY",
        "captured_tickers": len(captured_tickers),
        "canonical_settlements": len(canonical),
        "result": result,
    }


def prospective_status(research: Session) -> dict[str, Any]:
    captures = list(research.scalars(select(ProspectivePairedCapture)))
    evaluations = list(research.scalars(select(ProspectivePairEvaluation)))
    rejections = list(research.scalars(select(ProspectiveCaptureRejection)))
    lineage_payloads = [
        json.loads(row.comparator_lineage_json)
        for row in captures
        if row.comparator_lineage_json is not None
    ]
    range_verdicts = [
        json.loads(row.range_comparator_verdict_json)
        for row in captures
        if row.range_comparator_verdict_json is not None
    ]
    invalid_details = [
        json.loads(row.details_json)
        for row in rejections
        if row.reason == "CRYPTO_MODEL_INPUT_INVALID"
    ]
    exact_invalid_reasons = Counter(
        str(details["crypto_skip"]["reason"])
        for details in invalid_details
        if isinstance(details.get("crypto_skip"), dict) and details["crypto_skip"].get("reason")
    )
    category_details = [
        json.loads(row.details_json)
        for row in rejections
        if row.reason == "MODEL_OR_CATEGORY_MISMATCH"
    ]
    category_verdicts = Counter(
        str(details["candidate_category"]["verdict"])
        for details in category_details
        if isinstance(details.get("candidate_category"), dict)
        and details["candidate_category"].get("verdict")
    )

    def mean(field: str) -> str | None:
        return (
            str(sum((Decimal(getattr(row, field)) for row in evaluations), ZERO) / len(evaluations))
            if evaluations
            else None
        )

    latency = [json.loads(row.latency_json) for row in captures]
    event_counts = Counter(row.event_ticker for row in captures)
    asset_counts = Counter(
        row.series_ticker or row.event_ticker.split("-", 1)[0] for row in captures
    )
    horizon_counts = Counter(_horizon_bucket(row) for row in captures)
    max_event_share = (
        str(Decimal(max(event_counts.values())) / Decimal(len(captures))) if captures else None
    )
    max_asset_share = (
        str(Decimal(max(asset_counts.values())) / Decimal(len(captures))) if captures else None
    )
    max_horizon_share = (
        str(Decimal(max(horizon_counts.values())) / Decimal(len(captures))) if captures else None
    )
    by_event: dict[str, list[ProspectivePairEvaluation]] = {}
    for row in evaluations:
        by_event.setdefault(row.independent_event_id, []).append(row)
    capture_by_id = {row.capture_id: row for row in captures}
    settled_capture_ids = {row.capture_id for row in evaluations}
    probability_deltas = [
        abs(Decimal(row.crypto_probability) - Decimal(row.market_probability)) for row in captures
    ]
    settled_probability_deltas = [
        abs(
            Decimal(capture_by_id[row.capture_id].crypto_probability)
            - Decimal(capture_by_id[row.capture_id].market_probability)
        )
        for row in evaluations
    ]

    def ece(probability_field: str) -> str | None:
        if not evaluations:
            return None
        bins: dict[int, list[tuple[Decimal, int]]] = {}
        for row in evaluations:
            probability = Decimal(getattr(capture_by_id[row.capture_id], probability_field))
            bins.setdefault(min(int(probability * 10), 9), []).append((probability, row.outcome))
        total = Decimal(len(evaluations))
        value = ZERO
        for values in bins.values():
            confidence = sum((item[0] for item in values), ZERO) / len(values)
            accuracy = Decimal(sum(item[1] for item in values)) / len(values)
            value += Decimal(len(values)) / total * abs(confidence - accuracy)
        return str(value)

    def event_macro(field: str) -> str | None:
        if not by_event:
            return None
        event_means = [
            sum((Decimal(getattr(row, field)) for row in rows), ZERO) / len(rows)
            for rows in by_event.values()
        ]
        return str(sum(event_means, ZERO) / len(event_means))

    def paired_bootstrap() -> dict[str, Any]:
        event_count = len(by_event)
        policy = {
            "resampling_unit": "independent_event",
            "seed": PAIRED_BOOTSTRAP_SEED,
            "resamples": PAIRED_BOOTSTRAP_RESAMPLES,
            "minimum_events": 30,
        }
        if event_count < 30:
            return policy | {
                "permitted": False,
                "effective_event_n": event_count,
                "reason": "INSUFFICIENT_INDEPENDENT_EVENTS",
                "intervals": None,
            }
        fields = {
            "brier_delta_crypto_minus_market": ("crypto_brier", "market_brier"),
            "log_loss_delta_crypto_minus_market": ("crypto_log_loss", "market_log_loss"),
        }
        event_deltas: dict[str, list[Decimal]] = {}
        for label, (crypto_field, market_field) in fields.items():
            event_deltas[label] = [
                sum(
                    (
                        Decimal(getattr(row, crypto_field)) - Decimal(getattr(row, market_field))
                        for row in rows
                    ),
                    ZERO,
                )
                / len(rows)
                for rows in by_event.values()
            ]
        rng = random.Random(PAIRED_BOOTSTRAP_SEED)
        samples: dict[str, list[Decimal]] = {label: [] for label in fields}
        for _ in range(PAIRED_BOOTSTRAP_RESAMPLES):
            indices = [rng.randrange(event_count) for _ in range(event_count)]
            for label, values in event_deltas.items():
                samples[label].append(sum((values[index] for index in indices), ZERO) / event_count)
        lower_index = int(PAIRED_BOOTSTRAP_RESAMPLES * 0.025)
        upper_index = int(PAIRED_BOOTSTRAP_RESAMPLES * 0.975) - 1
        intervals = {}
        for label, values in samples.items():
            ordered = sorted(values)
            intervals[label] = {
                "estimate": str(sum(event_deltas[label], ZERO) / event_count),
                "lower_95": str(ordered[lower_index]),
                "upper_95": str(ordered[upper_index]),
            }
        return policy | {
            "permitted": True,
            "effective_event_n": event_count,
            "reason": None,
            "intervals": intervals,
        }

    runs = list(research.scalars(select(ProspectiveCaptureRun)))
    elapsed = sum(
        max((_utc(row.updated_at) - _utc(row.started_at)).total_seconds(), 0.001) for row in runs
    )
    scanned = sum(row.scanned for row in runs)
    return {
        "lane": "PROSPECTIVE_RESEARCH",
        "pairs": len(captures),
        "independent_events": len({row.event_ticker for row in captures}),
        "causal_pass": len(captures),
        "causal_reject": len(rejections),
        "rejection_counts": dict(Counter(row.reason for row in rejections)),
        "crypto_invalid_input_reasons": {
            "exact": dict(sorted(exact_invalid_reasons.items())),
            "historical_reason_unavailable": sum(
                not isinstance(details.get("crypto_skip"), dict) for details in invalid_details
            ),
        },
        "candidate_category_funnel": {
            "forward_classified_rejections": sum(category_verdicts.values()),
            "verdicts": dict(sorted(category_verdicts.items())),
            "historical_reason_unavailable": sum(
                not isinstance(details.get("candidate_category"), dict)
                for details in category_details
            ),
            "title_consulted": False,
            "settlement_consulted": False,
            "crypto_link_required_for_classification": False,
        },
        "settled_pairs": len(evaluations),
        "unsettled_pairs": len(captures) - len(evaluations),
        "matched_identical_time_rows": len(evaluations),
        "calibration": {
            "market_brier": mean("market_brier"),
            "crypto_brier": mean("crypto_brier"),
            "market_log_loss": mean("market_log_loss"),
            "crypto_log_loss": mean("crypto_log_loss"),
            "market_ece": ece("market_probability"),
            "crypto_ece": ece("crypto_probability"),
            "event_macro_market_brier": event_macro("market_brier"),
            "event_macro_crypto_brier": event_macro("crypto_brier"),
            "event_macro_market_log_loss": event_macro("market_log_loss"),
            "event_macro_crypto_log_loss": event_macro("crypto_log_loss"),
        },
        "paired_event_bootstrap": paired_bootstrap(),
        "probability_comparison": {
            "all_rows_equal": sum(delta == ZERO for delta in probability_deltas),
            "all_rows_different": sum(delta != ZERO for delta in probability_deltas),
            "settled_rows_equal": sum(delta == ZERO for delta in settled_probability_deltas),
            "settled_rows_different": sum(delta != ZERO for delta in settled_probability_deltas),
            "max_absolute_delta": str(max(probability_deltas, default=ZERO)),
            "diagnosis": (
                "IMMUTABLE_CAPTURED_PROBABILITIES_IDENTICAL"
                if probability_deltas and all(delta == ZERO for delta in probability_deltas)
                else "IMMUTABLE_CAPTURED_PROBABILITIES_DIFFER"
            ),
        },
        "comparator_lineage": {
            "forward_lineage_rows": len(lineage_payloads),
            "historical_rows_not_backfilled": len(captures) - len(lineage_payloads),
            "component_directions": dict(
                sorted(
                    Counter(
                        str(component.get("comparator") or "UNKNOWN")
                        for lineage in lineage_payloads
                        for component in lineage.get("component_directions", [])
                    ).items()
                )
            ),
            "fallback_directions": dict(
                sorted(
                    Counter(
                        str(lineage.get("fallback_direction") or "UNKNOWN")
                        for lineage in lineage_payloads
                    ).items()
                )
            ),
            "neutral_signed_momentum": sum(
                Decimal(str(lineage.get("signed_momentum") or "0")) == ZERO
                for lineage in lineage_payloads
            ),
            "nonzero_raw_adjustment": sum(
                Decimal(str(lineage.get("raw_adjustment") or "0")) != ZERO
                for lineage in lineage_payloads
            ),
            "bound_clipped": sum(
                bool(lineage.get("bound_clipped")) for lineage in lineage_payloads
            ),
        },
        "range_comparator_identifiability": {
            "forward_verdict_rows": len(range_verdicts),
            "historical_rows_not_backfilled": len(captures) - len(range_verdicts),
            "verdicts": dict(
                sorted(Counter(str(row.get("verdict")) for row in range_verdicts).items())
            ),
            "reason_codes": dict(
                sorted(
                    Counter(
                        str(reason)
                        for row in range_verdicts
                        for reason in row.get("reason_codes", [])
                    ).items()
                )
            ),
            "contract_structures": dict(
                sorted(
                    Counter(
                        str(row.get("contract_structure") or "UNKNOWN") for row in range_verdicts
                    ).items()
                )
            ),
            "candidate_probabilities": sum(
                row.get("candidate_probability") is not None for row in range_verdicts
            ),
            "research_model_deployed": any(
                row.get("model_version") is not None for row in range_verdicts
            ),
        },
        "executable": {
            "positive_gross": sum(Decimal(x.gross_edge) > 0 for x in evaluations),
            "positive_net": sum(Decimal(x.net_edge) > 0 for x in evaluations),
            "qualified": sum(x.terminal_reason == "EVALUATED_EXECUTABLE" for x in evaluations),
            "terminal_reasons": dict(Counter(x.terminal_reason for x in evaluations)),
            "mean_probability_advantage": mean("probability_advantage"),
            "mean_crossing_spread_cost": mean("crossing_spread_cost"),
            "mean_fees": mean("fees"),
            "mean_slippage": mean("slippage"),
            "timing_decay": "0",
            "timing_decay_basis": "EXACT_SNAPSHOT_AND_FORECAST_TIMESTAMP",
            "mean_gross_edge": mean("gross_edge"),
            "mean_net_edge": mean("net_edge"),
            "hypothetical_pnl": str(
                sum(
                    (
                        Decimal(x.hypothetical_pnl)
                        for x in evaluations
                        if x.hypothetical_pnl is not None
                    ),
                    ZERO,
                )
            ),
        },
        "latency_mean_ms": (
            {key: sum(int(row[key]) for row in latency) / len(latency) for key in latency[0]}
            if latency
            else {}
        ),
        "concentration": {
            "max_event_share": max_event_share,
            "max_asset_share": max_asset_share,
            "max_horizon_share": max_horizon_share,
            "by_asset": dict(sorted(asset_counts.items())),
            "by_horizon": dict(sorted(horizon_counts.items())),
            "settled_by_asset": dict(
                sorted(
                    Counter(
                        capture_by_id[capture_id].series_ticker
                        or capture_by_id[capture_id].event_ticker.split("-", 1)[0]
                        for capture_id in settled_capture_ids
                    ).items()
                )
            ),
            "settled_by_horizon": dict(
                sorted(
                    Counter(
                        _horizon_bucket(capture_by_id[capture_id])
                        for capture_id in settled_capture_ids
                    ).items()
                )
            ),
            "minimum_independent_events_for_claim": 30,
            "repeatable_edge_claim_permitted": len(by_event) >= 30
            and max_event_share is not None
            and Decimal(max_event_share) <= Decimal("0.20"),
        },
        "cohort_growth": _cohort_growth(captures),
        "throughput_rows_per_second": str(Decimal(scanned) / Decimal(str(elapsed)))
        if runs
        else None,
        "performance_metrics_merged": False,
    }


def _settlement_hash(settlement: Settlement) -> str:
    return settlement_lineage_hash(
        {column.name: getattr(settlement, column.name) for column in settlement.__table__.columns}
    )


def _horizon_bucket(capture: ProspectivePairedCapture) -> str:
    if capture.settlement_target is None:
        return "UNKNOWN"
    seconds = (_utc(capture.settlement_target) - _utc(capture.snapshot_timestamp)).total_seconds()
    if seconds <= 3600:
        return "LE_1H"
    if seconds <= 21600:
        return "LE_6H"
    if seconds <= 86400:
        return "LE_24H"
    return "GT_24H"


def _captures_by_run(
    captures: list[ProspectivePairedCapture],
) -> dict[str, list[ProspectivePairedCapture]]:
    grouped: dict[str, list[ProspectivePairedCapture]] = {}
    for capture in captures:
        grouped.setdefault(capture.run_id, []).append(capture)
    return grouped


def _cohort_growth(captures: list[ProspectivePairedCapture]) -> list[dict[str, Any]]:
    grouped = _captures_by_run(captures)
    ordered = sorted(
        grouped.items(),
        key=lambda item: min(_utc(row.snapshot_timestamp) for row in item[1]),
    )
    seen_events: set[str] = set()
    growth: list[dict[str, Any]] = []
    for run_id, rows in ordered:
        events = {row.event_ticker for row in rows}
        new_events = events - seen_events
        repeated_events = events & seen_events
        growth.append(
            {
                "run_id": run_id,
                "new_rows": len(rows),
                "new_events": len(new_events),
                "repeated_events": len(repeated_events),
                "new_event_rows": sum(row.event_ticker in new_events for row in rows),
                "repeated_event_rows": sum(row.event_ticker in seen_events for row in rows),
                "assets": dict(
                    sorted(
                        Counter(
                            row.series_ticker or row.event_ticker.split("-", 1)[0] for row in rows
                        ).items()
                    )
                ),
                "horizons": dict(sorted(Counter(_horizon_bucket(row) for row in rows).items())),
            }
        )
        seen_events.update(events)
    return growth
