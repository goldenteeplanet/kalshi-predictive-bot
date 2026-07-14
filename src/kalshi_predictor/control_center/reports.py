from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.autopilot.reports import build_autopilot_status
from kalshi_predictor.confidence.repository import confidence_rows_for_ui
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.lanes.repository import (
    latest_autopilot_metric,
    latest_learning_metric,
    row_to_dict,
)
from kalshi_predictor.learning.safety import learning_status
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


def build_control_center(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    learning = learning_status(session, settings=resolved_settings)
    autopilot = build_autopilot_status(session, settings=resolved_settings)
    confidence_rows = confidence_rows_for_ui(session)
    learning_metric = row_to_dict(latest_learning_metric(session)) or {}
    autopilot_metric = row_to_dict(latest_autopilot_metric(session)) or {}
    category_leaders = _category_leaders(confidence_rows)
    top_model = _top_model(confidence_rows)
    return {
        "generated_at": utc_now().isoformat(),
        "learning": learning,
        "autopilot": autopilot,
        "model_confidence_rows": confidence_rows,
        "category_leaders": category_leaders,
        "top_model": top_model,
        "learning_metric": learning_metric,
        "autopilot_metric": autopilot_metric,
        "lane_cards": {
            "learning": _learning_lane_card(learning, learning_metric),
            "autopilot": _autopilot_lane_card(autopilot, autopilot_metric, top_model),
        },
        "recommended_next_action": _recommended_next_action(
            learning=learning,
            autopilot=autopilot,
            confidence_rows=confidence_rows,
        ),
    }


def generate_control_center_report(
    session: Session,
    *,
    output_path: Path = Path("reports/control_center.md"),
    settings: Settings | None = None,
) -> Path:
    context = build_control_center(session, settings=settings)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_control_center_report(context), encoding="utf-8")
    return output_path


def render_control_center_report(context: dict[str, Any]) -> str:
    learning = context["learning"]
    autopilot = context["autopilot"]
    lines = [
        "# Control Center Report",
        "",
        f"- Generated at: {context['generated_at']}",
        "- Mode: PAPER / DEMO ONLY",
        "- Production live trading: unavailable",
        "",
        "## Learning Progress",
        "",
        f"- Status: {learning['plain_status']}",
        f"- Settled paper trades: {learning['settled_paper_trades']} / "
        f"{learning['target_settled_trades']}",
        f"- Progress to 500: {learning['progress_percent']}",
        f"- Trades created today: {learning['daily_paper_trades']}",
        f"- Learning confidence: {context['lane_cards']['learning']['learning_confidence']}",
        "",
        "## Autopilot Summary",
        "",
        f"- Status: {autopilot['plain_status']}",
        f"- Opportunities found: {context['lane_cards']['autopilot']['opportunities_found']}",
        f"- Dry-run orders: {context['lane_cards']['autopilot']['dry_run_orders']}",
        f"- Top model: {context['lane_cards']['autopilot']['top_model']}",
        f"- Current confidence: {context['lane_cards']['autopilot']['current_confidence']}",
        "",
        "## Model Confidence Summary",
        "",
        "| Category | Leader | Confidence | Settled trades |",
        "|---|---|---:|---:|",
    ]
    if context["category_leaders"]:
        for leader in context["category_leaders"]:
            lines.append(
                "| "
                f"{leader['category']} | {leader['model_name']} | "
                f"{leader['confidence_score']} | {leader['settled_trade_count']} |"
            )
    else:
        lines.append("| _No category leaders yet_ |  |  |  |")
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            context["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)


def _learning_lane_card(
    learning: dict[str, Any],
    metric: dict[str, Any],
) -> dict[str, Any]:
    return {
        "settled_paper_trades": learning["settled_paper_trades"],
        "progress_to_500": learning["progress_percent"],
        "trades_created_today": learning["daily_paper_trades"],
        "learning_confidence": metric.get("learning_confidence") or learning["progress_percent"],
    }


def _autopilot_lane_card(
    autopilot: dict[str, Any],
    metric: dict[str, Any],
    top_model: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "opportunities_found": metric.get("opportunities_found")
        or (autopilot.get("latest_cycle") or {}).get("opportunities_scanned")
        or 0,
        "dry_run_orders": metric.get("dry_run_orders")
        or len(
            (autopilot.get("latest_cycle") or {})
            .get("summary", {})
            .get("dry_run_orders")
            or []
        ),
        "top_model": (top_model or {}).get("model_name") or "Needs more data",
        "current_confidence": metric.get("current_confidence")
        or (top_model or {}).get("confidence_score")
        or "n/a",
    }


def _category_leaders(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    leaders: dict[str, dict[str, Any]] = {}
    for row in rows:
        category = str(row.get("category") or "general")
        current = leaders.get(category)
        if current is None or _confidence(row) > _confidence(current):
            leaders[category] = row
    return sorted(leaders.values(), key=lambda row: str(row.get("category") or ""))


def _top_model(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=_confidence)


def _confidence(row: dict[str, Any]) -> Any:
    return to_decimal(row.get("confidence_score")) or 0


def _recommended_next_action(
    *,
    learning: dict[str, Any],
    autopilot: dict[str, Any],
    confidence_rows: list[dict[str, Any]],
) -> str:
    if learning["settled_paper_trades"] < learning["target_settled_trades"]:
        return "Run accelerate-learning to grow settled paper-trade sample size."
    if not confidence_rows:
        return "Run model-confidence after more trades settle."
    if autopilot["plain_status"] == "Autopilot is OFF":
        return "Keep Autopilot dry-run only until confidence leaders are stable."
    return "Review Control Center metrics before changing thresholds."
