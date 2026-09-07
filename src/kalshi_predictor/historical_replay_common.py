from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from kalshi_predictor.utils.decimals import decimal_to_str

BUY_YES = "BUY_YES"
BUY_NO = "BUY_NO"
LOCAL_DERIVED_TICKER_PREFIXES = (
    "KXMVECROSSCATEGORY-",
    "KXMVESPORTSMULTIGAMEEXTENDED-",
)
SOURCE_SETTLED_STATUSES = {"settled", "resolved", "finalized"}
SOURCE_CLOSED_STATUSES = {"closed"}


def settlement_to_y_true(result: str | None) -> int | None:
    if result is None:
        return None
    normalized = result.strip().lower()
    if normalized in {"yes", "y", "1", "true"}:
        return 1
    if normalized in {"no", "n", "0", "false"}:
        return 0
    return None


def trade_from_decision(
    decision: Any,
    *,
    y_true: int,
    settlement_result: str | None,
    fee_per_contract: Decimal,
) -> dict[str, Any]:
    fee = fee_per_contract * Decimal(decision.quantity)
    exposure = decision.price * decision.quantity + fee
    if decision.side == BUY_YES:
        payout = Decimal(decision.quantity) if y_true == 1 else Decimal("0")
    elif decision.side == BUY_NO:
        payout = Decimal(decision.quantity) if y_true == 0 else Decimal("0")
    else:
        payout = Decimal("0")
    pnl = payout - exposure
    return {
        "ticker": decision.ticker,
        "forecast_id": decision.forecast_id,
        "simulated_at": decision.simulated_at.isoformat(),
        "side": decision.side,
        "price": decimal_to_str(decision.price) or "0",
        "quantity": decision.quantity,
        "edge": decimal_to_str(decision.edge) or "0",
        "settlement_result": settlement_result,
        "pnl": decimal_to_str(pnl) or "0",
        "exposure": decimal_to_str(exposure) or "0",
        "yes_probability": float(decision.yes_probability),
        "y_true": y_true,
    }


def has_usable_outcome(payload: Mapping[str, Any]) -> bool:
    result = payload.get("result")
    if result is not None and str(result).strip():
        return True
    return (
        payload.get("settlement_value_dollars") is not None
        or payload.get("settlement_value") is not None
        or payload.get("yes_settlement_value") is not None
    )


def source_is_closed_without_outcome(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("status") or "").strip().lower()
    return status in SOURCE_CLOSED_STATUSES and not has_usable_outcome(payload)


def source_is_settled(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("status") or "").strip().lower()
    return status in SOURCE_SETTLED_STATUSES or bool(
        payload.get("settlement_ts") or payload.get("settled_time") or payload.get("settled_at")
    )


def is_local_derived_composite_ticker(ticker: str) -> bool:
    return ticker.startswith(LOCAL_DERIVED_TICKER_PREFIXES)


def normalize_result(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"yes", "y", "1", "true"}:
        return "yes"
    if normalized in {"no", "n", "0", "false"}:
        return "no"
    return normalized or None


def markdown_cell_empty(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")


def markdown_cell_none(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")
