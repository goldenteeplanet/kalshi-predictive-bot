from datetime import UTC
from typing import Any

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    Forecast,
    LearningCycle,
    LearningRun,
    PaperOrder,
    Settlement,
)
from kalshi_predictor.paper.models import ORDER_FILLED
from kalshi_predictor.utils.time import utc_now


def learning_blocks_demo_execution(settings: Settings) -> bool:
    return settings.learning_mode and settings.learning_block_demo_execution


def learning_blocks_live_execution(settings: Settings) -> bool:
    return settings.learning_mode and settings.learning_block_live_execution


def settled_paper_trade_count(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count(func.distinct(PaperOrder.id)))
            .select_from(PaperOrder)
            .join(Settlement, PaperOrder.ticker == Settlement.ticker)
            .where(
                PaperOrder.status == ORDER_FILLED,
                or_(
                    Settlement.result.in_(("yes", "no")),
                    and_(
                        Settlement.yes_settlement_value.is_not(None),
                        func.trim(Settlement.yes_settlement_value) != "",
                    ),
                ),
            )
        )
        or 0
    )


def daily_paper_trade_count(session: Session) -> int:
    today = utc_now().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        session.scalar(
            select(func.count())
            .select_from(PaperOrder)
            .where(PaperOrder.created_at >= today)
        )
        or 0
    )


def learning_daily_cap_status(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    daily_trades = daily_paper_trade_count(session)
    daily_cap = resolved_settings.learning_max_daily_paper_trades
    reached = daily_trades >= daily_cap
    return {
        "daily_trades": daily_trades,
        "daily_cap": daily_cap,
        "reached": reached,
        "message": (
            f"Daily learning paper trade cap reached: {daily_trades} / {daily_cap}."
            if reached
            else ""
        ),
        "next_action": (
            "Wait until tomorrow or increase LEARNING_MAX_DAILY_PAPER_TRADES."
            if reached
            else ""
        ),
        "tooltip": (
            "Learning cap reached. The bot will continue syncing settlements and reports, "
            "but will not create more paper trades today."
        ),
    }


def latest_learning_run(session: Session) -> LearningRun | None:
    return session.scalar(
        select(LearningRun).order_by(desc(LearningRun.started_at), desc(LearningRun.id)).limit(1)
    )


def latest_learning_cycle(session: Session) -> LearningCycle | None:
    return session.scalar(
        select(LearningCycle)
        .order_by(desc(LearningCycle.started_at), desc(LearningCycle.id))
        .limit(1)
    )


def learning_status(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    settled = settled_paper_trade_count(session)
    target = max(1, resolved_settings.learning_target_settled_trades)
    cap_status = learning_daily_cap_status(session, settings=resolved_settings)
    daily_trades = int(cap_status["daily_trades"])
    latest_run = latest_learning_run(session)
    latest_cycle = latest_learning_cycle(session)
    progress = min(_decimal_percent(settled, target), 100.0)
    forecasts_evaluated = forecast_evaluation_count(session)
    return {
        "enabled": resolved_settings.learning_mode,
        "target_settled_trades": target,
        "settled_paper_trades": settled,
        "current_settled_trades": settled,
        "remaining_settled_trades": max(0, target - settled),
        "progress_percent": f"{progress:.1f}%",
        "progress_value": f"{progress:.1f}",
        "progress_bar_width": f"{progress:.1f}%",
        "daily_paper_trades": daily_trades,
        "paper_trades_created_today": daily_trades,
        "daily_paper_trade_cap": cap_status["daily_cap"],
        "daily_cap_reached": cap_status["reached"],
        "daily_cap_message": cap_status["message"],
        "daily_cap_next_action": cap_status["next_action"],
        "daily_cap_tooltip": cap_status["tooltip"],
        "min_trades_per_cycle": resolved_settings.learning_min_trades_per_cycle,
        "target_trades_per_cycle": resolved_settings.learning_target_trades_per_cycle,
        "trade_generation_health": _trade_generation_health(daily_trades),
        "fast_settlement_priority": resolved_settings.learning_prioritize_fast_settlement,
        "max_days_to_settlement": resolved_settings.learning_max_days_to_settlement,
        "forecasts_evaluated": forecasts_evaluated,
        "expected_completion": _expected_completion(
            settled=settled,
            target=target,
            daily_trades=daily_trades,
            max_days_to_settlement=resolved_settings.learning_max_days_to_settlement,
        ),
        "demo_execution_blocked": learning_blocks_demo_execution(resolved_settings),
        "live_execution_blocked": learning_blocks_live_execution(resolved_settings),
        "latest_run_status": latest_run.status if latest_run else "none",
        "latest_cycle_status": latest_cycle.status if latest_cycle else "none",
        "latest_cycle_id": latest_cycle.id if latest_cycle else None,
        "plain_status": _plain_status(resolved_settings, latest_run, settled, target),
        "recommended_next_action": _recommended_action(resolved_settings, settled, target),
    }


def forecast_evaluation_count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(Forecast)) or 0)


def _decimal_percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return (numerator / denominator) * 100.0


def _plain_status(
    settings: Settings,
    latest_run: LearningRun | None,
    settled: int,
    target: int,
) -> str:
    if not settings.learning_mode:
        return "Learning Mode is OFF"
    if settled >= target:
        return "Learning Mode target reached"
    if latest_run is not None:
        return f"Learning Mode is {latest_run.status}"
    return "Learning Mode is ON and ready"


def _recommended_action(settings: Settings, settled: int, target: int) -> str:
    if not settings.learning_mode:
        return "Turn Learning Mode on before running the learning loop."
    if settled >= target:
        return "Review model confidence before trusting the algorithm with larger stakes."
    return "Run paper-only learning cycles as the primary trade generator."


def _expected_completion(
    *,
    settled: int,
    target: int,
    daily_trades: int,
    max_days_to_settlement: int,
) -> str:
    remaining = max(0, target - settled)
    if remaining == 0:
        return "Target reached"
    if daily_trades <= 0:
        return "Needs today's paper trades before estimate"
    days = (remaining + daily_trades - 1) // daily_trades
    return (
        f"About {days} day(s) at today's pace, plus up to "
        f"{max_days_to_settlement} settlement day(s)"
    )


def _trade_generation_health(daily_trades: int) -> dict[str, str]:
    if daily_trades > 10:
        return {
            "label": "Green",
            "kind": "good",
            "message": "Trade generation is healthy.",
        }
    if daily_trades >= 5:
        return {
            "label": "Yellow",
            "kind": "warn",
            "message": "Trade generation is acceptable but below target pace.",
        }
    return {
        "label": "Red",
        "kind": "risk",
        "message": "Trade generation is too low for fast learning.",
    }
