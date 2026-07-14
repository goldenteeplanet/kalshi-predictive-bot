from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    Market,
    MarketRanking,
    PaperOrder,
    Signal,
    SignalEvent,
    SignalPerformance,
    SignalTrade,
)
from kalshi_predictor.opportunities.market_identity import annotated_opportunity_row
from kalshi_predictor.signals.registry import ensure_builtin_signals
from kalshi_predictor.signals.scoring import performance_rank_key, refresh_signal_performance
from kalshi_predictor.signals.status import signal_health_context, signal_status_rows
from kalshi_predictor.utils.decimals import to_decimal


def signal_marketplace(session: Session) -> dict[str, Any]:
    signals = ensure_builtin_signals(session)
    performance = _latest_performance_by_signal(session)
    if not performance:
        rows = refresh_signal_performance(session)
        performance = {row.signal_name: row for row in rows}
    readiness = {row["signal_name"]: row for row in signal_status_rows(session)}
    cards = [
        _signal_card(signal, performance.get(signal.signal_name), readiness.get(signal.signal_name))
        for signal in sorted(signals, key=lambda row: row.signal_name)
    ]
    leaderboard = sorted(
        [card for card in cards],
        key=_card_rank_key,
        reverse=True,
    )
    for index, row in enumerate(leaderboard, start=1):
        row["rank"] = index
    return {
        "cards": cards,
        "leaderboard": leaderboard,
        "summary": {
            "signals": len(cards),
            "active_forecasts": sum(card["forecast_count"] for card in cards),
            "active_trades": sum(card["trade_count"] for card in cards),
        },
    }


def signal_detail(session: Session, *, signal_name: str) -> dict[str, Any] | None:
    ensure_builtin_signals(session)
    signal = session.scalar(select(Signal).where(Signal.signal_name == signal_name))
    if signal is None:
        return None
    performance = _latest_performance_by_signal(session).get(signal.signal_name)
    events = list(
        session.scalars(
            select(SignalEvent)
            .where(SignalEvent.signal_name == signal.signal_name)
            .order_by(desc(SignalEvent.created_at), desc(SignalEvent.id))
            .limit(20)
        )
    )
    trades = list(
        session.scalars(
            select(PaperOrder)
            .join(SignalTrade, SignalTrade.paper_order_id == PaperOrder.id)
            .where(SignalTrade.signal_name == signal.signal_name)
            .order_by(desc(PaperOrder.created_at), desc(PaperOrder.id))
            .limit(20)
        )
    )
    tickers = [event.ticker for event in events[:10]]
    opportunities = _recent_opportunities(session, tickers)
    markets = _market_performance_rows(trades)
    return {
        "signal": signal,
        "card": _signal_card(signal, performance),
        "metadata": decode_json(signal.metadata_json),
        "events": [_event_row(row) for row in events],
        "recent_trades": [_trade_row(row) for row in trades],
        "recent_opportunities": opportunities,
        "recent_markets": tickers,
        "top_markets": markets[:5],
        "worst_markets": list(reversed(markets[-5:])),
        "research_summary": _research_summary(signal, performance),
    }


def signal_leaderboard_rows(session: Session, *, refresh: bool = False) -> list[dict[str, Any]]:
    ensure_builtin_signals(session)
    if refresh or not _latest_performance_by_signal(session):
        refresh_signal_performance(session)
    rows = list(_latest_performance_by_signal(session).values())
    readiness = {row["signal_name"]: row for row in signal_status_rows(session)}
    rows.sort(
        key=lambda row: (
            readiness.get(row.signal_name, {}).get("readiness_status") == "ACTIVE",
            *performance_rank_key(row),
        ),
        reverse=True,
    )
    return [
        _leaderboard_row(index, row, readiness.get(row.signal_name))
        for index, row in enumerate(rows, start=1)
    ]


def signal_explorer_rows(session: Session, *, refresh: bool = False) -> list[dict[str, Any]]:
    marketplace = signal_marketplace(session)
    if refresh:
        refresh_signal_performance(session)
        marketplace = signal_marketplace(session)
    rows = []
    for card in marketplace["cards"]:
        models = _models_for_signal(session, card["signal_name"])
        rows.append(
            {
                **card,
                "associated_models": models or ["n/a"],
                "current_activity": _activity_for_signal(session, card["signal_name"]),
            }
        )
    return rows


def signal_performance_summary(
    session: Session,
    *,
    signal_name: str,
    refresh: bool = False,
) -> dict[str, Any] | None:
    ensure_builtin_signals(session)
    if refresh:
        refresh_signal_performance(session)
    return signal_detail(session, signal_name=signal_name)


def signal_health(session: Session) -> dict[str, Any]:
    return signal_health_context(session)


def _latest_performance_by_signal(session: Session) -> dict[str, SignalPerformance]:
    rows = list(
        session.scalars(
            select(SignalPerformance).order_by(
                SignalPerformance.signal_name,
                desc(SignalPerformance.generated_at),
                desc(SignalPerformance.id),
            )
        )
    )
    latest: dict[str, SignalPerformance] = {}
    for row in rows:
        if row.signal_name not in latest:
            latest[row.signal_name] = row
    return latest


def _signal_card(
    signal: Signal,
    performance: SignalPerformance | None,
    readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    performance_status = signal.status if performance is None else decode_json(
        performance.raw_json
    ).get(
        "status",
        signal.status,
    )
    status = (
        readiness.get("status_label", performance_status)
        if readiness and readiness.get("readiness_status") != "ACTIVE"
        else performance_status
    )
    return {
        "signal_name": signal.signal_name,
        "category": signal.category,
        "description": signal.description,
        "status": status,
        "roi": performance.roi if performance else None,
        "win_rate": performance.win_rate if performance else None,
        "trade_count": performance.trade_count if performance else 0,
        "forecast_count": performance.forecast_count if performance else 0,
        "settled_trade_count": performance.settled_trade_count if performance else 0,
        "confidence_score": performance.confidence_score if performance else None,
        "brier_score": performance.brier_score if performance else None,
        "total_pnl": performance.total_pnl if performance else None,
        "avg_edge": performance.avg_edge if performance else None,
        "avg_opportunity_score": performance.avg_opportunity_score if performance else None,
        "readiness_status": (readiness or {}).get("readiness_status", "UNKNOWN"),
        "status_label": (readiness or {}).get("status_label", status),
        "missing_data": (readiness or {}).get("missing_data", "none"),
        "next_action": (readiness or {}).get("next_action", "No action needed."),
        "latest_signal": (readiness or {}).get("latest_signal", "none"),
        "skip_count": (readiness or {}).get("skip_count", 0),
        "skip_reason": (readiness or {}).get("skip_reason", "No skip logged yet."),
    }


def _leaderboard_row(
    rank: int,
    row: SignalPerformance,
    readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    performance_status = decode_json(row.raw_json).get("status", "Insufficient Data")
    status = (
        readiness.get("status_label", performance_status)
        if readiness and readiness.get("readiness_status") != "ACTIVE"
        else performance_status
    )
    return {
        "rank": rank,
        "signal_name": row.signal_name,
        "category": row.category,
        "roi": row.roi,
        "win_rate": row.win_rate,
        "forecast_count": row.forecast_count,
        "trade_count": row.trade_count,
        "confidence_score": row.confidence_score,
        "brier_score": row.brier_score,
        "status": status,
        "readiness_status": (readiness or {}).get("readiness_status", "UNKNOWN"),
        "status_label": (readiness or {}).get("status_label", status),
        "missing_data": (readiness or {}).get("missing_data", "none"),
        "next_action": (readiness or {}).get("next_action", "No action needed."),
        "latest_signal": (readiness or {}).get("latest_signal", "none"),
        "skip_count": (readiness or {}).get("skip_count", 0),
        "skip_reason": (readiness or {}).get("skip_reason", "No skip logged yet."),
    }


def _card_rank_key(card: dict[str, Any]) -> tuple[int, Any, Any, int, int]:
    active = 1 if card.get("readiness_status") == "ACTIVE" else 0
    return (
        active,
        to_decimal(card["roi"]) or -999,
        to_decimal(card["confidence_score"]) or 0,
        card["forecast_count"],
        card["trade_count"],
    )


def _event_row(row: SignalEvent) -> dict[str, Any]:
    return {
        "created_at": row.created_at.isoformat(),
        "ticker": row.ticker,
        "model_name": row.model_name,
        "signal_strength": row.signal_strength,
        "signal_value": row.signal_value,
        "signal_direction": row.signal_direction,
        "confidence": row.confidence,
    }


def _trade_row(row: PaperOrder) -> dict[str, Any]:
    return {
        "created_at": row.created_at.isoformat(),
        "ticker": row.ticker,
        "model_name": row.model_name,
        "side": row.side,
        "quantity": row.quantity,
        "edge": row.edge,
        "status": row.status,
    }


def _recent_opportunities(session: Session, tickers: list[str]) -> list[dict[str, Any]]:
    if not tickers:
        return []
    rows = list(
        session.scalars(
            select(MarketRanking)
            .where(MarketRanking.ticker.in_(tickers))
            .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
            .limit(20)
        )
    )
    seen: set[str] = set()
    opportunities: list[dict[str, Any]] = []
    for row in rows:
        if row.ticker in seen:
            continue
        seen.add(row.ticker)
        opportunities.append(
            annotated_opportunity_row(
                session,
                {
                    "ticker": row.ticker,
                    "title": row.title or row.ticker,
                    "model_name": row.forecast_model,
                    "score": row.opportunity_score,
                    "edge": row.estimated_edge,
                },
                ticker=row.ticker,
                ranking=row,
                market=session.get(Market, row.ticker),
            )
        )
    return opportunities


def _market_performance_rows(trades: list[PaperOrder]) -> list[dict[str, Any]]:
    rows = [
        {
            "ticker": trade.ticker,
            "edge": trade.edge,
            "model_name": trade.model_name,
            "quantity": trade.quantity,
            "status": trade.status,
        }
        for trade in trades
    ]
    return sorted(rows, key=lambda row: to_decimal(row["edge"]) or 0, reverse=True)


def _models_for_signal(session: Session, signal_name: str) -> list[str]:
    rows = list(
        session.scalars(
            select(SignalEvent.model_name)
            .where(SignalEvent.signal_name == signal_name, SignalEvent.model_name.is_not(None))
            .distinct()
        )
    )
    return sorted(str(row) for row in rows if row)


def _activity_for_signal(session: Session, signal_name: str) -> int:
    return len(
        list(
            session.scalars(
                select(SignalEvent.id)
                .where(SignalEvent.signal_name == signal_name)
                .order_by(desc(SignalEvent.created_at))
                .limit(50)
            )
        )
    )


def _research_summary(signal: Signal, performance: SignalPerformance | None) -> str:
    if performance is None:
        return f"{signal.signal_name} needs more forecast and paper-trade data."
    status = decode_json(performance.raw_json).get("status", signal.status)
    return (
        f"{signal.signal_name} is currently {status}. ROI is {performance.roi or 'n/a'}, "
        f"win rate is {performance.win_rate or 'n/a'}, and confidence is "
        f"{performance.confidence_score or 'n/a'}."
    )
