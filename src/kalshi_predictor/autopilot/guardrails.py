from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.autopilot.repository import (
    count_daily_submitted_orders,
    count_open_demo_orders,
    current_daily_pnl,
    has_duplicate_attempt,
    insert_risk_event,
)
from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import MarketSnapshot, RiskEvent
from kalshi_predictor.forecasting.registry import MODEL_NAMES
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class GuardrailResult:
    allowed: bool
    events: list[RiskEvent]

    @property
    def stop_reason(self) -> str | None:
        if not self.events:
            return None
        return "; ".join(event.message for event in self.events)


def evaluate_start_guardrails(
    session: Session,
    *,
    settings: Settings,
    autopilot_run_id: int | None = None,
    autopilot_cycle_id: int | None = None,
) -> GuardrailResult:
    if not settings.autopilot_enabled:
        return _blocked(
            session,
            settings=settings,
            guardrail_name="autopilot_enabled",
            message="AUTOPILOT_ENABLED=false; autopilot is disabled.",
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    if settings.kalshi_env.lower() != "demo":
        return _blocked(
            session,
            settings=settings,
            guardrail_name="kalshi_env",
            message=f"KALSHI_ENV={settings.kalshi_env}; autopilot requires demo.",
            raw={"KALSHI_ENV": settings.kalshi_env},
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    if settings.execution_kill_switch:
        return _blocked(
            session,
            settings=settings,
            guardrail_name="kill_switch",
            message="EXECUTION_KILL_SWITCH=true; autopilot stopped.",
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    if not settings.autopilot_dry_run and not settings.execution_enabled:
        return _blocked(
            session,
            settings=settings,
            guardrail_name="execution_enabled",
            message="EXECUTION_ENABLED=false; non-dry-run autopilot is blocked.",
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    if settings.autopilot_model not in MODEL_NAMES:
        return _blocked(
            session,
            settings=settings,
            guardrail_name="model_allow_list",
            message=f"Model {settings.autopilot_model} is not in the allow-list.",
            raw={"allowed_models": MODEL_NAMES},
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    daily_orders = count_daily_submitted_orders(session)
    if daily_orders >= settings.autopilot_max_daily_orders:
        return _blocked(
            session,
            settings=settings,
            guardrail_name="max_daily_orders",
            message=(
                "Daily demo order limit reached: "
                f"{daily_orders}/{settings.autopilot_max_daily_orders}."
            ),
            raw={"daily_orders": daily_orders},
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    open_orders = count_open_demo_orders(session)
    if open_orders >= settings.autopilot_max_open_demo_orders:
        return _blocked(
            session,
            settings=settings,
            guardrail_name="max_open_demo_orders",
            message=(
                "Open demo order limit reached: "
                f"{open_orders}/{settings.autopilot_max_open_demo_orders}."
            ),
            raw={"open_demo_orders": open_orders},
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    freshness = latest_snapshot_freshness(session)
    if freshness["latest_captured_at"] is None:
        return _blocked(
            session,
            settings=settings,
            guardrail_name="fresh_data",
            message="No market snapshots exist; autopilot requires fresh data.",
            raw=freshness,
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    max_age = Decimal(str(settings.autopilot_require_fresh_data_minutes))
    age = to_decimal(freshness["age_minutes"])
    if age is None or age > max_age:
        return _blocked(
            session,
            settings=settings,
            guardrail_name="fresh_data",
            message=(
                f"Latest snapshot is {freshness['age_minutes']} minutes old; "
                f"limit is {settings.autopilot_require_fresh_data_minutes}."
            ),
            raw=freshness,
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    if settings.autopilot_stop_on_drawdown:
        daily_pnl = current_daily_pnl(session)
        if daily_pnl <= -settings.autopilot_max_daily_drawdown:
            return _blocked(
                session,
                settings=settings,
                guardrail_name="daily_drawdown",
                message=(
                    f"Daily P&L {daily_pnl} breached drawdown limit "
                    f"-{settings.autopilot_max_daily_drawdown}."
                ),
                raw={"daily_pnl": decimal_to_str(daily_pnl)},
                autopilot_run_id=autopilot_run_id,
                autopilot_cycle_id=autopilot_cycle_id,
            )

    return GuardrailResult(allowed=True, events=[])


def evaluate_opportunity_guardrails(
    session: Session,
    *,
    opportunity: dict[str, Any],
    settings: Settings,
    cycle_orders_attempted: int,
    autopilot_run_id: int | None = None,
    autopilot_cycle_id: int | None = None,
) -> GuardrailResult:
    ticker = str(opportunity.get("ticker") or "")
    model_name = str(opportunity.get("model_name") or settings.autopilot_model)
    side = str(opportunity.get("side") or "")
    if cycle_orders_attempted >= settings.autopilot_max_orders_per_cycle:
        return _blocked(
            session,
            settings=settings,
            guardrail_name="max_orders_per_cycle",
            message=(
                "Cycle order limit reached: "
                f"{cycle_orders_attempted}/{settings.autopilot_max_orders_per_cycle}."
            ),
            ticker=ticker,
            raw=opportunity,
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    daily_orders = count_daily_submitted_orders(session)
    if daily_orders >= settings.autopilot_max_daily_orders:
        return _blocked(
            session,
            settings=settings,
            guardrail_name="max_daily_orders",
            message=(
                "Daily demo order limit reached: "
                f"{daily_orders}/{settings.autopilot_max_daily_orders}."
            ),
            ticker=ticker,
            raw=opportunity,
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    open_orders = count_open_demo_orders(session)
    if open_orders >= settings.autopilot_max_open_demo_orders:
        return _blocked(
            session,
            settings=settings,
            guardrail_name="max_open_demo_orders",
            message=(
                "Open demo order limit reached: "
                f"{open_orders}/{settings.autopilot_max_open_demo_orders}."
            ),
            ticker=ticker,
            raw=opportunity,
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    edge = to_decimal(opportunity.get("estimated_edge")) or Decimal("0")
    if edge < settings.autopilot_min_edge:
        return _blocked(
            session,
            settings=settings,
            guardrail_name="min_edge",
            message=f"Edge {edge} is below minimum {settings.autopilot_min_edge}.",
            ticker=ticker,
            raw=opportunity,
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    score = to_decimal(opportunity.get("opportunity_score")) or Decimal("0")
    if score < settings.autopilot_min_opportunity_score:
        return _blocked(
            session,
            settings=settings,
            guardrail_name="min_opportunity_score",
            message=(
                f"Opportunity score {score} is below minimum "
                f"{settings.autopilot_min_opportunity_score}."
            ),
            ticker=ticker,
            raw=opportunity,
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    if not side or not opportunity.get("price"):
        return _blocked(
            session,
            settings=settings,
            guardrail_name="side_and_price",
            message="Opportunity lacks executable side or price.",
            ticker=ticker,
            raw=opportunity,
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    if has_duplicate_attempt(session, ticker=ticker, model_name=model_name, side=side):
        return _blocked(
            session,
            settings=settings,
            guardrail_name="duplicate_ticker_model_side",
            message=f"Duplicate {ticker}/{model_name}/{side} autopilot attempt exists today.",
            ticker=ticker,
            raw=opportunity,
            autopilot_run_id=autopilot_run_id,
            autopilot_cycle_id=autopilot_cycle_id,
        )

    return GuardrailResult(allowed=True, events=[])


def latest_snapshot_freshness(session: Session) -> dict[str, Any]:
    snapshot = session.scalar(
        select(MarketSnapshot)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )
    if snapshot is None:
        return {"latest_captured_at": None, "age_minutes": None}
    captured_at = snapshot.captured_at
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=UTC)
    age_minutes = Decimal(str((utc_now() - captured_at).total_seconds() / 60))
    return {
        "latest_captured_at": captured_at.isoformat(),
        "age_minutes": decimal_to_str(age_minutes),
        "ticker": snapshot.ticker,
    }


def _blocked(
    session: Session,
    *,
    settings: Settings,
    guardrail_name: str,
    message: str,
    severity: str = "BLOCK",
    ticker: str | None = None,
    raw: dict[str, Any] | tuple[str, ...] | None = None,
    autopilot_run_id: int | None = None,
    autopilot_cycle_id: int | None = None,
) -> GuardrailResult:
    raw_payload = raw if isinstance(raw, dict) else {"value": raw}
    event = insert_risk_event(
        session,
        guardrail_name=guardrail_name,
        message=message,
        severity=severity,
        ticker=ticker,
        model_name=settings.autopilot_model,
        raw=raw_payload,
        autopilot_run_id=autopilot_run_id,
        autopilot_cycle_id=autopilot_cycle_id,
    )
    return GuardrailResult(allowed=False, events=[event])
