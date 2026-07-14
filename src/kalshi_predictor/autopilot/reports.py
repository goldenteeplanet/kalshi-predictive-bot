from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.autopilot.guardrails import latest_snapshot_freshness
from kalshi_predictor.autopilot.repository import (
    autopilot_config_payload,
    count_daily_submitted_orders,
    count_open_demo_orders,
    decode_summary,
    latest_autopilot_cycle,
    latest_autopilot_run,
    recent_autopilot_cycles,
    recent_risk_events,
)
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


def build_autopilot_status(
    session: Session,
    *,
    settings: Settings | None = None,
    risk_event_limit: int = 10,
    cycle_limit: int = 5,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    latest_run = latest_autopilot_run(session)
    latest_cycle = latest_autopilot_cycle(session)
    risk_events = recent_risk_events(session, limit=risk_event_limit)
    cycles = recent_autopilot_cycles(session, limit=cycle_limit)
    freshness = latest_snapshot_freshness(session)
    return {
        "config": autopilot_config_payload(resolved_settings),
        "latest_run": _run_row(latest_run),
        "latest_cycle": _cycle_row(latest_cycle),
        "recent_cycles": [_cycle_row(cycle) for cycle in cycles],
        "risk_events": [_risk_event_row(event) for event in risk_events],
        "daily_orders": count_daily_submitted_orders(session),
        "open_demo_orders": count_open_demo_orders(session),
        "freshness": freshness,
        "report_path": "reports/autopilot_report.md",
        "plain_status": plain_autopilot_status(
            resolved_settings,
            latest_cycle_status=latest_cycle.status if latest_cycle else None,
        ),
        "blocked_reason": blocked_reason(latest_cycle, risk_events),
        "top_guardrail": top_guardrail_name(risk_events),
        "checklist": autopilot_checklist(resolved_settings, freshness=freshness),
        "recommended_next_action": recommended_next_action(
            resolved_settings,
            latest_run_status=latest_run.status if latest_run else None,
        ),
    }


def generate_autopilot_report(
    session: Session,
    *,
    output_path: Path = Path("reports/autopilot_report.md"),
    settings: Settings | None = None,
) -> Path:
    status = build_autopilot_status(session, settings=settings, risk_event_limit=20, cycle_limit=10)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_autopilot_report(status), encoding="utf-8")
    return output_path


def render_autopilot_report(status: dict[str, Any]) -> str:
    lines = [
        "# Autopilot Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "- Environment: DEMO ONLY",
        "- Production live trading: unavailable",
        "",
        "## Current Config",
        "",
    ]
    for key, value in status["config"].items():
        lines.append(f"- `{key}`: `{value}`")

    latest_run = status.get("latest_run")
    lines.extend(["", "## Last Run Summary", ""])
    if latest_run:
        lines.extend(
            [
                f"- Run ID: {latest_run['id']}",
                f"- Status: {latest_run['status']}",
                f"- Model: {latest_run['model_name']}",
                f"- Dry run: {bool(latest_run['dry_run'])}",
                f"- Cycles completed: {latest_run['cycles_completed']}",
                f"- Orders attempted: {latest_run['orders_attempted']}",
                f"- Orders submitted: {latest_run['orders_submitted']}",
                f"- Orders blocked: {latest_run['orders_blocked']}",
                f"- Stop reason: {latest_run['stop_reason'] or 'n/a'}",
            ]
        )
    else:
        lines.append("No autopilot runs have been recorded.")

    lines.extend(["", "## Recent Cycles", ""])
    if status["recent_cycles"]:
        lines.extend(
            [
                "| Cycle | Status | Model | Scanned | Attempted | "
                "Submitted | Blocked | Stop reason |",
                "|---|---|---|---:|---:|---:|---:|---|",
            ]
        )
        for cycle in status["recent_cycles"]:
            lines.append(
                "| "
                f"{cycle['id']} | {cycle['status']} | {cycle['model_name']} | "
                f"{cycle['opportunities_scanned']} | {cycle['orders_attempted']} | "
                f"{cycle['orders_submitted']} | {cycle['orders_blocked']} | "
                f"{cycle['stop_reason'] or 'n/a'} |"
            )
    else:
        lines.append("No cycles have been recorded.")

    lines.extend(["", "## Risk Events", ""])
    if status["risk_events"]:
        lines.extend(
            [
                "| Time | Guardrail | Ticker | Severity | Message |",
                "|---|---|---|---|---|",
            ]
        )
        for event in status["risk_events"]:
            lines.append(
                "| "
                f"{event['created_at']} | {event['guardrail_name']} | "
                f"{event['ticker'] or 'n/a'} | {event['severity']} | "
                f"{event['message']} |"
            )
    else:
        lines.append("No risk events have been recorded.")

    latest_cycle = status.get("latest_cycle") or {}
    summary = latest_cycle.get("summary") or {}
    lines.extend(["", "## Orders Attempted, Submitted, Blocked", ""])
    lines.append(f"- Attempted: {latest_cycle.get('orders_attempted', 0)}")
    lines.append(f"- Submitted demo orders: {latest_cycle.get('orders_submitted', 0)}")
    lines.append(f"- Blocked orders: {latest_cycle.get('orders_blocked', 0)}")
    lines.append(f"- Open demo orders counted: {status['open_demo_orders']}")
    lines.append(f"- Daily demo orders counted: {status['daily_orders']}")
    lines.extend(_order_lines("Dry-run orders", summary.get("dry_run_orders")))
    lines.extend(_order_lines("Submitted orders", summary.get("submitted_orders")))
    lines.extend(_order_lines("Blocked orders", summary.get("blocked_orders")))

    lines.extend(
        [
            "",
            "## Stop Reasons",
            "",
            f"- Latest run: {(latest_run or {}).get('stop_reason') or 'n/a'}",
            f"- Latest cycle: {latest_cycle.get('stop_reason') or 'n/a'}",
            "",
            "## Recommended Next Action",
            "",
            status["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)


def recommended_next_action(settings: Settings, *, latest_run_status: str | None) -> str:
    if not settings.autopilot_enabled:
        return "Keep autopilot disabled unless you are intentionally testing a dry-run cycle."
    if settings.kalshi_env.lower() != "demo":
        return "Set `KALSHI_ENV=demo` before running autopilot."
    if settings.autopilot_dry_run:
        return "Run `kalshi-bot autopilot-once` and inspect the dry-run report."
    if latest_run_status == "BLOCKED":
        return "Review recent risk events before changing any limits."
    return "Monitor demo-only cycles and keep production live trading out of scope."


def plain_autopilot_status(settings: Settings, *, latest_cycle_status: str | None) -> str:
    if not settings.autopilot_enabled:
        return "Autopilot is OFF"
    if settings.autopilot_dry_run:
        return "Autopilot is ON but DRY RUN only"
    if latest_cycle_status == "BLOCKED":
        return "Autopilot is blocked"
    return "Autopilot is ON for demo-only review"


def blocked_reason(latest_cycle: Any | None, risk_events: list[Any]) -> str:
    if latest_cycle is not None and latest_cycle.stop_reason:
        return str(latest_cycle.stop_reason)
    if risk_events:
        return str(risk_events[0].message)
    return "No current guardrail block has been recorded."


def top_guardrail_name(risk_events: list[Any]) -> str:
    if not risk_events:
        return "none"
    counts: dict[str, int] = {}
    for event in risk_events:
        counts[event.guardrail_name] = counts.get(event.guardrail_name, 0) + 1
    return max(counts, key=counts.get)


def autopilot_checklist(
    settings: Settings,
    *,
    freshness: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    fresh_age = to_decimal((freshness or {}).get("age_minutes"))
    fresh_data_passed = (
        fresh_age is not None and fresh_age <= settings.autopilot_require_fresh_data_minutes
    )
    return [
        {
            "label": "Demo environment",
            "passed": settings.kalshi_env.lower() == "demo",
            "detail": f"KALSHI_ENV={settings.kalshi_env}",
        },
        {
            "label": "Execution enabled",
            "passed": settings.execution_enabled or settings.autopilot_dry_run,
            "detail": "Only required when autopilot dry-run is off.",
        },
        {
            "label": "Dry run status",
            "passed": settings.autopilot_dry_run,
            "detail": f"AUTOPILOT_DRY_RUN={settings.autopilot_dry_run}",
        },
        {
            "label": "Fresh data",
            "passed": fresh_data_passed,
            "detail": (
                f"Latest age {(freshness or {}).get('age_minutes') or 'n/a'} minutes; "
                f"limit is {settings.autopilot_require_fresh_data_minutes}."
            ),
        },
        {
            "label": "Risk limits",
            "passed": settings.autopilot_max_daily_orders > 0
            and settings.autopilot_max_orders_per_cycle > 0,
            "detail": (
                f"{settings.autopilot_max_orders_per_cycle} per cycle, "
                f"{settings.autopilot_max_daily_orders} daily."
            ),
        },
        {
            "label": "Model allowed",
            "passed": bool(settings.autopilot_model),
            "detail": settings.autopilot_model,
        },
        {
            "label": "Kill switch",
            "passed": not settings.execution_kill_switch,
            "detail": f"EXECUTION_KILL_SWITCH={settings.execution_kill_switch}",
        },
    ]


def _run_row(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "started_at": row.started_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "status": row.status,
        "model_name": row.model_name,
        "dry_run": row.dry_run,
        "max_cycles": row.max_cycles,
        "cycles_completed": row.cycles_completed,
        "orders_attempted": row.orders_attempted,
        "orders_submitted": row.orders_submitted,
        "orders_blocked": row.orders_blocked,
        "stop_reason": row.stop_reason,
        "summary": decode_summary(row.summary_json),
    }


def _cycle_row(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "autopilot_run_id": row.autopilot_run_id,
        "cycle_number": row.cycle_number,
        "started_at": row.started_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "status": row.status,
        "model_name": row.model_name,
        "opportunities_scanned": row.opportunities_scanned,
        "orders_attempted": row.orders_attempted,
        "orders_submitted": row.orders_submitted,
        "orders_blocked": row.orders_blocked,
        "stop_reason": row.stop_reason,
        "summary": decode_summary(row.summary_json),
    }


def _risk_event_row(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat(),
        "event_type": row.event_type,
        "severity": row.severity,
        "ticker": row.ticker,
        "model_name": row.model_name,
        "guardrail_name": row.guardrail_name,
        "message": row.message,
        "autopilot_run_id": row.autopilot_run_id,
        "autopilot_cycle_id": row.autopilot_cycle_id,
    }


def _order_lines(title: str, orders: Any) -> list[str]:
    if not isinstance(orders, list) or not orders:
        return [f"- {title}: none"]
    lines = [f"- {title}:"]
    for order in orders[:5]:
        if not isinstance(order, dict):
            continue
        lines.append(
            "  - "
            f"{order.get('ticker')} {order.get('side')} "
            f"@ {order.get('price')} via {order.get('model_name')}"
        )
    return lines
