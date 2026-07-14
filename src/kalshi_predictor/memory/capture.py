from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    Forecast,
    ForecastMemory,
    MarketOpportunity,
    MarketRanking,
    MarketSnapshot,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PositionSizingDecisionLog,
    Settlement,
)
from kalshi_predictor.memory.contracts import (
    DATA_MODE_AS_OBSERVED,
    INGESTION_LIVE,
    decimal_string,
    local_code_commit,
    model_quality_flags,
    optional_non_negative_decimal,
    quality_flags_for_quote,
    raw_payload_hash,
    score_0_100_to_unit,
    stable_id,
)
from kalshi_predictor.memory.repository import (
    latest_market_memory_for_instrument,
    latest_market_memory_for_source,
    write_forecast_memory,
    write_market_memory,
    write_trade_memory,
)
from kalshi_predictor.paper.models import BUY_NO, BUY_YES, ORDER_FILLED
from kalshi_predictor.utils.decimals import ONE_DOLLAR, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

logger = logging.getLogger(__name__)
FORECAST_OUTCOME_FINALIZED_SEQUENCE = 9_000_000


def capture_market_snapshot(
    session: Session,
    snapshot: MarketSnapshot,
    *,
    snapshot_type: str = "DECISION",
    ingestion_mode: str = INGESTION_LIVE,
    settings: Settings | None = None,
) -> str | None:
    try:
        raw_market = decode_json(snapshot.raw_market_json)
        bid = snapshot.best_yes_bid or snapshot.yes_bid_dollars
        ask = snapshot.best_yes_ask or snapshot.yes_ask_dollars
        observed_at = _ensure_utc(snapshot.captured_at)
        event_time = _ensure_utc(snapshot.captured_at)
        flags = quality_flags_for_quote(
            bid=bid, ask=ask, event_time=event_time, observed_at=observed_at
        )
        receipt = write_market_memory(
            session,
            {
                "market_memory_id": stable_id("market_snapshot", snapshot.id, snapshot_type),
                "event_type": snapshot_type,
                "event_sequence": int(snapshot.id or 1),
                "event_time": event_time,
                "observed_at": observed_at,
                "source_component": "market_snapshot_repository",
                "source_event_id": f"market_snapshot:{snapshot.id}",
                "idempotency_key": f"market_snapshot:{snapshot.id}:{snapshot_type}",
                "instrument_id": snapshot.ticker,
                "venue_id": "kalshi",
                "asset_class": "event_contract",
                "category_id": raw_market.get("category"),
                "contract_id": snapshot.ticker,
                "contract_expiry": parse_datetime(raw_market.get("expiration_time")),
                "timeframe": "snapshot",
                "snapshot_type": snapshot_type,
                "market_event_time": event_time,
                "source_name": "kalshi_public_api",
                "source_sequence": str(snapshot.id),
                "bid_price": decimal_string(bid),
                "ask_price": decimal_string(ask),
                "mid_price": _midpoint_string(bid, ask),
                "last_price": decimal_string(snapshot.last_price_dollars),
                "bid_size": None,
                "ask_size": None,
                "spread_absolute": decimal_string(snapshot.spread),
                "volume": optional_non_negative_decimal(snapshot.volume_fp, field="volume"),
                "open_interest": optional_non_negative_decimal(
                    snapshot.open_interest_fp,
                    field="open_interest",
                ),
                "liquidity_score": None,
                "trading_status": snapshot.status,
                "raw_payload_uri": f"market_snapshots:{snapshot.id}",
                "raw_payload_hash": raw_payload_hash(
                    snapshot.raw_market_json,
                    snapshot.raw_orderbook_json,
                ),
                "data_mode": DATA_MODE_AS_OBSERVED,
                "ingestion_mode": ingestion_mode,
                "data_quality_flags": flags,
                "event_payload": {
                    "market_snapshot_id": snapshot.id,
                    "ticker": snapshot.ticker,
                    "raw_payload_pointer": f"market_snapshots:{snapshot.id}",
                },
            },
            settings=settings,
        )
        return receipt.memory_event_id
    except Exception:
        logger.exception("Phase 3O market snapshot capture failed.")
        return None


def capture_settlement_market_memory(
    session: Session,
    settlement: Settlement,
    *,
    settings: Settings | None = None,
) -> str | None:
    try:
        event_time = _ensure_utc(settlement.settled_at or settlement.updated_at)
        idempotency_key = f"settlement:{settlement.ticker}:{settlement.updated_at.isoformat()}"
        receipt = write_market_memory(
            session,
            {
                "market_memory_id": stable_id(
                    "settlement", settlement.ticker, settlement.updated_at
                ),
                "event_type": "SETTLEMENT_FINAL",
                "event_sequence": 9_000_000 + _stable_small_int(settlement.ticker),
                "event_time": event_time,
                "observed_at": _ensure_utc(settlement.updated_at),
                "source_component": "settlement_repository",
                "source_event_id": f"settlement:{settlement.ticker}",
                "idempotency_key": idempotency_key,
                "instrument_id": settlement.ticker,
                "venue_id": "kalshi",
                "asset_class": "event_contract",
                "contract_id": settlement.ticker,
                "timeframe": "settlement",
                "snapshot_type": "SETTLEMENT_FINAL",
                "market_event_time": event_time,
                "source_name": "kalshi_public_settlements",
                "settlement_price": decimal_string(settlement.yes_settlement_value),
                "raw_payload_uri": f"settlements:{settlement.ticker}",
                "raw_payload_hash": raw_payload_hash(settlement.raw_json),
                "data_mode": DATA_MODE_AS_OBSERVED,
                "ingestion_mode": INGESTION_LIVE,
                "data_quality_flags": []
                if settlement.result in {"yes", "no"}
                else ["TRADE_SETTLEMENT_PENDING"],
                "event_payload": {
                    "ticker": settlement.ticker,
                    "result": settlement.result,
                    "yes_settlement_value": settlement.yes_settlement_value,
                },
            },
            settings=settings,
        )
        return receipt.memory_event_id
    except Exception:
        logger.exception("Phase 3O settlement market capture failed.")
        return None


def capture_forecast_attempt(
    session: Session,
    *,
    snapshot: MarketSnapshot,
    model_name: str,
    forecast: Any | None,
    error: str | None = None,
    settings: Settings | None = None,
) -> None:
    if forecast is not None:
        return
    try:
        market_memory_id = _ensure_market_memory_for_snapshot(session, snapshot, settings=settings)
        forecast_id = f"forecast_attempt:{snapshot.id}:{model_name}"
        _write_forecast_event(
            session,
            event_type="FORECAST_FAILED",
            forecast_id=forecast_id,
            event_sequence=1,
            event_time=_ensure_utc(snapshot.captured_at),
            instrument_id=snapshot.ticker,
            market_memory_id=market_memory_id,
            model_name=model_name,
            reason_codes=["FORECAST_FAILED"],
            decision_status="FAILED",
            data_quality_flags=["FORECAST_FAILED"],
            event_payload={
                "market_snapshot_id": snapshot.id,
                "model_name": model_name,
                "error": error or "forecaster_returned_none",
            },
            idempotency_key=f"{forecast_id}:FORECAST_FAILED:v1",
            settings=settings,
        )
    except Exception:
        logger.exception("Phase 3O forecast-attempt capture failed.")


def capture_forecast_created(
    session: Session,
    forecast: Forecast,
    *,
    settings: Settings | None = None,
    ingestion_mode: str = INGESTION_LIVE,
) -> None:
    try:
        snapshot = _snapshot_for_forecast(session, forecast)
        market_memory_id = (
            _ensure_market_memory_for_snapshot(session, snapshot, settings=settings)
            if snapshot is not None
            else None
        )
        features = decode_json(forecast.feature_json)
        feature_hash = raw_payload_hash(forecast.feature_json)
        model_name = forecast.model_name
        artifact_hash = _model_artifact_hash(features)
        code_commit = local_code_commit()
        flags = model_quality_flags(
            model_id=model_name,
            model_version=model_name,
            artifact_hash=artifact_hash,
            feature_schema_version="forecast_features_v1",
            code_commit_sha=code_commit,
            feature_vector_hash=feature_hash,
        )
        if market_memory_id is None:
            flags.append("FORECAST_MARKET_LINK_MISSING")
        _write_forecast_event(
            session,
            event_type="FORECAST_CREATED",
            forecast_id=_forecast_memory_id(forecast.id),
            event_sequence=1,
            event_time=_ensure_utc(forecast.forecasted_at),
            observed_at=_ensure_utc(forecast.forecasted_at),
            instrument_id=forecast.ticker,
            market_memory_id=market_memory_id,
            model_name=model_name,
            predicted_probability=decimal_string(forecast.yes_probability),
            probability_up=decimal_string(forecast.yes_probability),
            probability_down=_probability_down(forecast.yes_probability),
            confidence_score=_probability_confidence(forecast.yes_probability),
            decision_status="FORECAST_ONLY",
            forecast_generated_at=_ensure_utc(forecast.forecasted_at),
            forecast_valid_from=_ensure_utc(forecast.forecasted_at),
            forecast_target_at=_forecast_target_at(session, forecast),
            forecast_type="binary_probability",
            primary_model_id=model_name,
            primary_model_family=_model_family(model_name),
            primary_model_version=model_name,
            primary_model_artifact_hash=artifact_hash,
            feature_set_id="forecast_features",
            feature_schema_version="forecast_features_v1",
            feature_vector_hash=feature_hash,
            feature_observed_through=_ensure_utc(snapshot.captured_at) if snapshot else None,
            feature_computation_version="local_feature_builder_v1",
            code_commit_sha=code_commit,
            configuration_version=model_name,
            model_lineage=[
                {
                    "role": "forecast",
                    "component_id": model_name,
                    "version": model_name,
                    "artifact_hash": artifact_hash,
                }
            ],
            feature_lineage={
                "feature_set_id": "forecast_features",
                "feature_schema_version": "forecast_features_v1",
                "feature_vector_hash": feature_hash,
                "market_memory_ids": [market_memory_id] if market_memory_id else [],
                "missing_feature_flags": flags,
            },
            forecast_outcome_status="PENDING",
            data_quality_flags=flags,
            event_payload={
                "forecast_row_id": forecast.id,
                "feature_json": features,
                "notes": forecast.notes,
            },
            ingestion_mode=ingestion_mode,
            idempotency_key=f"{_forecast_memory_id(forecast.id)}:FORECAST_CREATED:v1",
            settings=settings,
        )
    except Exception:
        logger.exception("Phase 3O forecast capture failed.")


def capture_market_ranking(
    session: Session,
    ranking: MarketRanking,
    *,
    settings: Settings | None = None,
) -> None:
    try:
        forecast = _latest_forecast(
            session,
            ticker=ranking.ticker,
            model_name=ranking.forecast_model,
            at_or_before=ranking.ranked_at,
        )
        forecast_id = _forecast_memory_id(forecast.id) if forecast else f"ranking:{ranking.id}"
        event_type = "OPPORTUNITY_SCORED" if ranking.best_side else "OPPORTUNITY_REJECTED"
        _write_forecast_event(
            session,
            event_type=event_type,
            forecast_id=forecast_id,
            event_sequence=2_000_000 + int(ranking.id or 0),
            event_time=_ensure_utc(ranking.ranked_at),
            instrument_id=ranking.ticker,
            market_memory_id=_latest_market_memory_id(session, ranking.ticker),
            opportunity_id=f"ranking:{ranking.id}",
            model_name=ranking.forecast_model,
            opportunity_score=score_0_100_to_unit(ranking.opportunity_score),
            liquidity_score=score_0_100_to_unit(ranking.liquidity_score),
            confidence_score=score_0_100_to_unit(ranking.model_confidence_score),
            predicted_probability=decimal_string(ranking.forecast_probability),
            raw_expected_value=decimal_string(ranking.estimated_edge),
            eligibility_status="ELIGIBLE" if ranking.best_side else "NO_TRADE",
            decision_status="CANDIDATE" if ranking.best_side else "NO_TRADE",
            reason_codes=[] if ranking.best_side else ["NO_TRADE"],
            event_payload={
                "ranking_id": ranking.id,
                "best_side": ranking.best_side,
                "best_price": ranking.best_price,
                "reason": ranking.reason,
            },
            idempotency_key=f"ranking:{ranking.id}:{event_type}:v1",
            settings=settings,
        )
    except Exception:
        logger.exception("Phase 3O ranking capture failed.")


def capture_market_opportunity(
    session: Session,
    opportunity: MarketOpportunity,
    *,
    settings: Settings | None = None,
) -> None:
    try:
        forecast = _latest_forecast(
            session,
            ticker=opportunity.ticker,
            model_name=opportunity.model_name,
            at_or_before=opportunity.detected_at,
        )
        forecast_id = (
            _forecast_memory_id(forecast.id) if forecast else f"opportunity:{opportunity.id}"
        )
        _write_forecast_event(
            session,
            event_type="TRADE_SELECTED",
            forecast_id=forecast_id,
            event_sequence=2_500_000 + int(opportunity.id or 0),
            event_time=_ensure_utc(opportunity.detected_at),
            instrument_id=opportunity.ticker,
            market_memory_id=_latest_market_memory_id(session, opportunity.ticker),
            opportunity_id=f"opportunity:{opportunity.id}",
            model_name=opportunity.model_name,
            opportunity_score=score_0_100_to_unit(opportunity.opportunity_score),
            predicted_probability=decimal_string(opportunity.forecast_probability),
            raw_expected_value=decimal_string(opportunity.estimated_edge),
            eligibility_status=opportunity.status,
            decision_status="SELECTED",
            event_payload={
                "opportunity_id": opportunity.id,
                "side": opportunity.side,
                "price": opportunity.price,
                "reason": opportunity.reason,
            },
            idempotency_key=f"opportunity:{opportunity.id}:TRADE_SELECTED:v1",
            settings=settings,
        )
    except Exception:
        logger.exception("Phase 3O opportunity capture failed.")


def capture_position_sizing_decision(
    session: Session,
    row: PositionSizingDecisionLog,
    *,
    settings: Settings | None = None,
) -> None:
    try:
        forecast_id = _forecast_id_from_trade_intent(row.trade_intent_id) or _latest_forecast_id(
            session,
            row.ticker,
            row.model_name,
            row.decision_timestamp,
        )
        _write_forecast_event(
            session,
            event_type="PHASE_3M_SIZED",
            forecast_id=forecast_id or f"phase3m:{row.id}",
            event_sequence=3_000_000 + int(row.id or 0),
            event_time=_ensure_utc(row.decision_timestamp),
            instrument_id=row.ticker,
            market_memory_id=_latest_market_memory_id(session, row.ticker),
            model_name=row.model_name,
            strategy_id=row.strategy_id or "paper_edge_v1",
            phase_3m_decision_id=str(row.id),
            phase_3m_tier=row.tier,
            phase_3m_proposed_contracts=row.proposed_contracts,
            phase_3m_composite_score=decimal_string(row.composite_score),
            phase_3m_config_version=row.version,
            reason_codes=_decode_list(row.reason_codes_json),
            decision_status="SIZED",
            event_payload={"position_sizing_decision_id": row.id, "raw": decode_json(row.raw_json)},
            idempotency_key=f"phase3m:{row.id}:PHASE_3M_SIZED:v1",
            settings=settings,
        )
    except Exception:
        logger.exception("Phase 3O position sizing capture failed.")


def capture_advanced_risk_decision(
    session: Session,
    row: AdvancedRiskDecisionLog,
    *,
    settings: Settings | None = None,
) -> None:
    try:
        forecast_id = _forecast_id_from_trade_intent(row.trade_intent_id) or _latest_forecast_id(
            session,
            row.ticker,
            row.model_id,
            row.decision_timestamp,
        )
        reason_codes = _decode_list(row.reason_codes_json)
        _write_forecast_event(
            session,
            event_type="PHASE_3N_EVALUATED",
            forecast_id=forecast_id or f"phase3n:{row.id}",
            event_sequence=4_000_000 + int(row.id or 0),
            event_time=_ensure_utc(row.decision_timestamp),
            instrument_id=row.ticker,
            market_memory_id=_latest_market_memory_id(session, row.ticker),
            model_name=row.model_id,
            strategy_id=row.strategy_id or "paper_edge_v1",
            phase_3m_decision_id=str(row.position_sizing_decision_id)
            if row.position_sizing_decision_id
            else None,
            phase_3m_tier=row.phase_3m_tier,
            phase_3m_proposed_contracts=row.phase_3m_proposed_contracts,
            phase_3n_decision_id=str(row.id),
            phase_3n_action=row.action,
            phase_3n_approved_contracts=row.executed_contracts,
            phase_3n_reason_codes=reason_codes,
            phase_3n_config_version=row.version,
            reason_codes=reason_codes,
            decision_status="RISK_BLOCKED" if row.action == "BLOCK" else "RISK_APPROVED",
            event_payload={"advanced_risk_decision_id": row.id, "raw": decode_json(row.raw_json)},
            idempotency_key=f"phase3n:{row.id}:PHASE_3N_EVALUATED:v1",
            settings=settings,
        )
        if row.action == "BLOCK":
            _write_forecast_event(
                session,
                event_type="NO_TRADE_FINALIZED",
                forecast_id=forecast_id or f"phase3n:{row.id}",
                event_sequence=5_000_000 + int(row.id or 0),
                event_time=_ensure_utc(row.decision_timestamp),
                instrument_id=row.ticker,
                market_memory_id=_latest_market_memory_id(session, row.ticker),
                model_name=row.model_id,
                strategy_id=row.strategy_id or "paper_edge_v1",
                decision_status="RISK_BLOCKED",
                reason_codes=reason_codes,
                event_payload={"advanced_risk_decision_id": row.id, "action": row.action},
                idempotency_key=f"phase3n:{row.id}:NO_TRADE_FINALIZED:v1",
                settings=settings,
            )
    except Exception:
        logger.exception("Phase 3O advanced risk capture failed.")


def capture_paper_order_created(
    session: Session,
    order: PaperOrder,
    *,
    settings: Settings | None = None,
) -> None:
    try:
        raw = decode_json(order.raw_decision_json)
        _write_trade_event(
            session,
            event_type="TRADE_INTENT_CREATED",
            trade_id=_paper_trade_id(order.id),
            event_sequence=1,
            event_time=_ensure_utc(order.created_at),
            order=order,
            requested_quantity=order.quantity,
            accepted_quantity=order.quantity,
            open_quantity=order.quantity,
            intended_entry_price=order.limit_price,
            submitted_price=order.limit_price,
            phase_3m_proposed_contracts=_raw_int(
                raw, "position_sizing_decision", "proposed_contracts"
            ),
            phase_3n_approved_contracts=_raw_int(
                raw, "advanced_risk_decision", "executed_contracts"
            ),
            market_memory_id=_latest_market_memory_id(session, order.ticker),
            event_payload={"paper_order_id": order.id, "raw_decision": raw},
            idempotency_key=f"paper_order:{order.id}:TRADE_INTENT_CREATED:v1",
            settings=settings,
        )
    except Exception:
        logger.exception("Phase 3O paper order capture failed.")


def capture_paper_fill(
    session: Session,
    fill: PaperFill,
    *,
    settings: Settings | None = None,
) -> None:
    try:
        order = session.get(PaperOrder, fill.paper_order_id)
        if order is None:
            return
        _write_trade_event(
            session,
            event_type="ENTRY_FILLED",
            trade_id=_paper_trade_id(order.id),
            event_sequence=2_000_000 + int(fill.id or 0),
            event_time=_ensure_utc(fill.filled_at),
            order=order,
            fill=fill,
            filled_quantity=fill.quantity,
            fill_price=fill.price,
            commission=fill.fee,
            exchange_fees=fill.fee,
            total_cost=fill.fee,
            paper_fill_model_id="immediate_fill",
            paper_fill_model_version="v1",
            paper_fill_policy={"simulation": "immediate_fill_v1"},
            market_memory_id=_latest_market_memory_id(session, order.ticker),
            event_payload={"paper_fill_id": fill.id, "raw_fill": decode_json(fill.raw_fill_json)},
            idempotency_key=f"paper_fill:{fill.id}:ENTRY_FILLED:v1",
            settings=settings,
        )
    except Exception:
        logger.exception("Phase 3O paper fill capture failed.")


def capture_position_opened(
    session: Session,
    fill: PaperFill,
    position: PaperPosition,
    *,
    settings: Settings | None = None,
) -> None:
    try:
        order = session.get(PaperOrder, fill.paper_order_id)
        if order is None:
            return
        _write_trade_event(
            session,
            event_type="POSITION_OPENED",
            trade_id=_paper_trade_id(order.id),
            event_sequence=3_000_000 + int(fill.id or 0),
            event_time=_ensure_utc(position.updated_at),
            order=order,
            fill=fill,
            position_id=position.ticker,
            filled_quantity=fill.quantity,
            open_quantity=position.yes_contracts
            if order.side == BUY_YES
            else position.no_contracts,
            average_entry_price=position.avg_yes_price
            if order.side == BUY_YES
            else position.avg_no_price,
            market_memory_id=_latest_market_memory_id(session, order.ticker),
            event_payload={
                "position": {
                    "ticker": position.ticker,
                    "yes_contracts": position.yes_contracts,
                    "no_contracts": position.no_contracts,
                    "avg_yes_price": position.avg_yes_price,
                    "avg_no_price": position.avg_no_price,
                }
            },
            idempotency_key=f"paper_fill:{fill.id}:POSITION_OPENED:v1",
            settings=settings,
        )
    except Exception:
        logger.exception("Phase 3O position capture failed.")


def capture_settlement_outcomes(
    session: Session,
    settlement: Settlement,
    *,
    settings: Settings | None = None,
) -> None:
    try:
        market_memory_id = capture_settlement_market_memory(session, settlement, settings=settings)
        _capture_forecast_outcomes(
            session, settlement, market_memory_id=market_memory_id, settings=settings
        )
        _capture_trade_outcomes(
            session, settlement, market_memory_id=market_memory_id, settings=settings
        )
    except Exception:
        logger.exception("Phase 3O settlement outcome capture failed.")


def _capture_forecast_outcomes(
    session: Session,
    settlement: Settlement,
    *,
    market_memory_id: str | None,
    settings: Settings | None,
) -> None:
    resolved = settings or get_settings()
    forecasts = list(session.scalars(select(Forecast).where(Forecast.ticker == settlement.ticker)))
    for forecast in forecasts:
        forecast_memory_id = _forecast_memory_id(forecast.id)
        existing_outcome = _existing_forecast_memory_event(
            session,
            forecast_id=forecast_memory_id,
            event_sequence=FORECAST_OUTCOME_FINALIZED_SEQUENCE,
        )
        if settlement.result not in {"yes", "no"}:
            status = "WAITING_FOR_DATA"
            reason_codes = ["FORECAST_OUTCOME_WAITING_FOR_DATA"]
        else:
            status = "FINAL"
            reason_codes = []
        probability = to_decimal(forecast.yes_probability)
        actual = _actual_yes_value(settlement)
        brier = None
        absolute_error = None
        direction_correct = None
        if probability is not None and actual is not None:
            error = probability - actual
            brier = error * error
            absolute_error = abs(error)
            direction_correct = int((probability >= Decimal("0.5")) == (actual == Decimal("1")))
        if existing_outcome is not None:
            if _same_forecast_outcome(existing_outcome, settlement, actual):
                continue
            logger.warning(
                "forecast_outcome_memory_already_finalized",
                extra={
                    "memory": {
                        "forecast_id": forecast_memory_id,
                        "event_sequence": FORECAST_OUTCOME_FINALIZED_SEQUENCE,
                        "settlement_ticker": settlement.ticker,
                    }
                },
            )
            continue
        _write_forecast_event(
            session,
            event_type="FORECAST_OUTCOME_FINALIZED",
            forecast_id=forecast_memory_id,
            event_sequence=FORECAST_OUTCOME_FINALIZED_SEQUENCE,
            event_time=_ensure_utc(settlement.settled_at or settlement.updated_at),
            instrument_id=forecast.ticker,
            market_memory_id=_latest_market_memory_id(session, forecast.ticker),
            outcome_market_memory_id=market_memory_id,
            model_name=forecast.model_name,
            predicted_probability=decimal_string(forecast.yes_probability),
            forecast_outcome_status=status,
            label_policy_id=resolved.phase_3o_forecast_label_policy_id,
            label_policy_version=resolved.phase_3o_forecast_label_policy_version,
            label_available_at=_ensure_utc(settlement.updated_at),
            outcome_finalized_at=_ensure_utc(settlement.updated_at) if status == "FINAL" else None,
            actual_value=decimal_string(actual),
            direction_correct=direction_correct,
            absolute_error=decimal_string(absolute_error),
            squared_error=decimal_string(brier),
            brier_component=decimal_string(brier),
            outcome_class=settlement.result,
            reason_codes=reason_codes,
            data_quality_flags=reason_codes,
            event_payload={
                "forecast_row_id": forecast.id,
                "settlement_ticker": settlement.ticker,
                "settlement_result": settlement.result,
            },
            idempotency_key=(
                f"{forecast_memory_id}:FORECAST_OUTCOME_FINALIZED:"
                f"{settlement.ticker}:{settlement.result}"
            ),
            settings=settings,
        )


def _capture_trade_outcomes(
    session: Session,
    settlement: Settlement,
    *,
    market_memory_id: str | None,
    settings: Settings | None,
) -> None:
    orders = list(
        session.scalars(
            select(PaperOrder)
            .where(PaperOrder.ticker == settlement.ticker, PaperOrder.status == ORDER_FILLED)
            .order_by(PaperOrder.created_at, PaperOrder.id)
        )
    )
    for order in orders:
        fills = list(
            session.scalars(
                select(PaperFill)
                .where(PaperFill.paper_order_id == order.id)
                .order_by(PaperFill.filled_at, PaperFill.id)
            )
        )
        fees = sum((to_decimal(fill.fee) or Decimal("0")) for fill in fills)
        quantity = sum(fill.quantity for fill in fills) or order.quantity
        price = to_decimal(order.limit_price) or to_decimal(order.market_price) or Decimal("0")
        won = _order_won(order, settlement)
        gross_pnl = (ONE_DOLLAR - price) * quantity if won else -price * quantity
        net_pnl = gross_pnl - fees
        settled_at = _ensure_utc(settlement.settled_at or settlement.updated_at)
        _write_trade_event(
            session,
            event_type="SETTLEMENT_FINAL",
            trade_id=_paper_trade_id(order.id),
            event_sequence=4_000_000 + _stable_small_int(settlement.updated_at.isoformat()),
            event_time=settled_at,
            order=order,
            settlement_id=settlement.ticker,
            settlement_status="FINAL" if settlement.result in {"yes", "no"} else "UNAVAILABLE",
            settlement_source="kalshi_public_settlements",
            settlement_reference=settlement.ticker,
            settlement_version="v1",
            settled_at=settled_at,
            settlement_price=settlement.yes_settlement_value,
            market_memory_id=market_memory_id,
            event_payload={"settlement": decode_json(settlement.raw_json)},
            idempotency_key=f"paper_order:{order.id}:SETTLEMENT_FINAL:{settlement.updated_at.isoformat()}",
            settings=settings,
        )
        _write_trade_event(
            session,
            event_type="TRADE_OUTCOME_FINALIZED",
            trade_id=_paper_trade_id(order.id),
            event_sequence=5_000_000 + _stable_small_int(settlement.updated_at.isoformat()),
            event_time=settled_at,
            order=order,
            settlement_id=settlement.ticker,
            closed_quantity=quantity,
            settlement_status="FINAL" if settlement.result in {"yes", "no"} else "UNAVAILABLE",
            settled_at=settled_at,
            gross_pnl=decimal_string(gross_pnl),
            net_pnl=decimal_string(net_pnl),
            pnl_currency="USD",
            return_fraction=decimal_string(
                net_pnl / (price * quantity) if price and quantity else None
            ),
            outcome_class="WIN" if won else "LOSS",
            outcome_finalized_at=settled_at if settlement.result in {"yes", "no"} else None,
            outcome_reason_codes=[]
            if settlement.result in {"yes", "no"}
            else ["TRADE_SETTLEMENT_PENDING"],
            market_memory_id=market_memory_id,
            event_payload={
                "settlement_result": settlement.result,
                "quantity": quantity,
                "fees": decimal_string(fees),
            },
            idempotency_key=f"paper_order:{order.id}:TRADE_OUTCOME_FINALIZED:{settlement.updated_at.isoformat()}",
            settings=settings,
        )


def _write_forecast_event(
    session: Session,
    *,
    event_type: str,
    forecast_id: str,
    event_sequence: int,
    event_time: Any,
    instrument_id: str,
    idempotency_key: str,
    settings: Settings | None,
    observed_at: Any | None = None,
    market_memory_id: str | None = None,
    outcome_market_memory_id: str | None = None,
    opportunity_id: str | None = None,
    model_name: str | None = None,
    strategy_id: str | None = None,
    reason_codes: list[str] | None = None,
    phase_3n_reason_codes: list[str] | None = None,
    data_quality_flags: list[str] | None = None,
    event_payload: dict[str, Any] | None = None,
    ingestion_mode: str = INGESTION_LIVE,
    **kwargs: Any,
) -> None:
    resolved = settings or get_settings()
    model_id = kwargs.pop("primary_model_id", None) or model_name
    model_version = kwargs.pop("primary_model_version", None) or model_name
    write_forecast_memory(
        session,
        {
            "forecast_id": forecast_id,
            "event_type": event_type,
            "event_sequence": event_sequence,
            "schema_version": resolved.phase_3o_schema_version,
            "event_time": _ensure_utc(event_time),
            "observed_at": _ensure_utc(observed_at) if observed_at is not None else None,
            "source_component": "phase_3o_capture",
            "source_event_id": idempotency_key,
            "idempotency_key": idempotency_key,
            "market_memory_id": market_memory_id,
            "outcome_market_memory_id": outcome_market_memory_id,
            "opportunity_id": opportunity_id,
            "instrument_id": instrument_id,
            "strategy_id": strategy_id or "forecast_registry",
            "timeframe": "market_lifecycle",
            "primary_model_id": model_id,
            "primary_model_family": kwargs.pop("primary_model_family", _model_family(model_id)),
            "primary_model_version": model_version,
            "reason_codes": reason_codes or [],
            "phase_3n_reason_codes": phase_3n_reason_codes or [],
            "data_quality_flags": data_quality_flags or [],
            "event_payload": event_payload or {},
            "ingestion_mode": ingestion_mode,
            **kwargs,
        },
        settings=resolved,
    )


def _existing_forecast_memory_event(
    session: Session,
    *,
    forecast_id: str,
    event_sequence: int,
) -> ForecastMemory | None:
    return session.scalar(
        select(ForecastMemory)
        .where(
            ForecastMemory.forecast_id == forecast_id,
            ForecastMemory.event_sequence == event_sequence,
        )
        .limit(1)
    )


def _same_forecast_outcome(
    existing: ForecastMemory,
    settlement: Settlement,
    actual: Decimal | None,
) -> bool:
    return (
        existing.event_type == "FORECAST_OUTCOME_FINALIZED"
        and existing.outcome_class == settlement.result
        and existing.actual_value == decimal_string(actual)
    )


def _write_trade_event(
    session: Session,
    *,
    event_type: str,
    trade_id: str,
    event_sequence: int,
    event_time: Any,
    order: PaperOrder,
    idempotency_key: str,
    settings: Settings | None,
    fill: PaperFill | None = None,
    **kwargs: Any,
) -> None:
    raw = decode_json(order.raw_decision_json)
    write_trade_memory(
        session,
        {
            "trade_id": trade_id,
            "event_type": event_type,
            "event_sequence": event_sequence,
            "schema_version": (settings or get_settings()).phase_3o_schema_version,
            "event_time": _ensure_utc(event_time),
            "observed_at": _ensure_utc(event_time),
            "source_component": "paper_ledger",
            "source_event_id": idempotency_key,
            "idempotency_key": idempotency_key,
            "forecast_id": _forecast_memory_id(order.forecast_id) if order.forecast_id else None,
            "trade_intent_id": f"forecast:{order.forecast_id}"
            if order.forecast_id
            else f"paper_order:{order.id}",
            "order_correlation_id": f"paper_order:{order.id}",
            "order_id": str(order.id),
            "fill_id": str(fill.id) if fill is not None else None,
            "execution_mode": "PAPER",
            "instrument_id": order.ticker,
            "strategy_id": str(raw.get("strategy") or "paper_edge_v1"),
            "model_id": order.model_name,
            "model_version": order.model_name,
            "model_lineage": [
                {"role": "forecast", "component_id": order.model_name, "version": order.model_name}
            ],
            "direction": "LONG",
            "order_type": "LIMIT",
            "time_in_force": "IMMEDIATE_SIMULATION",
            "requested_quantity": order.quantity,
            "accepted_quantity": order.quantity,
            "intended_entry_price": order.limit_price,
            "submitted_price": order.limit_price,
            "confidence_score": _probability_confidence(order.probability),
            "risk_adjusted_expected_value": order.edge,
            "unmodeled_reason_code": None if order.forecast_id else "TRADE_FORECAST_LINK_MISSING",
            "data_quality_flags": [] if order.forecast_id else ["TRADE_FORECAST_LINK_MISSING"],
            "event_payload": {},
            **kwargs,
        },
        settings=settings,
    )


def _ensure_market_memory_for_snapshot(
    session: Session,
    snapshot: MarketSnapshot,
    *,
    settings: Settings | None,
) -> str | None:
    source_event_id = f"market_snapshot:{snapshot.id}"
    existing = latest_market_memory_for_source(session, source_event_id)
    if existing is not None:
        return existing.market_memory_id
    return capture_market_snapshot(session, snapshot, settings=settings)


def _snapshot_for_forecast(session: Session, forecast: Forecast) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(
            MarketSnapshot.ticker == forecast.ticker,
            MarketSnapshot.captured_at <= forecast.forecasted_at,
        )
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _latest_forecast(
    session: Session,
    *,
    ticker: str,
    model_name: str | None,
    at_or_before: Any,
) -> Forecast | None:
    statement = select(Forecast).where(Forecast.ticker == ticker)
    if model_name:
        statement = statement.where(Forecast.model_name == model_name)
    timestamp = parse_datetime(at_or_before)
    if timestamp is not None:
        statement = statement.where(Forecast.forecasted_at <= timestamp)
    return session.scalar(
        statement.order_by(desc(Forecast.forecasted_at), desc(Forecast.id)).limit(1)
    )


def _latest_forecast_id(
    session: Session,
    ticker: str,
    model_name: str | None,
    at_or_before: Any,
) -> str | None:
    forecast = _latest_forecast(
        session,
        ticker=ticker,
        model_name=model_name,
        at_or_before=at_or_before,
    )
    return _forecast_memory_id(forecast.id) if forecast else None


def _latest_market_memory_id(session: Session, ticker: str) -> str | None:
    memory = latest_market_memory_for_instrument(session, ticker)
    return memory.market_memory_id if memory else None


def _forecast_memory_id(forecast_id: int | None) -> str:
    return f"forecast:{forecast_id}" if forecast_id is not None else "forecast:unknown"


def _paper_trade_id(order_id: int | None) -> str:
    return f"paper_order:{order_id}" if order_id is not None else "paper_order:unknown"


def _forecast_id_from_trade_intent(value: str | None) -> str | None:
    if not value or not value.startswith("forecast:"):
        return None
    return value


def _forecast_target_at(session: Session, forecast: Forecast) -> Any | None:
    from kalshi_predictor.data.schema import Market

    market = session.get(Market, forecast.ticker)
    if market is not None:
        return market.close_time or market.expected_expiration_time or market.expiration_time
    return forecast.forecasted_at + timedelta(days=1)


def _model_artifact_hash(features: dict[str, Any]) -> str | None:
    explicit = features.get("model_artifact_hash") or features.get("artifact_hash")
    return str(explicit) if explicit else None


def _model_family(model_name: str | None) -> str | None:
    if not model_name:
        return None
    return str(model_name).split("_")[0]


def _probability_down(value: Any) -> str | None:
    probability = to_decimal(value)
    if probability is None:
        return None
    return decimal_string(Decimal("1") - probability)


def _probability_confidence(value: Any) -> str | None:
    probability = to_decimal(value)
    if probability is None:
        return None
    return decimal_string(max(probability, Decimal("1") - probability))


def _midpoint_string(bid: Any, ask: Any) -> str | None:
    bid_decimal = to_decimal(bid)
    ask_decimal = to_decimal(ask)
    if bid_decimal is None or ask_decimal is None:
        return None
    return decimal_string((bid_decimal + ask_decimal) / Decimal("2"))


def _actual_yes_value(settlement: Settlement) -> Decimal | None:
    value = to_decimal(settlement.yes_settlement_value)
    if value is not None:
        return value
    if settlement.result == "yes":
        return Decimal("1")
    if settlement.result == "no":
        return Decimal("0")
    return None


def _order_won(order: PaperOrder, settlement: Settlement) -> bool:
    if order.side == BUY_YES:
        return settlement.result == "yes"
    if order.side == BUY_NO:
        return settlement.result == "no"
    return False


def _raw_int(raw: dict[str, Any], section: str, key: str) -> int | None:
    section_value = raw.get(section)
    if not isinstance(section_value, dict):
        return None
    value = section_value.get(key)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _decode_list(value: str | None) -> list[str]:
    decoded = decode_json(value)
    if decoded:
        return [str(item) for item in decoded.values()]
    try:
        import json

        raw = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def _ensure_utc(value: Any) -> Any:
    parsed = parse_datetime(value)
    return parsed if parsed is not None else utc_now()


def _stable_small_int(value: str) -> int:
    return int(stable_id("sequence", value).replace("-", "")[:8], 16)
