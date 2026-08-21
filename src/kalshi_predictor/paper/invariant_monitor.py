from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    PaperFill,
    PaperOrder,
    PaperPnl,
    PositionSizingDecisionLog,
    Settlement,
)
from kalshi_predictor.paper.activation import ACTIVATION_VERSION
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal

MONITOR_VERSION = "paper_activation_invariant_monitor_v1"


def activated_trade_invariant_rows(session: Session) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for order in session.scalars(select(PaperOrder).order_by(PaperOrder.id)):
        raw = decode_json(order.raw_decision_json)
        if raw.get("source") != ACTIVATION_VERSION:
            continue
        rows.append(activated_trade_invariant_row(session, order, raw=raw))
    return rows


def activated_trade_invariant_status(session: Session) -> dict[str, Any]:
    rows = activated_trade_invariant_rows(session)
    alerts = [alert for row in rows for alert in row["alerts"]]
    return {
        "version": MONITOR_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "ALERT" if alerts else "HEALTHY",
        "activated_trades": len(rows),
        "alert_count": len(alerts),
        "alerts": alerts,
        "rows": rows,
    }


def write_activated_trade_invariant_status(
    session: Session, *, output_path: Path
) -> dict[str, Any]:
    payload = activated_trade_invariant_status(session)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(output_path)
    return payload


def activated_trade_invariant_row(
    session: Session,
    order: PaperOrder,
    *,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = raw if raw is not None else decode_json(order.raw_decision_json)
    same_forecast_orders = list(
        session.scalars(
            select(PaperOrder).where(
                PaperOrder.ticker == order.ticker,
                PaperOrder.model_name == order.model_name,
                PaperOrder.forecast_id == order.forecast_id,
            )
        )
    )
    fills = list(
        session.scalars(
            select(PaperFill)
            .where(PaperFill.paper_order_id == order.id)
            .order_by(PaperFill.id)
        )
    )
    sizing_ids = set(
        session.scalars(
            select(PositionSizingDecisionLog.id).where(
                PositionSizingDecisionLog.paper_order_id == order.id
            )
        )
    )
    risk_ids = set(
        session.scalars(
            select(AdvancedRiskDecisionLog.id).where(
                AdvancedRiskDecisionLog.paper_order_id == order.id
            )
        )
    )
    expected_sizing_id = _optional_int(raw.get("position_sizing_decision_id"))
    expected_risk_id = _optional_int(raw.get("advanced_risk_decision_id"))
    settlement = session.get(Settlement, order.ticker)
    pnl = session.scalar(
        select(PaperPnl)
        .where(PaperPnl.ticker == order.ticker)
        .order_by(desc(PaperPnl.calculated_at), desc(PaperPnl.id))
        .limit(1)
    )
    settlement_result = str(settlement.result or "").strip().lower() if settlement else ""
    realized = bool(
        settlement_result
        and pnl
        and str(pnl.settlement_result or "").strip().lower() == settlement_result
        and (pnl.notes or "").strip().lower() == "settled market realized paper p&l"
    )
    expected_pnl = _expected_final_pnl(order, fills[0], settlement_result) if fills and settlement_result else None
    recorded_pnl = to_decimal(pnl.realized_pnl) if realized and pnl else None
    alerts: list[dict[str, Any]] = []

    def alert(code: str, detail: str) -> None:
        alerts.append(
            {
                "code": code,
                "ticker": order.ticker,
                "paper_order_id": order.id,
                "detail": detail,
            }
        )

    if len(same_forecast_orders) > 1:
        alert("DUPLICATE_FORECAST_ORDER", f"Found {len(same_forecast_orders)} orders.")
    if len(fills) > 1:
        alert("DUPLICATE_ORDER_FILL", f"Found {len(fills)} fills.")
    if expected_sizing_id is None or sizing_ids != {expected_sizing_id}:
        alert(
            "POSITION_SIZING_ATTRIBUTION_CHANGED",
            f"Expected {expected_sizing_id}; linked IDs are {sorted(sizing_ids)}.",
        )
    if expected_risk_id is None or risk_ids != {expected_risk_id}:
        alert(
            "ADVANCED_RISK_ATTRIBUTION_CHANGED",
            f"Expected {expected_risk_id}; linked IDs are {sorted(risk_ids)}.",
        )
    if settlement_result and not realized:
        alert(
            "SETTLEMENT_WITHOUT_REALIZED_PNL",
            f"Settlement result {settlement_result!r} exists without matching realized P&L.",
        )
    if realized and expected_pnl is not None and recorded_pnl != expected_pnl:
        alert(
            "FINAL_PAYOUT_MISMATCH",
            f"Expected realized P&L {expected_pnl}; recorded {recorded_pnl}.",
        )
    return {
        "ticker": order.ticker,
        "paper_order_id": order.id,
        "forecast_id": order.forecast_id,
        "forecast_snapshot_pair_key": raw.get("forecast_snapshot_pair_key"),
        "order_count": len(same_forecast_orders),
        "fill_count": len(fills),
        "expected_position_sizing_decision_id": expected_sizing_id,
        "linked_position_sizing_decision_ids": sorted(sizing_ids),
        "expected_advanced_risk_decision_id": expected_risk_id,
        "linked_advanced_risk_decision_ids": sorted(risk_ids),
        "settlement_state": "SETTLED" if settlement_result else "AWAITING_SETTLEMENT",
        "settlement_result": settlement_result or None,
        "settled_at": (
            settlement.settled_at.isoformat()
            if settlement and settlement.settled_at
            else None
        ),
        "realization_state": "REALIZED" if realized else "PENDING",
        "fill_price": fills[0].price if fills else None,
        "fill_fee": fills[0].fee if fills else None,
        "expected_final_pnl": decimal_to_str(expected_pnl),
        "recorded_realized_pnl": decimal_to_str(recorded_pnl),
        "status": "ALERT" if alerts else "HEALTHY",
        "alerts": alerts,
    }


def _expected_final_pnl(
    order: PaperOrder, fill: PaperFill, settlement_result: str
) -> Decimal:
    price = to_decimal(fill.price) or Decimal("0")
    fee = to_decimal(fill.fee) or Decimal("0")
    quantity = Decimal(fill.quantity)
    wins = (order.side == BUY_YES and settlement_result == "yes") or (
        order.side == BUY_NO and settlement_result == "no"
    )
    payout = quantity if wins else Decimal("0")
    return payout - (price * quantity) - fee


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
