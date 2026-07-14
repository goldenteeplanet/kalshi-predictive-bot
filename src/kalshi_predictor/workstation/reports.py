from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.autopilot.reports import build_autopilot_status
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.leaderboard.reports import generate_leaderboard_report
from kalshi_predictor.overnight.reports import build_overnight_status
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.workstation.repository import (
    alerts_summary,
    analytics_summary,
    evaluate_alerts,
    market_monitor_rows,
    model_performance_rows,
    portfolio_summary,
    record_portfolio_state,
)


def generate_portfolio_summary_report(
    session: Session,
    *,
    output_path: Path = Path("reports/portfolio_summary.md"),
    summary: dict[str, Any] | None = None,
    record_state: bool = True,
) -> Path:
    if record_state:
        record_portfolio_state(session)
    resolved_summary = summary or portfolio_summary(session)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_portfolio_summary(resolved_summary), encoding="utf-8")
    return output_path


def render_portfolio_summary(summary: dict[str, Any]) -> str:
    lines = [
        "# Portfolio Summary",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "- Mode: PAPER / DEMO ONLY",
        "",
        "## Overview",
        "",
        f"- Portfolio value: {summary['portfolio_value']}",
        f"- Total exposure: {summary['total_exposure']}",
        f"- Open positions: {summary['open_positions']}",
        f"- Realized P&L: {summary['realized_pnl']}",
        f"- Unrealized P&L: {summary['unrealized_pnl']}",
        f"- Total P&L: {summary['total_pnl']}",
        f"- Open paper orders: {summary['open_orders']}",
        "",
        "## Positions",
        "",
        "| Ticker | Category | Size | Avg cost | Market price | Total P&L | Exposure |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    if not summary["positions"]:
        lines.append("| _No paper positions_ |  | 0 |  |  | 0 | 0 |")
    for row in summary["positions"]:
        lines.append(
            "| "
            f"{row['ticker']} | {row['category']} | {row['position_size']} | "
            f"{row['avg_cost'] or ''} | {row['market_price'] or ''} | "
            f"{row['total_pnl']} | {row['exposure']} |"
        )
    lines.append("")
    return "\n".join(lines)


def generate_daily_briefing(
    session: Session,
    *,
    output_path: Path = Path("reports/daily_briefing.md"),
    settings: Settings | None = None,
) -> Path:
    resolved_settings = settings or get_settings()
    record_portfolio_state(session)
    evaluate_alerts(session)
    portfolio = portfolio_summary(session)
    opportunities = market_monitor_rows(session, limit=10)
    models = model_performance_rows(session)
    alerts = alerts_summary(session, limit=10)
    autopilot = build_autopilot_status(session, settings=resolved_settings)
    overnight = build_overnight_status(session, settings=resolved_settings)
    try:
        generate_leaderboard_report(
            session,
            days=30,
            output_path=Path("reports/model_leaderboard.md"),
        )
    except Exception:
        pass
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_daily_briefing(
            portfolio=portfolio,
            opportunities=opportunities,
            models=models,
            alerts=alerts,
            autopilot=autopilot,
            overnight=overnight,
        ),
        encoding="utf-8",
    )
    return output_path


def render_daily_briefing(
    *,
    portfolio: dict[str, Any],
    opportunities: list[dict[str, Any]],
    models: list[dict[str, Any]],
    alerts: dict[str, Any],
    autopilot: dict[str, Any],
    overnight: dict[str, Any],
) -> str:
    lines = [
        "# Daily Briefing",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "- Mode: PAPER / DEMO ONLY",
        "",
        "## Portfolio Summary",
        "",
        f"- Total P&L: {portfolio['total_pnl']}",
        f"- Exposure: {portfolio['total_exposure']}",
        f"- Open positions: {portfolio['open_positions']}",
        f"- Open paper orders: {portfolio['open_orders']}",
        "",
        "## Top Opportunities",
        "",
        "| Ticker | Category | Score | Model | Action |",
        "|---|---|---:|---|---|",
    ]
    if not opportunities:
        lines.append("| _No ranked opportunities_ |  |  |  | Run forecasts and scans. |")
    for row in opportunities:
        lines.append(
            "| "
            f"{row['ticker']} | {row['category']} | {row['opportunity_score']} | "
            f"{row['best_model']} | {row['recommended_action']} |"
        )
    lines.extend(
        [
            "",
            "## Top Risks",
            "",
        ]
    )
    if alerts["events"]:
        for event in alerts["events"][:5]:
            lines.append(f"- {event['severity']}: {event['message']}")
    else:
        lines.append("- No alert events recorded.")
    lines.extend(
        [
            "",
            "## Model Leaderboard",
            "",
            "| Model | ROI | Win rate | Forecasts | Trades | Rank |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for row in models:
        lines.append(
            "| "
            f"{row['model_name']} | {row['roi'] or 'n/a'} | {row['win_rate'] or 'n/a'} | "
            f"{row['forecast_count']} | {row['trade_count']} | {row['rank_color']} |"
        )
    lines.extend(
        [
            "",
            "## Autopilot Summary",
            "",
            f"- {autopilot['plain_status']}",
            f"- Recommended: {autopilot['recommended_next_action']}",
            "",
            "## Overnight Summary",
            "",
            f"- {overnight['plain_status']}",
            f"- Recommended: {overnight['recommended_next_action']}",
            "",
            "## Recommended Actions",
            "",
            "- Let paper trades settle before increasing trust.",
            "- Review alerts before changing thresholds.",
            "- Compare model rows against realized paper P&L.",
            "",
        ]
    )
    return "\n".join(lines)


def generate_analytics_report(
    session: Session,
    *,
    output_path: Path = Path("reports/analytics_report.md"),
) -> Path:
    summary = analytics_summary(session)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_analytics_report(summary), encoding="utf-8")
    return output_path


def render_analytics_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Analytics Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "- Mode: PAPER / DEMO ONLY",
        "",
        "## Daily P&L",
        "",
    ]
    lines.extend(_series_lines(summary["daily_pnl"], value_key="total_pnl"))
    lines.extend(["", "## Weekly P&L", ""])
    lines.extend(_series_lines(summary["weekly_pnl"], value_key="total_pnl"))
    lines.extend(["", "## Monthly P&L", ""])
    lines.extend(_series_lines(summary["monthly_pnl"], value_key="total_pnl"))
    lines.extend(["", "## Opportunity Trend", ""])
    lines.extend(_series_lines(summary["opportunity_trend"], value_key="value"))
    lines.extend(["", "## Paper Trade Growth", ""])
    lines.extend(_series_lines(summary["paper_trade_growth"], value_key="value"))
    lines.append("")
    return "\n".join(lines)


def _series_lines(rows: list[dict[str, Any]], *, value_key: str) -> list[str]:
    if not rows:
        return ["No data yet."]
    return [f"- {row['time']}: {row.get(value_key, '0')}" for row in rows[-10:]]
