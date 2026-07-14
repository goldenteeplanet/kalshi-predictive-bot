from dataclasses import asdict, dataclass
from typing import Any, Protocol

from sqlalchemy.orm import Session

from kalshi_predictor.autopilot.guardrails import (
    evaluate_opportunity_guardrails,
    evaluate_start_guardrails,
    latest_snapshot_freshness,
)
from kalshi_predictor.autopilot.repository import (
    complete_autopilot_cycle,
    complete_autopilot_run,
    create_autopilot_cycle,
    create_autopilot_run,
)
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import get_recent_snapshots
from kalshi_predictor.data.schema import AutopilotCycle, AutopilotRun
from kalshi_predictor.forecasting.registry import run_forecast_models
from kalshi_predictor.lanes.metrics import refresh_autopilot_metrics
from kalshi_predictor.lanes.repository import (
    insert_autopilot_opportunity,
    insert_autopilot_paper_trade,
)
from kalshi_predictor.opportunities.scanner import scan_opportunities
from kalshi_predictor.ui.service import DecisionUiService
from kalshi_predictor.utils.decimals import decimal_to_str


@dataclass(frozen=True)
class AutopilotCycleResult:
    run_id: int
    cycle_id: int
    status: str
    orders_attempted: int
    orders_submitted: int
    orders_blocked: int
    opportunities_scanned: int
    stop_reason: str | None
    summary: dict[str, Any]


class AutopilotExecutionClient(Protocol):
    def execute(
        self,
        session: Session,
        *,
        opportunity: dict[str, Any],
        settings: Settings,
    ) -> dict[str, Any]:
        """Execute a demo-only order path and return an auditable summary."""


class Phase3ADemoExecutionClient:
    def execute(
        self,
        session: Session,
        *,
        opportunity: dict[str, Any],
        settings: Settings,
    ) -> dict[str, Any]:
        service = DecisionUiService(session, settings=settings)
        result = service.demo_execute(
            str(opportunity["ticker"]),
            confirmation=settings.execution_confirmation_token,
        )
        return asdict(result)


def run_autopilot_once(
    session: Session,
    *,
    settings: Settings | None = None,
    execution_client: AutopilotExecutionClient | None = None,
) -> AutopilotCycleResult:
    resolved_settings = settings or get_settings()
    run = create_autopilot_run(session, resolved_settings)
    result = run_autopilot_cycle(
        session,
        run=run,
        cycle_number=1,
        settings=resolved_settings,
        execution_client=execution_client,
    )
    complete_autopilot_run(
        session,
        run,
        status=result.status,
        cycles_completed=1,
        orders_attempted=result.orders_attempted,
        orders_submitted=result.orders_submitted,
        orders_blocked=result.orders_blocked,
        stop_reason=result.stop_reason,
        summary=result.summary,
    )
    return result


def run_autopilot_cycle(
    session: Session,
    *,
    run: AutopilotRun,
    cycle_number: int,
    settings: Settings,
    execution_client: AutopilotExecutionClient | None = None,
) -> AutopilotCycleResult:
    cycle = create_autopilot_cycle(
        session,
        run_id=run.id,
        cycle_number=cycle_number,
        settings=settings,
    )
    start_result = evaluate_start_guardrails(
        session,
        settings=settings,
        autopilot_run_id=run.id,
        autopilot_cycle_id=cycle.id,
    )
    if not start_result.allowed:
        summary = {
            "status": "BLOCKED",
            "stage": "start_guardrails",
            "risk_events": [_risk_event_summary(event) for event in start_result.events],
            "freshness": latest_snapshot_freshness(session),
            "order_attempts": [],
            "dry_run_orders": [],
            "submitted_orders": [],
            "blocked_orders": [],
        }
        return _finish_cycle(
            session,
            cycle=cycle,
            status="BLOCKED",
            opportunities_scanned=0,
            orders_attempted=0,
            orders_submitted=0,
            orders_blocked=len(start_result.events),
            stop_reason=start_result.stop_reason,
            summary=summary,
        )

    snapshots = get_recent_snapshots(session, limit=100)
    forecast_summary = run_forecast_models(
        session,
        model_name=settings.autopilot_model,
        snapshots=snapshots,
    )
    scan_limit = max(
        settings.opportunity_max_results,
        settings.autopilot_max_orders_per_cycle + 5,
    )
    autopilot_settings = settings.model_copy(update={"learning_mode": False})
    opportunity_summary = scan_opportunities(
        session,
        model_name=settings.autopilot_model,
        limit=scan_limit,
        settings=autopilot_settings,
        min_edge=settings.autopilot_min_edge,
        min_score=settings.autopilot_min_opportunity_score,
    )
    for opportunity in opportunity_summary.opportunities:
        insert_autopilot_opportunity(
            session,
            {
                **opportunity,
                "autopilot_run_id": run.id,
                "autopilot_cycle_id": cycle.id,
                "source": "autopilot-cycle",
            },
        )

    order_attempts: list[dict[str, Any]] = []
    dry_run_orders: list[dict[str, Any]] = []
    submitted_orders: list[dict[str, Any]] = []
    blocked_orders: list[dict[str, Any]] = []
    execution_results: list[dict[str, Any]] = []
    client = execution_client or Phase3ADemoExecutionClient()

    for opportunity in opportunity_summary.opportunities:
        candidate = _candidate_from_opportunity(opportunity)
        guardrail_result = evaluate_opportunity_guardrails(
            session,
            opportunity=candidate,
            settings=settings,
            cycle_orders_attempted=len(order_attempts),
            autopilot_run_id=run.id,
            autopilot_cycle_id=cycle.id,
        )
        if not guardrail_result.allowed:
            blocked_orders.append(
                {
                    **candidate,
                    "risk_events": [
                        _risk_event_summary(event) for event in guardrail_result.events
                    ],
                }
            )
            continue

        order_attempts.append(candidate)
        if settings.autopilot_dry_run:
            dry_run_order = {**candidate, "status": "DRY_RUN"}
            dry_run_orders.append(dry_run_order)
            insert_autopilot_paper_trade(
                session,
                {
                    **dry_run_order,
                    "autopilot_run_id": run.id,
                    "autopilot_cycle_id": cycle.id,
                    "quantity": settings.paper_max_order_quantity,
                    "raw_json": dry_run_order,
                },
            )
            continue

        execution_result = client.execute(
            session,
            opportunity=candidate,
            settings=settings,
        )
        execution_results.append(execution_result)
        if _execution_was_submitted(execution_result):
            submitted_order = {**candidate, "execution_result": execution_result}
            submitted_orders.append(submitted_order)
            insert_autopilot_paper_trade(
                session,
                {
                    **submitted_order,
                    "autopilot_run_id": run.id,
                    "autopilot_cycle_id": cycle.id,
                    "quantity": settings.paper_max_order_quantity,
                    "status": "SUBMITTED",
                    "raw_json": submitted_order,
                },
            )

    status = _cycle_status(
        dry_run=settings.autopilot_dry_run,
        attempts=len(order_attempts),
        submitted=len(submitted_orders),
        blocked=len(blocked_orders),
    )
    stop_reason = _stop_reason(status, blocked_orders)
    summary = {
        "status": status,
        "stage": "completed",
        "freshness": latest_snapshot_freshness(session),
        "forecast": {
            "snapshots_scanned": forecast_summary.snapshots_scanned,
            "forecasts_inserted": forecast_summary.forecasts_inserted,
            "skipped": forecast_summary.skipped,
        },
        "opportunities": {
            "markets_scanned": opportunity_summary.markets_scanned,
            "rankings_inserted": opportunity_summary.rankings_inserted,
            "opportunities_detected": opportunity_summary.opportunities_detected,
            "top_opportunity_ticker": opportunity_summary.top_opportunity_ticker,
            "top_opportunity_score": decimal_to_str(opportunity_summary.top_opportunity_score),
        },
        "order_attempts": order_attempts,
        "dry_run_orders": dry_run_orders,
        "submitted_orders": submitted_orders,
        "blocked_orders": blocked_orders,
        "execution_results": execution_results,
    }
    metric = refresh_autopilot_metrics(session, settings=settings)
    summary["autopilot_metric_id"] = metric.id
    return _finish_cycle(
        session,
        cycle=cycle,
        status=status,
        opportunities_scanned=opportunity_summary.opportunities_detected,
        orders_attempted=len(order_attempts),
        orders_submitted=len(submitted_orders),
        orders_blocked=len(blocked_orders),
        stop_reason=stop_reason,
        summary=summary,
    )


def _finish_cycle(
    session: Session,
    *,
    cycle: AutopilotCycle,
    status: str,
    opportunities_scanned: int,
    orders_attempted: int,
    orders_submitted: int,
    orders_blocked: int,
    stop_reason: str | None,
    summary: dict[str, Any],
) -> AutopilotCycleResult:
    complete_autopilot_cycle(
        session,
        cycle,
        status=status,
        opportunities_scanned=opportunities_scanned,
        orders_attempted=orders_attempted,
        orders_submitted=orders_submitted,
        orders_blocked=orders_blocked,
        stop_reason=stop_reason,
        summary=summary,
    )
    return AutopilotCycleResult(
        run_id=cycle.autopilot_run_id,
        cycle_id=cycle.id,
        status=status,
        orders_attempted=orders_attempted,
        orders_submitted=orders_submitted,
        orders_blocked=orders_blocked,
        opportunities_scanned=opportunities_scanned,
        stop_reason=stop_reason,
        summary=summary,
    )


def _candidate_from_opportunity(opportunity: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": str(opportunity["ticker"]),
        "model_name": str(opportunity["model_name"]),
        "side": str(opportunity["side"]),
        "price": str(opportunity["price"]),
        "forecast_probability": str(opportunity["forecast_probability"]),
        "estimated_edge": str(opportunity["estimated_edge"]),
        "opportunity_score": str(opportunity["opportunity_score"]),
        "reason": str(opportunity.get("reason") or ""),
    }


def _risk_event_summary(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "guardrail_name": event.guardrail_name,
        "message": event.message,
        "ticker": event.ticker,
        "severity": event.severity,
        "created_at": event.created_at.isoformat(),
    }


def _cycle_status(*, dry_run: bool, attempts: int, submitted: int, blocked: int) -> str:
    if dry_run and attempts:
        return "DRY_RUN"
    if submitted:
        return "SUBMITTED"
    if blocked and not attempts:
        return "BLOCKED"
    return "COMPLETED"


def _execution_was_submitted(execution_result: dict[str, Any]) -> bool:
    status = str(execution_result.get("status") or "").upper()
    return status in {"SUBMITTED", "DEMO_SUBMITTED", "DEMO_ORDER_SUBMITTED"}


def _stop_reason(status: str, blocked_orders: list[dict[str, Any]]) -> str | None:
    if status != "BLOCKED" or not blocked_orders:
        return None
    first = blocked_orders[0].get("risk_events", [{}])[0]
    return str(first.get("message") or "Guardrails blocked all candidate orders.")
