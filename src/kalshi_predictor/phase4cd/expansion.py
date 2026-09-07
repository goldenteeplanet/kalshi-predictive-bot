from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import (
    CryptoFeatureLineage,
    EvidenceExpansionMember,
    EvidenceExpansionPartition,
    Forecast,
    Market,
    MarketSnapshot,
    Settlement,
)
from kalshi_predictor.phase4cd.domain import deterministic_id, independent_event_id
from kalshi_predictor.phase4cd.lineage import (
    FEATURE_LINEAGE_VERIFIED,
    FEATURE_RECONSTRUCTABLE_POINT_IN_TIME,
    make_crypto_lineage_record,
)
from kalshi_predictor.utils.time import utc_now

ModelT = TypeVar("ModelT")


@dataclass(frozen=True)
class ExpansionResult:
    partition_id: str
    status: str
    scanned: int
    admitted: int
    rejected: int
    independent_events: int
    paired_market_forecasts: int
    cohort_hash: str | None
    rejection_counts: dict[str, int]


def expand_verified_crypto_cohort(
    research: Session,
    source: Session,
    *,
    max_independent_events: int = 25,
    scan_limit: int = 20000,
    resume: bool = False,
    checkpoint_every: int = 100,
) -> ExpansionResult:
    if min(max_independent_events, scan_limit, checkpoint_every) < 1:
        raise ValueError("expansion bounds must be positive")
    config = {
        "model": "crypto_v2",
        "max_independent_events": max_independent_events,
        "scan_limit": scan_limit,
        "strict_lineage": True,
        "clock_skew_seconds": 0,
    }
    partition = _partition(research, config, resume)
    existing_members = list(
        research.scalars(
            select(EvidenceExpansionMember).where(
                EvidenceExpansionMember.partition_id == partition.partition_id
            )
        )
    )
    if resume and partition.status in {"COMPLETED", "EXHAUSTED"}:
        return ExpansionResult(
            partition.partition_id,
            partition.status,
            partition.scanned,
            partition.admitted,
            partition.rejected,
            partition.independent_events,
            sum(row.paired_market_forecast_id is not None for row in existing_members),
            partition.cohort_hash,
            dict(sorted(json.loads(partition.rejection_counts_json or "{}").items())),
        )
    admitted_events = {row.independent_event_id for row in existing_members}
    all_member_ids = set(research.scalars(select(EvidenceExpansionMember.source_forecast_id)))
    frozen_query = select(Forecast, Market).join(Market, Market.ticker == Forecast.ticker)
    if all_member_ids:
        frozen_query = frozen_query.where(Forecast.id.not_in(all_member_ids))
    frozen_events = {
        market.event_ticker or market.ticker for _, market in research.execute(frozen_query)
    }
    query = (
        select(Forecast, Market, Settlement)
        .join(Market, Market.ticker == Forecast.ticker)
        .join(Settlement, Settlement.ticker == Forecast.ticker)
        .where(Forecast.model_name == "crypto_v2")
        .order_by(Market.event_ticker, Forecast.forecasted_at, Forecast.id)
    )
    if resume and partition.last_event_ticker and partition.last_forecast_timestamp:
        query = query.where(
            or_(
                Market.event_ticker > partition.last_event_ticker,
                and_(
                    Market.event_ticker == partition.last_event_ticker,
                    Forecast.forecasted_at > partition.last_forecast_timestamp,
                ),
                and_(
                    Market.event_ticker == partition.last_event_ticker,
                    Forecast.forecasted_at == partition.last_forecast_timestamp,
                    Forecast.id > (partition.last_forecast_id or 0),
                ),
            )
        )
    rows = source.execute(query.limit(scan_limit))
    rejection_counts = Counter(json.loads(partition.rejection_counts_json or "{}"))
    paired_n = sum(row.paired_market_forecast_id is not None for row in existing_members)
    batch_scanned = 0
    for forecast, market, settlement in rows:
        event = market.event_ticker or market.ticker
        if event not in admitted_events and len(admitted_events) >= max_independent_events:
            break
        batch_scanned += 1
        partition.scanned += 1
        partition.last_event_ticker = event
        partition.last_forecast_timestamp = forecast.forecasted_at
        partition.last_forecast_id = int(forecast.id)
        reason = _eligibility_reason(source, forecast, settlement)
        if event in frozen_events:
            reason = "FROZEN_EVENT_EXCLUDED"
        if research.get(Forecast, forecast.id) is not None:
            reason = "FORECAST_ALREADY_PRESENT"
        snapshot = _snapshot(source, forecast)
        if snapshot is None:
            reason = "NO_PREDECISION_SNAPSHOT"
        lineage = make_crypto_lineage_record(forecast, source)
        if lineage.verdict not in {
            FEATURE_LINEAGE_VERIFIED,
            FEATURE_RECONSTRUCTABLE_POINT_IN_TIME,
        }:
            reason = lineage.verdict
        if reason:
            partition.rejected += 1
            rejection_counts[reason] += 1
        else:
            assert snapshot is not None
            _merge_row(research, market, Market)
            _merge_row(research, settlement, Settlement)
            _merge_row(research, snapshot, MarketSnapshot)
            _merge_row(research, forecast, Forecast)
            research.merge(lineage)
            paired = source.scalar(
                select(Forecast)
                .where(
                    Forecast.ticker == forecast.ticker,
                    Forecast.model_name == "market_implied_v1",
                    Forecast.forecasted_at == forecast.forecasted_at,
                )
                .order_by(Forecast.id)
                .limit(1)
            )
            if paired is not None:
                _merge_row(research, paired, Forecast)
                paired_n += 1
            event_id = independent_event_id(
                ticker=market.ticker,
                event_ticker=market.event_ticker,
                series_ticker=market.series_ticker,
            )
            source_hash = _source_hash(forecast, market, settlement, snapshot, lineage)
            research.merge(
                EvidenceExpansionMember(
                    member_id=deterministic_id(
                        "expansion-member-v1", partition.partition_id, forecast.id
                    ),
                    partition_id=partition.partition_id,
                    source_forecast_id=int(forecast.id),
                    paired_market_forecast_id=int(paired.id) if paired else None,
                    market_ticker=market.ticker,
                    event_ticker=event,
                    independent_event_id=event_id,
                    asset=_asset(forecast),
                    forecast_timestamp=forecast.forecasted_at,
                    settlement_timestamp=settlement.settled_at,
                    snapshot_id=int(snapshot.id),
                    lineage_id=lineage.lineage_id,
                    source_hash=source_hash,
                    created_at=utc_now(),
                )
            )
            # Autoflush is disabled globally; materialize identity keys so later
            # forecasts for the same market merge instead of queuing duplicates.
            research.flush()
            partition.admitted += 1
            admitted_events.add(event_id)
        partition.independent_events = len(admitted_events)
        partition.updated_at = utc_now()
        partition.rejection_counts_json = json.dumps(dict(rejection_counts), sort_keys=True)
        if batch_scanned % checkpoint_every == 0:
            partition.status = "CHECKPOINTED"
            research.commit()
    partition.status = (
        "COMPLETED"
        if len(admitted_events) >= max_independent_events
        else "CHECKPOINTED"
        if batch_scanned >= scan_limit
        else "EXHAUSTED"
    )
    partition.independent_events = len(admitted_events)
    partition.rejection_counts_json = json.dumps(dict(rejection_counts), sort_keys=True)
    research.flush()
    partition.cohort_hash = _cohort_hash(research, partition.partition_id)
    partition.updated_at = utc_now()
    research.commit()
    return ExpansionResult(
        partition.partition_id,
        partition.status,
        partition.scanned,
        partition.admitted,
        partition.rejected,
        partition.independent_events,
        paired_n,
        partition.cohort_hash,
        dict(sorted(rejection_counts.items())),
    )


def _partition(
    session: Session, config: dict[str, Any], resume: bool
) -> EvidenceExpansionPartition:
    encoded = json.dumps(config, sort_keys=True)
    if resume:
        existing = session.scalar(
            select(EvidenceExpansionPartition)
            .where(
                EvidenceExpansionPartition.config_json == encoded,
                EvidenceExpansionPartition.status.in_(("CHECKPOINTED", "COMPLETED", "EXHAUSTED")),
            )
            .order_by(EvidenceExpansionPartition.updated_at.desc())
            .limit(1)
        )
        if existing:
            return existing
    now = utc_now()
    row = EvidenceExpansionPartition(
        partition_id=deterministic_id("expansion-partition-v1", encoded, now.isoformat()),
        model="crypto_v2",
        status="RUNNING",
        max_independent_events=config["max_independent_events"],
        last_event_ticker=None,
        last_forecast_timestamp=None,
        last_forecast_id=None,
        scanned=0,
        admitted=0,
        rejected=0,
        independent_events=0,
        cohort_hash=None,
        rejection_counts_json="{}",
        config_json=encoded,
        started_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def _eligibility_reason(source: Session, forecast: Forecast, settlement: Settlement) -> str | None:
    if settlement.settled_at is None or settlement.result is None:
        return "SETTLEMENT_INVALID"
    if _utc(settlement.settled_at) <= _utc(forecast.forecasted_at):
        return "SETTLEMENT_NOT_AFTER_FORECAST"
    return None


def _snapshot(source: Session, forecast: Forecast) -> MarketSnapshot | None:
    return source.scalar(
        select(MarketSnapshot)
        .where(
            MarketSnapshot.ticker == forecast.ticker,
            MarketSnapshot.captured_at <= forecast.forecasted_at,
        )
        .order_by(MarketSnapshot.captured_at.desc(), MarketSnapshot.id.desc())
        .limit(1)
    )


def _merge_row(session: Session, row: Any, model: Any) -> Any:
    values = {column.key: getattr(row, column.key) for column in model.__table__.columns}
    return session.merge(model(**values))


def _source_hash(
    forecast: Forecast,
    market: Market,
    settlement: Settlement,
    snapshot: MarketSnapshot,
    lineage: CryptoFeatureLineage,
) -> str:
    payload = {
        "forecast": {
            column.key: getattr(forecast, column.key) for column in Forecast.__table__.columns
        },
        "market_ticker": market.ticker,
        "settlement": [settlement.settled_at, settlement.result],
        "snapshot": [
            snapshot.id,
            snapshot.captured_at,
            snapshot.best_yes_ask,
            snapshot.best_no_ask,
        ],
        "lineage_hash": lineage.provenance_hash,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def _cohort_hash(session: Session, partition_id: str) -> str | None:
    hashes = list(
        session.scalars(
            select(EvidenceExpansionMember.source_hash)
            .where(EvidenceExpansionMember.partition_id == partition_id)
            .order_by(EvidenceExpansionMember.source_forecast_id)
        )
    )
    return hashlib.sha256("\n".join(hashes).encode()).hexdigest() if hashes else None


def _asset(forecast: Forecast) -> str | None:
    try:
        payload = json.loads(forecast.feature_json or "{}")
    except json.JSONDecodeError:
        return None
    symbols = payload.get("component_symbols") or [payload.get("symbol")]
    return "+".join(str(item) for item in symbols if item) or None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
