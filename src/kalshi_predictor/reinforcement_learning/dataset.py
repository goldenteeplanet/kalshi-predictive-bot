from __future__ import annotations

from collections import Counter
from datetime import datetime

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import ForecastMemory, TradeMemory
from kalshi_predictor.reinforcement_learning.contracts import (
    ACTION_PROCEED,
    ACTION_SKIP,
    ACTION_SPACE,
    BASELINE_POLICY_ID,
    BASELINE_POLICY_VERSION,
    RewardDefinition,
    RLConfig,
    RLDataset,
    RLDatasetRow,
    checksum_payload,
    stable_phase_3s_id,
)
from kalshi_predictor.reinforcement_learning.reward import reward_for_trade
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime


def build_rl_dataset(
    session: Session,
    *,
    training_as_of: datetime,
    config: RLConfig,
    reward_definition: RewardDefinition,
) -> RLDataset:
    forecasts = list(
        session.scalars(
            select(ForecastMemory)
            .where(ForecastMemory.event_time <= training_as_of)
            .order_by(ForecastMemory.event_time, ForecastMemory.forecast_memory_event_id)
            .limit(config.max_decisions_per_run)
        )
    )
    trade_by_forecast = _latest_trade_by_forecast(session, training_as_of=training_as_of)
    rows: list[RLDatasetRow] = []
    exclusions: Counter[str] = Counter()
    for forecast in forecasts:
        exclusion = _exclusion_reason(forecast, training_as_of=training_as_of)
        if exclusion:
            exclusions[exclusion] += 1
            continue
        chosen_action = _chosen_action(forecast)
        trade = trade_by_forecast.get(forecast.forecast_id)
        reward = reward_for_trade(
            trade,
            action=chosen_action,
            reward_definition=reward_definition,
            phase_3n_action=forecast.phase_3n_action,
        )
        if reward["reward_status"] != "FINAL":
            exclusions[str(reward["reason_codes"][0])] += 1
            continue
        row = RLDatasetRow(
            decision_id=stable_phase_3s_id(
                "decision",
                forecast.forecast_memory_event_id,
                forecast.event_sequence,
            ),
            decision_at=forecast.event_time,
            chosen_action=chosen_action,
            action_set=ACTION_SPACE,
            action_mask={ACTION_SKIP: True, ACTION_PROCEED: True},
            propensities=_propensities(chosen_action),
            propensity_quality="DETERMINISTIC_POLICY_KNOWN",
            behavior_policy_id=BASELINE_POLICY_ID,
            behavior_policy_version=BASELINE_POLICY_VERSION,
            opportunity_id=forecast.opportunity_id,
            forecast_id=forecast.forecast_id,
            trade_id=trade.trade_id if trade is not None else None,
            instrument_id=forecast.instrument_id,
            category_id=forecast.category_id,
            model_id=forecast.primary_model_id,
            opportunity_score=to_decimal(forecast.opportunity_score),
            confidence_score=to_decimal(forecast.confidence_score),
            reward=reward["reward"],
            raw_reward=reward["raw_reward"],
            gross_pnl=reward["gross_pnl"],
            net_pnl=reward["net_pnl"],
            total_cost=reward["total_cost"],
            roi_denominator=reward["roi_denominator"],
            evidence_type=str(reward["evidence_type"]),
            reward_status=str(reward["reward_status"]),
            reason_codes=tuple(str(code) for code in reward["reason_codes"]),
            feature_values={
                "opportunity_score": decimal_to_str(to_decimal(forecast.opportunity_score)),
                "confidence_score": decimal_to_str(to_decimal(forecast.confidence_score)),
                "predicted_probability": forecast.predicted_probability,
                "liquidity_score": forecast.liquidity_score,
                "phase_3n_action": forecast.phase_3n_action,
            },
        )
        rows.append(row)
    payload = [row.as_payload() for row in rows]
    dataset_hash = checksum_payload(payload)
    manifest_id = stable_phase_3s_id("dataset", training_as_of.isoformat(), dataset_hash)
    return RLDataset(
        dataset_manifest_id=manifest_id,
        dataset_hash=dataset_hash,
        training_as_of=training_as_of,
        rows=tuple(rows),
        rows_total=len(forecasts),
        exclusion_counts=dict(exclusions),
        source_watermarks={
            "forecast_memory": _max_recorded_at(session, ForecastMemory.recorded_at),
            "trade_memory": _max_recorded_at(session, TradeMemory.recorded_at),
        },
    )


def _latest_trade_by_forecast(
    session: Session,
    *,
    training_as_of: datetime,
) -> dict[str, TradeMemory]:
    rows = list(
        session.scalars(
            select(TradeMemory)
            .where(TradeMemory.event_time <= training_as_of)
            .order_by(TradeMemory.forecast_id, desc(TradeMemory.event_sequence))
        )
    )
    output: dict[str, TradeMemory] = {}
    for row in rows:
        if row.forecast_id and row.forecast_id not in output:
            output[row.forecast_id] = row
    return output


def _exclusion_reason(forecast: ForecastMemory, *, training_as_of: datetime) -> str | None:
    decision_time = parse_datetime(forecast.event_time)
    feature_time = parse_datetime(forecast.feature_observed_through)
    label_time = parse_datetime(forecast.label_available_at)
    cutoff = parse_datetime(training_as_of)
    if feature_time and decision_time and feature_time > decision_time:
        return "feature_observed_after_decision"
    if label_time and cutoff and label_time > cutoff:
        return "label_after_training_cutoff"
    if forecast.instrument_id.startswith("synthetic:"):
        return "synthetic_market_no_realized_roi"
    return None


def _chosen_action(forecast: ForecastMemory) -> str:
    if (forecast.decision_status or "").upper() in {"NO_TRADE", "REJECTED", "SKIP"}:
        return ACTION_SKIP
    if (forecast.phase_3n_action or "").upper() == "BLOCK":
        return ACTION_PROCEED
    if (
        forecast.market_memory_id
        or forecast.opportunity_id
        or forecast.event_type == "TRADE_SELECTED"
    ):
        return ACTION_PROCEED
    return ACTION_SKIP


def _propensities(chosen_action: str) -> dict[str, str]:
    return {
        ACTION_SKIP: "1" if chosen_action == ACTION_SKIP else "0",
        ACTION_PROCEED: "1" if chosen_action == ACTION_PROCEED else "0",
    }


def _max_recorded_at(session: Session, column) -> str | None:
    value = session.scalar(select(func.max(column)))
    return value.isoformat() if value else None
