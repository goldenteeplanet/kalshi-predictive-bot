from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.confidence.repository import confidence_rows_for_ui
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.lanes.repository import (
    recent_learning_opportunities,
)
from kalshi_predictor.lanes.repository import (
    row_to_dict as lane_row_to_dict,
)
from kalshi_predictor.learning.config import learning_config_payload
from kalshi_predictor.learning.diagnostics import build_learning_diagnostics
from kalshi_predictor.learning.repository import (
    recent_learning_cycles,
    recent_learning_runs,
    row_to_dict,
)
from kalshi_predictor.learning.safety import learning_status
from kalshi_predictor.learning.targets import generate_learning_targets, latest_learning_target_rows
from kalshi_predictor.utils.time import utc_now


def build_learning_dashboard(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    targets = latest_learning_target_rows(session, limit=20)
    recent_cycles = [row_to_dict(row) for row in recent_learning_cycles(session)]
    recent_opportunities = [
        lane_row_to_dict(row) or {} for row in recent_learning_opportunities(session, limit=20)
    ]
    confidence_rows = confidence_rows_for_ui(session, limit=100)
    return {
        "config": dict(learning_config_payload(resolved_settings)),
        "status": learning_status(session, settings=resolved_settings),
        "recent_runs": [row_to_dict(row) for row in recent_learning_runs(session)],
        "recent_cycles": recent_cycles,
        "targets": targets,
        "recent_learning_opportunities": recent_opportunities,
        "fast_settlement_categories": _fast_settlement_categories(targets),
        "category_breakdown": _category_breakdown(targets, recent_opportunities),
        "blocked_opportunities": _blocked_opportunities(recent_cycles),
        "trade_generation": _trade_generation_summary(recent_cycles),
        "diagnostics": build_learning_diagnostics(
            session,
            settings=resolved_settings,
            rejection_limit=50,
        ),
        "model_confidence_summary": _model_confidence_summary(confidence_rows),
        "report_path": "reports/learning_report.md",
    }


def generate_learning_report(
    session: Session,
    *,
    output_path: Path = Path("reports/learning_report.md"),
    settings: Settings | None = None,
) -> Path:
    dashboard = build_learning_dashboard(session, settings=settings)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_learning_report(dashboard), encoding="utf-8")
    return output_path


def generate_learning_targets_report(
    session: Session,
    *,
    output_path: Path = Path("reports/learning_targets.md"),
    settings: Settings | None = None,
    model_name: str = "ensemble_v2",
    limit: int = 100,
    refresh: bool = True,
) -> Path:
    resolved_settings = settings or get_settings()
    if refresh:
        generate_learning_targets(
            session,
            settings=resolved_settings,
            model_name=model_name,
            limit=limit,
            persist=True,
        )
    rows = latest_learning_target_rows(session, limit=limit)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_learning_targets_report(rows), encoding="utf-8")
    return output_path


def render_learning_report(dashboard: dict[str, Any]) -> str:
    status = dashboard["status"]
    lines = [
        "# Learning Mode Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "- Mode: PAPER ONLY",
        f"- Status: {status['plain_status']}",
        f"- Settled paper trades: {status['settled_paper_trades']} / "
        f"{status['target_settled_trades']}",
        f"- Progress: {status['progress_percent']}",
        f"- Daily paper trades: {status['daily_paper_trades']} / "
        f"{status['daily_paper_trade_cap']}",
        f"- Forecasts Evaluated: {status['forecasts_evaluated']}",
        f"- Trade generation health: {status['trade_generation_health']['label']}",
        f"- Expected completion: {status['expected_completion']}",
        "",
        "## Trade Generation",
        "",
        f"- Opportunities Evaluated: {dashboard['trade_generation']['opportunities_evaluated']}",
        f"- Trades Created: {dashboard['trade_generation']['trades_created']}",
        f"- Trades Blocked: {dashboard['trade_generation']['trades_blocked']}",
        "",
        "## Safety",
        "",
        f"- Demo execution blocked: {status['demo_execution_blocked']}",
        f"- Live execution blocked: {status['live_execution_blocked']}",
        "- Live trading: not added",
        "",
        "## Category Breakdown",
        "",
        "| Category | Targets | Opportunities |",
        "|---|---:|---:|",
    ]
    if dashboard["category_breakdown"]:
        for row in dashboard["category_breakdown"]:
            lines.append(
                f"| {row['category']} | {row['targets']} | {row['opportunities']} |"
            )
    else:
        lines.append("| _No category data yet_ |  |  |")
    lines.extend(
        [
            "",
            "## Model Confidence Summary",
            "",
            "| Label | Models |",
            "|---|---:|",
        ]
    )
    if dashboard["model_confidence_summary"]["label_counts"]:
        for row in dashboard["model_confidence_summary"]["label_counts"]:
            lines.append(f"| {row['label']} | {row['count']} |")
    else:
        lines.append("| _No model confidence rows yet_ |  |")
    lines.extend(
        [
            "",
            "## Recent Cycles",
            "",
            "| Started | Status | Markets | Forecasts | Opportunities | Paper orders | "
            "Settled total |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    if dashboard["recent_cycles"]:
        for cycle in dashboard["recent_cycles"]:
            lines.append(
                "| "
                f"{cycle.get('started_at') or ''} | {cycle.get('status') or ''} | "
                f"{cycle.get('markets_scanned') or 0} | "
                f"{cycle.get('forecasts_generated') or 0} | "
                f"{cycle.get('opportunities_found') or 0} | "
                f"{cycle.get('paper_trades_created') or 0} | "
                f"{cycle.get('settled_paper_trades_total') or 0} |"
            )
    else:
        lines.append("| _No learning cycles yet_ |  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Block Reasons",
            "",
            "| Reason | Count | Action |",
            "|---|---:|---|",
        ]
    )
    if dashboard["blocked_opportunities"]:
        for row in dashboard["blocked_opportunities"]:
            lines.append(f"| {row['reason']} | {row['count']} | {row['action']} |")
    else:
        lines.append("| _No recent learning blockers_ |  |  |")
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


def render_learning_targets_report(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Learning Targets",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "- Goal: prioritize paper bets that can settle quickly and improve feedback loops.",
        "",
        "| Ticker | Model | Category | Speed | Priority | Reason |",
        "|---|---|---|---:|---:|---|",
    ]
    if not rows:
        lines.append("| _No learning targets yet_ |  |  |  |  |  |")
    for row in rows:
        lines.append(
            "| "
            f"{row['ticker']} | {row['model_name']} | {row['category']} | "
            f"{row['settlement_speed_score']} | {row['learning_priority_score']} | "
            f"{row['reason']} |"
        )
    lines.append("")
    return "\n".join(lines)


def _fast_settlement_categories(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    categories: dict[str, dict[str, Any]] = {}
    for target in targets:
        category = str(target.get("category") or "unknown")
        current = categories.get(category)
        speed = target.get("settlement_speed_score") or "0"
        if current is None or str(speed) > str(current["best_speed"]):
            categories[category] = {"category": category, "best_speed": speed}
    return sorted(categories.values(), key=lambda row: str(row["best_speed"]), reverse=True)


def _category_breakdown(
    targets: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for target in targets:
        category = str(target.get("category") or "unknown")
        rows.setdefault(category, {"category": category, "targets": 0, "opportunities": 0})
        rows[category]["targets"] += 1
    for opportunity in opportunities:
        raw = opportunity.get("raw_json") or {}
        category = str(raw.get("category") or "unknown")
        rows.setdefault(category, {"category": category, "targets": 0, "opportunities": 0})
        rows[category]["opportunities"] += 1
    return sorted(rows.values(), key=lambda row: (-row["targets"], row["category"]))


def _blocked_opportunities(cycles: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
    totals = {
        "Below learning edge": 0,
        "Risk or position cap": 0,
        "Duplicate forecast": 0,
    }
    for cycle in cycles[:5]:
        summary = (cycle or {}).get("summary_json") or {}
        paper_run = ((summary.get("steps") or {}).get("paper_run") or {})
        totals["Below learning edge"] += int(paper_run.get("skipped_due_to_edge") or 0)
        totals["Risk or position cap"] += int(paper_run.get("skipped_due_to_risk_limits") or 0)
        totals["Duplicate forecast"] += int(paper_run.get("duplicates_skipped") or 0)
    actions = {
        "Below learning edge": "Collect fresh forecasts or review model calibration.",
        "Risk or position cap": "Wait for fills/settlements or raise paper-only caps carefully.",
        "Duplicate forecast": "Already paper-bet this forecast; wait for new forecasts.",
    }
    return [
        {"reason": reason, "count": count, "action": actions[reason]}
        for reason, count in totals.items()
        if count
    ]


def _trade_generation_summary(cycles: list[dict[str, Any] | None]) -> dict[str, int]:
    opportunities = 0
    trades = 0
    blocked = 0
    for cycle in cycles[:5]:
        summary = (cycle or {}).get("summary_json") or {}
        steps = summary.get("steps") or {}
        find_step = steps.get("find_opportunities") or {}
        paper_step = steps.get("paper_run") or {}
        opportunities += int(find_step.get("opportunities_detected") or 0)
        trades += int(paper_step.get("orders_created") or 0)
        blocked += int(paper_step.get("skipped_due_to_edge") or 0)
        blocked += int(paper_step.get("skipped_due_to_risk_limits") or 0)
        blocked += int(paper_step.get("duplicates_skipped") or 0)
    return {
        "opportunities_evaluated": opportunities,
        "trades_created": trades,
        "trades_blocked": blocked,
    }


def _model_confidence_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    label_counts: dict[str, int] = {}
    leaders: list[dict[str, Any]] = []
    for row in rows:
        label = str(row.get("confidence_label") or "Needs More Data")
        label_counts[label] = label_counts.get(label, 0) + 1
        if label == "Leader":
            leaders.append(row)
    return {
        "label_counts": [
            {"label": label, "count": count}
            for label, count in sorted(label_counts.items(), key=lambda item: item[0])
        ],
        "leaders": leaders[:10],
    }
