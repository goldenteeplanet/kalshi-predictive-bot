from __future__ import annotations

from decimal import Decimal

from kalshi_predictor.data.schema import TradeMemory
from kalshi_predictor.reinforcement_learning.contracts import (
    ACTION_PROCEED,
    ACTION_SKIP,
    EVIDENCE_DOWNSTREAM_BLOCKED,
    EVIDENCE_LIVE,
    EVIDENCE_NO_ACTION,
    EVIDENCE_PAPER,
    RewardDefinition,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal


def reward_for_trade(
    trade: TradeMemory | None,
    *,
    action: str,
    reward_definition: RewardDefinition,
    phase_3n_action: str | None = None,
) -> dict[str, object]:
    if action == ACTION_SKIP:
        return {
            "reward": Decimal("0"),
            "raw_reward": Decimal("0"),
            "gross_pnl": None,
            "net_pnl": None,
            "total_cost": None,
            "roi_denominator": None,
            "evidence_type": EVIDENCE_NO_ACTION,
            "reward_status": "FINAL",
            "reason_codes": ["RECOMMEND_SKIP"],
        }
    if phase_3n_action and phase_3n_action.upper() == "BLOCK":
        return {
            "reward": Decimal("0"),
            "raw_reward": Decimal("0"),
            "gross_pnl": None,
            "net_pnl": None,
            "total_cost": None,
            "roi_denominator": None,
            "evidence_type": EVIDENCE_DOWNSTREAM_BLOCKED,
            "reward_status": "FINAL",
            "reason_codes": ["DOWNSTREAM_PHASE_3N_BLOCKED"],
        }
    if action != ACTION_PROCEED or trade is None:
        return _invalid_reward("REWARD_INVALID")
    if trade.outcome_finalized_at is None and trade.settled_at is None:
        return _invalid_reward("REWARD_PROVISIONAL")
    net_pnl = to_decimal(trade.net_pnl)
    if net_pnl is None:
        return _invalid_reward("REWARD_INVALID")
    denominator = _roi_denominator(trade)
    if denominator is None or denominator <= 0:
        return _invalid_reward("REWARD_INVALID")
    raw_reward = net_pnl / denominator
    clipped = min(max(raw_reward, reward_definition.clip_min), reward_definition.clip_max)
    evidence_type = EVIDENCE_LIVE if trade.execution_mode == "LIVE" else EVIDENCE_PAPER
    reason_codes = ["RECOMMEND_PROCEED"]
    if trade.filled_quantity == 0:
        reason_codes.append("NO_FILL")
    elif (
        trade.filled_quantity
        and trade.requested_quantity
        and trade.filled_quantity < trade.requested_quantity
    ):
        reason_codes.append("PARTIAL_FILL")
    return {
        "reward": clipped,
        "raw_reward": raw_reward,
        "gross_pnl": to_decimal(trade.gross_pnl),
        "net_pnl": net_pnl,
        "total_cost": to_decimal(trade.total_cost),
        "roi_denominator": denominator,
        "evidence_type": evidence_type,
        "reward_status": "FINAL",
        "reason_codes": reason_codes,
    }


def reward_payload(values: dict[str, object]) -> dict[str, object]:
    output = dict(values)
    for key in ("reward", "raw_reward", "gross_pnl", "net_pnl", "total_cost", "roi_denominator"):
        output[key] = decimal_to_str(output.get(key))
    return output


def _roi_denominator(trade: TradeMemory) -> Decimal | None:
    for value in (
        trade.committed_risk,
        trade.risk_per_contract,
        trade.gross_notional,
        trade.total_cost,
    ):
        decimal = to_decimal(value)
        if decimal is not None and decimal > 0:
            return decimal
    fill_price = to_decimal(trade.fill_price or trade.average_entry_price)
    quantity = Decimal(str(trade.filled_quantity or trade.accepted_quantity or 0))
    if fill_price is not None and quantity > 0:
        return fill_price * quantity
    return None


def _invalid_reward(reason: str) -> dict[str, object]:
    return {
        "reward": Decimal("0"),
        "raw_reward": Decimal("0"),
        "gross_pnl": None,
        "net_pnl": None,
        "total_cost": None,
        "roi_denominator": None,
        "evidence_type": "UNKNOWN",
        "reward_status": "UNAVAILABLE",
        "reason_codes": [reason],
    }
