from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.consensus.repository import consensus_signal_row, recent_consensus_signals
from kalshi_predictor.data.schema import MarketOpportunity, PaperPnl
from kalshi_predictor.overnight.repository import (
    latest_overnight_cycle,
    latest_overnight_run,
    overnight_config_payload,
    recent_iteration_metrics,
    recent_overnight_cycles,
    row_to_dict,
)
from kalshi_predictor.utils.time import utc_now


def build_overnight_status(
    session: Session,
    *,
    settings: Settings | None = None,
    cycle_limit: int = 10,
    metric_limit: int = 10,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    latest_run = latest_overnight_run(session)
    latest_cycle = latest_overnight_cycle(session)
    cycles = recent_overnight_cycles(session, limit=cycle_limit)
    metrics = recent_iteration_metrics(session, limit=metric_limit)
    latest_pnl = _latest_paper_pnl(session)
    latest_opportunity_count = _latest_opportunity_count(session, resolved_settings)
    consensus_rows = [
        consensus_signal_row(signal, settings=resolved_settings)
        for signal in recent_consensus_signals(session, limit=5)
    ]
    return {
        "config": overnight_config_payload(resolved_settings),
        "latest_run": row_to_dict(latest_run),
        "latest_cycle": row_to_dict(latest_cycle),
        "recent_cycles": [row_to_dict(cycle) for cycle in cycles],
        "metrics": [row_to_dict(metric) for metric in metrics],
        "latest_paper_pnl": _paper_pnl_row(latest_pnl),
        "latest_opportunity_count": latest_opportunity_count,
        "consensus_signals": consensus_rows,
        "report_path": "reports/overnight_report.md",
        "plain_status": plain_overnight_status(
            resolved_settings,
            latest_run_status=latest_run.status if latest_run else None,
        ),
        "what_happened": what_happened(latest_run, latest_cycle),
        "what_improved": what_improved(metrics),
        "needs_attention": needs_attention(latest_cycle),
        "bot_health": bot_health(resolved_settings, latest_cycle),
        "recommended_next_action": recommended_next_action(resolved_settings, latest_cycle),
    }


def generate_overnight_report(
    session: Session,
    *,
    output_path: Path = Path("reports/overnight_report.md"),
    settings: Settings | None = None,
) -> Path:
    status = build_overnight_status(session, settings=settings, cycle_limit=20, metric_limit=20)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_overnight_report(status), encoding="utf-8")
    return output_path


def render_overnight_report(status: dict[str, Any]) -> str:
    latest_run = status.get("latest_run") or {}
    latest_cycle = status.get("latest_cycle") or {}
    latest_pnl = status.get("latest_paper_pnl") or {}
    lines = [
        "# Overnight Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "- Mode: PAPER / DEMO ONLY",
        "- Production live trading: unavailable",
        "",
        "## Current Config",
        "",
    ]
    for key, value in status["config"].items():
        lines.append(f"- `{key}`: `{value}`")

    lines.extend(
        [
            "",
            "## What Happened Overnight",
            "",
            status["what_happened"],
            "",
            "## What Improved",
            "",
            status["what_improved"],
            "",
            "## What Needs Attention",
            "",
            status["needs_attention"],
            "",
            "## Current Bot Health",
            "",
            status["bot_health"],
            "",
            "## Last Run",
            "",
            f"- Run ID: {latest_run.get('id') or 'n/a'}",
            f"- Status: {latest_run.get('status') or 'none'}",
            f"- Cycles completed: {latest_run.get('cycles_completed') or 0}",
            f"- Errors count: {latest_run.get('errors_count') or 0}",
            "",
            "## Latest Cycle",
            "",
            f"- Cycle ID: {latest_cycle.get('id') or 'n/a'}",
            f"- Status: {latest_cycle.get('status') or 'none'}",
            f"- Markets collected: {latest_cycle.get('markets_collected') or 0}",
            f"- Snapshots inserted: {latest_cycle.get('snapshots_inserted') or 0}",
            f"- Forecasts inserted: {latest_cycle.get('forecasts_inserted') or 0}",
            f"- Paper orders created: {latest_cycle.get('paper_orders_created') or 0}",
            f"- Opportunities detected: {latest_cycle.get('opportunities_detected') or 0}",
            f"- Settlements synced: {latest_cycle.get('settlements_synced') or 0}",
            f"- Reports generated: {latest_cycle.get('reports_generated') or 0}",
            "",
            "## Paper P&L Trend",
            "",
            f"- Latest ticker: {latest_pnl.get('ticker') or 'n/a'}",
            f"- Latest total P&L: {latest_pnl.get('total_pnl') or 'n/a'}",
            f"- Latest realized P&L: {latest_pnl.get('realized_pnl') or 'n/a'}",
            "",
            "## Model Iteration Metrics",
            "",
            "| Generated | Cycle | Model | Forecasts | Opportunities | Paper trades | "
            "Estimated P&L | Avg edge | Avg score | Notes |",
            "|---|---:|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    if status["metrics"]:
        for metric in status["metrics"]:
            lines.append(_metric_row(metric))
    else:
        lines.append("| _No model iteration metrics yet_ |  |  |  |  |  |  |  |  |  |")

    lines.extend(
        [
            "",
            "## Best Current Opportunities",
            "",
            f"- Latest opportunity count for `{status['config']['OVERNIGHT_MODEL']}`: "
            f"{status['latest_opportunity_count']}",
            "- See `reports/opportunities.md` for ranked opportunities.",
            "",
            "## Forum Consensus Snapshot",
            "",
        ]
    )
    if status["consensus_signals"]:
        lines.extend(
            [
                "| Ticker | Source | Winners | Win rate | Price | Signal |",
                "|---|---|---:|---:|---:|---|",
            ]
        )
        for signal in status["consensus_signals"]:
            lines.append(
                "| "
                f"{signal['ticker']} | {signal['source']} | {signal['winner_count']} | "
                f"{signal['average_win_rate'] or ''} | {signal['longshot_price'] or ''} | "
                f"{signal['assessment']} |"
            )
    else:
        lines.append("No forum consensus signals have been imported yet.")

    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            status["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)


def plain_overnight_status(settings: Settings, *, latest_run_status: str | None) -> str:
    if not settings.overnight_enabled:
        return "Overnight scheduler is OFF"
    if latest_run_status in {"RUNNING", "COMPLETED", "COMPLETED_WITH_ERRORS"}:
        return f"Overnight scheduler is {latest_run_status}"
    return "Overnight scheduler is configured for PAPER / DEMO ONLY"


def what_happened(latest_run: Any | None, latest_cycle: Any | None) -> str:
    if latest_run is None:
        return "No overnight run has been recorded yet."
    if latest_cycle is None:
        return f"Run {latest_run.id} started but has no completed cycle yet."
    return (
        f"Run {latest_run.id} completed {latest_run.cycles_completed} cycle(s). "
        f"The latest cycle collected {latest_cycle.markets_collected} markets, inserted "
        f"{latest_cycle.forecasts_inserted} forecasts, and created "
        f"{latest_cycle.paper_orders_created} paper order(s)."
    )


def what_improved(metrics: list[Any]) -> str:
    if not metrics:
        return "No model iteration metrics exist yet. The first cycle will establish a baseline."
    latest = metrics[0]
    return (
        f"The latest metric row tracks {latest.forecast_count} forecasts, "
        f"{latest.opportunity_count} opportunities, {latest.paper_trade_count} paper trades, "
        f"and estimated P&L {latest.estimated_pnl or 'n/a'}."
    )


def needs_attention(latest_cycle: Any | None) -> str:
    if latest_cycle is None:
        return "Run `kalshi-bot overnight-once` to create the first paper-learning cycle."
    errors = latest_cycle.errors_json or ""
    if latest_cycle.status == "COMPLETED_WITH_ERRORS" or errors not in {"", "[]"}:
        return "The latest cycle stored errors. Review the cycle history before relying on trends."
    if latest_cycle.paper_orders_created == 0:
        return (
            "No paper bets were created in the latest cycle; thresholds may be too strict "
            "or data sparse."
        )
    return "No urgent attention item was recorded in the latest cycle."


def bot_health(settings: Settings, latest_cycle: Any | None) -> str:
    status = latest_cycle.status if latest_cycle is not None else "no cycles yet"
    demo = "off" if not settings.overnight_run_demo else "requested but guarded"
    return (
        f"Latest cycle status is {status}. Paper betting is "
        f"{'on' if settings.overnight_run_paper else 'off'}; demo execution is {demo}."
    )


def recommended_next_action(settings: Settings, latest_cycle: Any | None) -> str:
    if latest_cycle is None:
        return "Start with `kalshi-bot overnight-once`, then inspect this report and paper P&L."
    if latest_cycle.status == "COMPLETED_WITH_ERRORS":
        return (
            "Fix or accept the stored errors, then run another overnight cycle to compare metrics."
        )
    if latest_cycle.paper_orders_created == 0:
        return (
            "Collect more snapshots or lower paper thresholds carefully before judging the model."
        )
    return (
        "Let the paper loop accumulate more settled outcomes, then compare model weights, "
        "leaderboard rows, and paper P&L before increasing trust."
    )


def _latest_paper_pnl(session: Session) -> PaperPnl | None:
    return session.scalar(
        select(PaperPnl).order_by(desc(PaperPnl.calculated_at), desc(PaperPnl.id)).limit(1)
    )


def _latest_opportunity_count(session: Session, settings: Settings) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(MarketOpportunity)
            .where(MarketOpportunity.model_name == settings.overnight_model)
        )
        or 0
    )


def _paper_pnl_row(row: PaperPnl | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "ticker": row.ticker,
        "calculated_at": row.calculated_at.isoformat(),
        "realized_pnl": row.realized_pnl,
        "unrealized_pnl": row.unrealized_pnl,
        "total_pnl": row.total_pnl,
        "notes": row.notes,
    }


def _metric_row(metric: dict[str, Any]) -> str:
    return (
        "| "
        f"{metric.get('generated_at') or ''} | "
        f"{metric.get('cycle_number') or ''} | "
        f"{metric.get('model_name') or ''} | "
        f"{metric.get('forecast_count') or 0} | "
        f"{metric.get('opportunity_count') or 0} | "
        f"{metric.get('paper_trade_count') or 0} | "
        f"{metric.get('estimated_pnl') or ''} | "
        f"{metric.get('avg_edge') or ''} | "
        f"{metric.get('avg_opportunity_score') or ''} | "
        f"{metric.get('notes') or ''} |"
    )
