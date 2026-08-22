from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import AdvancedRiskDecisionLog, Forecast, MarketSnapshot
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal


def latest_crypto_v2_forecast(session: Session, ticker: str) -> Forecast | None:
    return session.scalar(
        select(Forecast)
        .where(Forecast.ticker == ticker, Forecast.model_name == "crypto_v2")
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(1)
    )


def latest_market_snapshot(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def latest_risk_decisions_by_ticker(
    session: Session,
    tickers: list[str],
) -> dict[str, dict[str, Any]]:
    if not tickers:
        return {}
    seen: dict[str, dict[str, Any]] = {}
    rows = session.scalars(
        select(AdvancedRiskDecisionLog)
        .where(AdvancedRiskDecisionLog.ticker.in_(tickers))
        .order_by(
            desc(AdvancedRiskDecisionLog.decision_timestamp),
            desc(AdvancedRiskDecisionLog.id),
        )
    )
    for row in rows:
        if row.ticker in seen:
            continue
        seen[row.ticker] = {
            "id": row.id,
            "decision_timestamp": row.decision_timestamp.isoformat(),
            "mode": row.mode,
            "action": row.action,
            "phase_3m_tier": row.phase_3m_tier,
            "phase_3m_proposed_contracts": row.phase_3m_proposed_contracts,
            "live_candidate_contracts": row.live_candidate_contracts,
            "executed_contracts": row.executed_contracts,
            "reason_codes": decode_list(row.reason_codes_json),
            "hard_blocks": decode_list(row.hard_blocks_json),
            "limiting_factors": decode_list(row.limiting_factors_json),
        }
    return seen


def crypto_candidate_sort_key(row: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    return (
        to_decimal(row.get("expected_value")) or Decimal("-999"),
        to_decimal(row.get("opportunity_score")) or Decimal("0"),
        to_decimal(row.get("estimated_edge")) or Decimal("0"),
    )


def decode_list(value: str | None) -> list[Any]:
    decoded = decode_json(value)
    if isinstance(decoded, list):
        return decoded
    if decoded in (None, ""):
        return []
    return [decoded]


def format_cents(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return decimal_to_str((value * Decimal("100")).quantize(Decimal("0.1")))


def markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def int_from_float_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_required(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
