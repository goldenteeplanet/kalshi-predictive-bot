from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as datetime_time
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    CalibrationObservation,
    CanonicalEvaluation,
    CryptoFeatureLineage,
    Feature,
    Forecast,
    Market,
    MarketLeg,
    MarketSnapshot,
    ReplayDisposition,
    ResearchCheckpoint,
    ResearchPartition,
    ResearchRun,
    Settlement,
)
from kalshi_predictor.phase4cd.domain import (
    HISTORICAL_REPLAY,
    correlation_cluster_id,
    deterministic_id,
    independent_event_id,
    score_probability,
    settlement_value,
    validate_point_in_time,
)
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now

EVALUATED_EXECUTABLE = "EVALUATED_EXECUTABLE"
CALIBRATION_ONLY = "CALIBRATION_ONLY"
NO_PREDECISION_SNAPSHOT = "NO_PREDECISION_SNAPSHOT"
NO_VALID_EXECUTABLE_PRICE = "NO_VALID_EXECUTABLE_PRICE"
NON_POSITIVE_GROSS_EDGE = "NON_POSITIVE_GROSS_EDGE"
FEES_CONSUME_EDGE = "FEES_CONSUME_EDGE"
SLIPPAGE_CONSUMES_EDGE = "SLIPPAGE_CONSUMES_EDGE"
MISSING_POINT_IN_TIME_FEATURES = "MISSING_POINT_IN_TIME_FEATURES"
SETTLEMENT_INVALID = "SETTLEMENT_INVALID"
LOOKAHEAD_REJECTED = "LOOKAHEAD_REJECTED"
MODEL_OR_CATEGORY_MISMATCH = "MODEL_OR_CATEGORY_MISMATCH"
DISPOSITIONS = (
    EVALUATED_EXECUTABLE,
    CALIBRATION_ONLY,
    NO_PREDECISION_SNAPSHOT,
    NO_VALID_EXECUTABLE_PRICE,
    NON_POSITIVE_GROSS_EDGE,
    FEES_CONSUME_EDGE,
    SLIPPAGE_CONSUMES_EDGE,
    MISSING_POINT_IN_TIME_FEATURES,
    SETTLEMENT_INVALID,
    LOOKAHEAD_REJECTED,
    MODEL_OR_CATEGORY_MISMATCH,
)


@dataclass(frozen=True)
class ReplayResult:
    run_id: str
    processed: int
    evaluated: int
    skipped: int
    errors: int
    calibration_observations: int
    disposition_counts: dict[str, int]
    elapsed_seconds: float
    evaluations_per_minute: float
    status: str


@dataclass(frozen=True)
class _Outcome:
    disposition: str
    details: dict[str, Any]
    calibration: CalibrationObservation | None = None
    evaluation: CanonicalEvaluation | None = None


def run_research_replay(
    session: Session,
    *,
    model: str,
    category: str = "all",
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 1000,
    resume: bool = False,
    checkpoint_every: int = 100,
    slippage: Decimal = Decimal("0"),
    require_verified_lineage: bool = False,
    settings: Settings | None = None,
) -> ReplayResult:
    if limit < 1 or checkpoint_every < 1 or slippage < 0:
        raise ValueError("invalid replay bounds")
    began = time.monotonic()
    cfg = {
        "model": model,
        "category": category,
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "limit": limit,
        "slippage": str(slippage),
        "require_verified_lineage": require_verified_lineage,
    }
    run = _get_run(session, cfg, resume)
    query = (
        select(Forecast, Market, Settlement)
        .join(Market, Market.ticker == Forecast.ticker)
        .join(Settlement, Settlement.ticker == Forecast.ticker)
        .where(Forecast.model_name == model)
        .order_by(Market.event_ticker, Forecast.forecasted_at, Forecast.id)
    )
    if start_date:
        query = query.where(
            Forecast.forecasted_at >= datetime.combine(start_date, datetime_time.min, UTC)
        )
    if end_date:
        query = query.where(
            Forecast.forecasted_at <= datetime.combine(end_date, datetime_time.max, UTC)
        )
    if category != "all":
        query = query.join(MarketLeg, MarketLeg.ticker == Market.ticker).where(
            MarketLeg.category == category
        )
    if require_verified_lineage:
        query = query.join(
            CryptoFeatureLineage, CryptoFeatureLineage.forecast_id == Forecast.id
        ).where(
            CryptoFeatureLineage.verdict.in_(
                ("FEATURE_LINEAGE_VERIFIED", "FEATURE_RECONSTRUCTABLE_POINT_IN_TIME")
            )
        )
    cursor = _cursor(session, run) if resume else None
    if cursor:
        event, at, row_id = cursor
        query = query.where(
            or_(
                Market.event_ticker > event,
                and_(Market.event_ticker == event, Forecast.forecasted_at > at),
                and_(
                    Market.event_ticker == event, Forecast.forecasted_at == at, Forecast.id > row_id
                ),
            )
        )
    rows = list(session.execute(query.limit(limit)))
    part = _partition(session, run, category)
    counts = Counter(
        r.disposition
        for r in session.scalars(
            select(ReplayDisposition).where(ReplayDisposition.run_id == run.run_id)
        )
    )
    calibration_n = len(
        list(
            session.scalars(
                select(CalibrationObservation.observation_id).where(
                    CalibrationObservation.run_id == run.run_id
                )
            )
        )
    )
    resolved = settings or get_settings()
    for forecast, market, settlement in rows:
        result = _classify(session, forecast, market, settlement, resolved, slippage)
        if result.calibration:
            result.calibration.run_id = run.run_id
            result.calibration.observation_id = deterministic_id(
                "calibration-v1", run.run_id, forecast.id
            )
            if session.get(CalibrationObservation, result.calibration.observation_id) is None:
                session.add(result.calibration)
                calibration_n += 1
        if (
            result.evaluation
            and session.get(CanonicalEvaluation, result.evaluation.evaluation_id) is None
        ):
            session.add(result.evaluation)
            run.evaluated += 1
            part.evaluated += 1
        else:
            run.skipped += 1
            part.skipped += 1
        session.add(
            ReplayDisposition(
                disposition_id=deterministic_id("disposition-v1", run.run_id, forecast.id),
                run_id=run.run_id,
                forecast_id=int(forecast.id),
                market_ticker=market.ticker,
                event_ticker=market.event_ticker or market.ticker,
                model=forecast.model_name,
                forecast_timestamp=forecast.forecasted_at,
                disposition=result.disposition,
                calibration_eligible=int(result.calibration is not None),
                details_json=json.dumps(result.details, sort_keys=True),
                created_at=utc_now(),
            )
        )
        counts[result.disposition] += 1
        run.processed += 1
        part.processed += 1
        run.last_completed_event = market.event_ticker or market.ticker
        run.updated_at = utc_now()
        part.updated_at = run.updated_at
        if run.processed % checkpoint_every == 0:
            _checkpoint(session, run, counts)
            session.commit()
    run.status = "COMPLETED" if len(rows) < limit else "CHECKPOINTED"
    part.status = run.status
    run.updated_at = utc_now()
    part.updated_at = run.updated_at
    _checkpoint(session, run, counts)
    session.commit()
    elapsed = max(time.monotonic() - began, 0.000001)
    return ReplayResult(
        run.run_id,
        run.processed,
        run.evaluated,
        run.skipped,
        run.errors,
        calibration_n,
        {name: counts[name] for name in DISPOSITIONS},
        round(elapsed, 6),
        round(run.processed / elapsed * 60, 3),
        run.status,
    )


def _classify(
    session: Session,
    forecast: Forecast,
    market: Market,
    settlement: Settlement,
    settings: Settings,
    slippage: Decimal,
) -> _Outcome:
    actual = settlement_value(settlement.result)
    probability = to_decimal(forecast.yes_probability)
    if actual is None or settlement.settled_at is None:
        return _Outcome(SETTLEMENT_INVALID, {"result": settlement.result})
    if probability is None or not Decimal("0") <= probability <= Decimal("1"):
        return _Outcome(MODEL_OR_CATEGORY_MISMATCH, {"reason": "invalid_probability"})
    settled_utc = _as_utc(settlement.settled_at)
    forecast_utc = _as_utc(forecast.forecasted_at)
    if settled_utc <= forecast_utc:
        return _Outcome(LOOKAHEAD_REJECTED, {"reason": "settlement_not_after_decision"})
    event_id = independent_event_id(
        ticker=market.ticker, event_ticker=market.event_ticker, series_ticker=market.series_ticker
    )
    brier, log_loss = score_probability(probability, actual)
    calibration = CalibrationObservation(
        observation_id="",
        run_id="",
        forecast_id=int(forecast.id),
        market_ticker=market.ticker,
        event_ticker=market.event_ticker or market.ticker,
        model=forecast.model_name,
        forecast_timestamp=forecast.forecasted_at,
        settlement_timestamp=settlement.settled_at,
        forecast_probability=str(probability),
        settlement_result=settlement.result or "",
        brier_contribution=str(brier),
        log_loss_contribution=str(log_loss),
        independent_event_id=event_id,
        provenance_json=json.dumps({"forecast_id": forecast.id, "trade": False}, sort_keys=True),
        created_at=utc_now(),
    )
    feature = session.scalar(
        select(Feature)
        .where(Feature.ticker == forecast.ticker, Feature.generated_at <= forecast.forecasted_at)
        .order_by(Feature.generated_at.desc())
        .limit(1)
    )
    lineage = session.scalar(
        select(CryptoFeatureLineage).where(CryptoFeatureLineage.forecast_id == forecast.id)
    )
    lineage_verified = lineage is not None and lineage.verdict in {
        "FEATURE_LINEAGE_VERIFIED",
        "FEATURE_RECONSTRUCTABLE_POINT_IN_TIME",
    }
    if forecast.model_name != "market_implied_v1" and feature is None and not lineage_verified:
        return _Outcome(MISSING_POINT_IN_TIME_FEATURES, {"feature_required": True}, calibration)
    snapshot = session.scalar(
        select(MarketSnapshot)
        .where(
            MarketSnapshot.ticker == forecast.ticker,
            MarketSnapshot.captured_at <= forecast.forecasted_at,
        )
        .order_by(MarketSnapshot.captured_at.desc())
        .limit(1)
    )
    if snapshot is None:
        return _Outcome(NO_PREDECISION_SNAPSHOT, {}, calibration)
    try:
        validate_point_in_time(
            feature_timestamp=_as_utc(feature.generated_at) if feature else None,
            source_timestamp=(
                _as_utc(feature.source_timestamp) if feature and feature.source_timestamp else None
            ),
            snapshot_timestamp=_as_utc(snapshot.captured_at),
            decision_timestamp=_as_utc(forecast.forecasted_at),
            settlement_timestamp=_as_utc(settlement.settled_at),
        )
    except ValueError as exc:
        return _Outcome(LOOKAHEAD_REJECTED, {"reason": str(exc)}, calibration)
    yes_ask = to_decimal(forecast.best_yes_ask) or to_decimal(snapshot.best_yes_ask)
    no_ask = to_decimal(snapshot.best_no_ask)
    choices = []
    if yes_ask is not None:
        choices.append(("BUY_YES", yes_ask, probability - yes_ask))
    if settings.paper_allow_buy_no and no_ask is not None:
        choices.append(("BUY_NO", no_ask, Decimal("1") - probability - no_ask))
    if not choices:
        return _Outcome(NO_VALID_EXECUTABLE_PRICE, {}, calibration)
    side, price, gross = max(choices, key=lambda value: value[2])
    details = {"side": side, "price": str(price), "gross_edge": str(gross)}
    if gross <= 0:
        return _Outcome(NON_POSITIVE_GROSS_EDGE, details, calibration)
    if gross < settings.paper_min_edge:
        details["required_min_edge"] = str(settings.paper_min_edge)
        return _Outcome(CALIBRATION_ONLY, details, calibration)
    fee = settings.paper_default_fee_per_contract
    if gross - fee <= 0:
        details["fee"] = str(fee)
        return _Outcome(FEES_CONSUME_EDGE, details, calibration)
    net = gross - fee - slippage
    if net <= 0:
        details.update(fee=str(fee), slippage=str(slippage))
        return _Outcome(SLIPPAGE_CONSUMES_EDGE, details, calibration)
    won = (side == "BUY_YES" and actual == 1) or (side == "BUY_NO" and actual == 0)
    pnl = (Decimal("1") if won else Decimal("0")) - price - fee - slippage
    evaluation = CanonicalEvaluation(
        evaluation_id=deterministic_id(
            "canonical-evaluation-v1", HISTORICAL_REPLAY, forecast.id, snapshot.id
        ),
        source_lane=HISTORICAL_REPLAY,
        market_ticker=market.ticker,
        event_ticker=market.event_ticker,
        series_ticker=market.series_ticker,
        model=forecast.model_name,
        model_version=forecast.model_name.rsplit("_", 1)[-1],
        decision_timestamp=forecast.forecasted_at,
        forecast_timestamp=forecast.forecasted_at,
        forecast_probability=str(probability),
        snapshot_timestamp=snapshot.captured_at,
        executable_price=str(price),
        spread=snapshot.spread,
        liquidity=snapshot.open_interest_fp or snapshot.volume_fp,
        gross_edge=str(gross),
        fees=str(fee),
        slippage=str(slippage),
        net_edge=str(net),
        risk_result="RESEARCH_ONLY",
        settlement_timestamp=settlement.settled_at,
        settlement_result=settlement.result or "",
        realized_or_simulated_pnl=str(pnl),
        brier_contribution=str(brier),
        log_loss_contribution=str(log_loss),
        independent_event_id=event_id,
        correlation_cluster_id=correlation_cluster_id(
            independent_id=event_id, series_ticker=market.series_ticker
        ),
        provenance=json.dumps(
            {
                "forecast_id": forecast.id,
                "snapshot_id": snapshot.id,
                "feature_id": feature.id if feature else None,
                "lineage_id": lineage.lineage_id if lineage else None,
                "lineage_hash": lineage.provenance_hash if lineage else None,
            },
            sort_keys=True,
        ),
        created_at=utc_now(),
    )
    return _Outcome(EVALUATED_EXECUTABLE, details, calibration, evaluation)


def _cursor(session: Session, run: ResearchRun) -> tuple[str, datetime, int] | None:
    row = session.scalar(
        select(ReplayDisposition)
        .where(ReplayDisposition.run_id == run.run_id)
        .order_by(
            ReplayDisposition.event_ticker.desc(),
            ReplayDisposition.forecast_timestamp.desc(),
            ReplayDisposition.forecast_id.desc(),
        )
        .limit(1)
    )
    return (row.event_ticker, row.forecast_timestamp, row.forecast_id) if row else None


def _get_run(session: Session, cfg: dict[str, Any], resume: bool) -> ResearchRun:
    encoded = json.dumps(cfg, sort_keys=True)
    if resume:
        old = session.scalar(
            select(ResearchRun)
            .where(
                ResearchRun.model == cfg["model"],
                ResearchRun.category == cfg["category"],
                ResearchRun.config_json == encoded,
                ResearchRun.status.in_(("RUNNING", "CHECKPOINTED")),
            )
            .order_by(ResearchRun.updated_at.desc())
            .limit(1)
        )
        if old:
            old.status = "RUNNING"
            return old
    now = utc_now()
    run = ResearchRun(
        run_id=deterministic_id("research-run-v1", encoded, now.isoformat()),
        model=cfg["model"],
        category=cfg["category"],
        status="RUNNING",
        last_completed_event=None,
        processed=0,
        evaluated=0,
        skipped=0,
        errors=0,
        started_at=now,
        updated_at=now,
        config_json=encoded,
    )
    session.add(run)
    session.flush()
    return run


def _partition(session: Session, run: ResearchRun, category: str) -> ResearchPartition:
    row = session.scalar(
        select(ResearchPartition).where(
            ResearchPartition.run_id == run.run_id, ResearchPartition.partition_key == category
        )
    )
    if row:
        row.status = "RUNNING"
        return row
    row = ResearchPartition(
        run_id=run.run_id,
        partition_key=category,
        status="RUNNING",
        processed=0,
        evaluated=0,
        skipped=0,
        errors=0,
        updated_at=utc_now(),
    )
    session.add(row)
    return row


def _checkpoint(session: Session, run: ResearchRun, counts: Counter[str]) -> None:
    session.add(
        ResearchCheckpoint(
            run_id=run.run_id,
            last_completed_event=run.last_completed_event,
            processed=run.processed,
            evaluated=run.evaluated,
            skipped=run.skipped,
            errors=run.errors,
            disposition_counts_json=json.dumps(dict(counts), sort_keys=True),
            created_at=utc_now(),
        )
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
