from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.signals.repository import signal_explorer_rows, signal_leaderboard_rows
from kalshi_predictor.signals.scoring import refresh_signal_performance
from kalshi_predictor.signals.status import signal_status_rows
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


def generate_signal_report(session: Session, *, output_path: str | Path) -> Path:
    refresh_signal_performance(session)
    rows = signal_leaderboard_rows(session)
    explorer = signal_explorer_rows(session)
    readiness = signal_status_rows(session)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_signal_report(rows, explorer, readiness), encoding="utf-8")
    return output


def _render_signal_report(
    rows: list[dict[str, Any]],
    explorer: list[dict[str, Any]],
    readiness: list[dict[str, Any]],
) -> str:
    highest_roi = sorted(rows, key=lambda row: to_decimal(row["roi"]) or -999, reverse=True)
    worst = sorted(rows, key=lambda row: to_decimal(row["roi"]) or -999)
    most_active = sorted(
        rows,
        key=lambda row: row["forecast_count"] + row["trade_count"],
        reverse=True,
    )
    calibrated = sorted(
        [row for row in rows if row["brier_score"] is not None],
        key=lambda row: to_decimal(row["brier_score"]) or 999,
    )
    needs_data = [
        row
        for row in rows
        if row["missing_data"] != "none" or row["status"] == "Insufficient Data"
    ]
    active = [row for row in readiness if row["readiness_status"] == "ACTIVE"]
    inactive = [row for row in readiness if row["readiness_status"] != "ACTIVE"]
    lines = [
        "# Signal Marketplace Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "- Mode: PAPER / DEMO ONLY",
        f"- Active expected signals: {len(active)}",
        f"- Inactive expected signals: {len(inactive)}",
        "",
        "## Top Signals",
        "",
        *_table(rows[:10]),
        "",
        "## Worst Signals",
        "",
        *_table(worst[:10]),
        "",
        "## Most Active Signals",
        "",
        *_table(most_active[:10]),
        "",
        "## Highest ROI Signals",
        "",
        *_table(highest_roi[:10]),
        "",
        "## Best Calibrated Signals",
        "",
        *_table(calibrated[:10]),
        "",
        "## Signals Needing More Data",
        "",
        *_readiness_table(needs_data[:15]),
        "",
        "## Active Signals",
        "",
        *_readiness_table(active),
        "",
        "## Inactive Signals",
        "",
        *_readiness_table(inactive),
        "",
        "## Missing Data",
        "",
        *_missing_data_table([row for row in readiness if row["missing_data"] != "none"]),
        "",
        "## Recommended Next Actions",
        "",
        *_next_actions(inactive),
        "",
        "## Explorer",
        "",
        "| Signal | Category | Models | Current Activity | Status |",
        "|---|---|---|---:|---|",
    ]
    if not explorer:
        lines.append("| _No signals_ |  |  |  |  |")
    for row in explorer:
        lines.append(
            "| "
            f"{row['signal_name']} | "
            f"{row['category']} | "
            f"{', '.join(row['associated_models'])} | "
            f"{row['current_activity']} | "
            f"{row['status']} |"
        )
    lines.extend(
        [
            "",
            "## Reminder",
            "",
            "Signal attribution is local and diagnostic. It does not place live trades.",
            "",
        ]
    )
    return "\n".join(lines)


def _table(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Rank | Signal | ROI | Win Rate | Forecasts | Trades | Confidence | "
        "Status | Missing Data |",
        "|---:|---|---:|---:|---:|---:|---:|---|---|",
    ]
    if not rows:
        lines.append("| _No signal data_ |  |  |  |  |  |  |  |  |")
        return lines
    for row in rows:
        lines.append(
            "| "
            f"{row.get('rank', '')} | "
            f"{row['signal_name']} | "
            f"{row['roi'] or 'n/a'} | "
            f"{row['win_rate'] or 'n/a'} | "
            f"{row['forecast_count']} | "
            f"{row['trade_count']} | "
            f"{row['confidence_score'] or 'n/a'} | "
            f"{row['status']} | "
            f"{row.get('missing_data', 'none')} |"
        )
    return lines


def _readiness_table(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Signal | Status | Forecasts | Trades | Latest Signal | Missing Data | Next Action |",
        "|---|---|---:|---:|---|---|---|",
    ]
    if not rows:
        lines.append("| _No signals_ |  |  |  |  |  |  |")
        return lines
    for row in rows:
        lines.append(
            "| "
            f"{row['signal_name']} | "
            f"{row['status_label']} | "
            f"{row['forecast_count']} | "
            f"{row['trade_count']} | "
            f"{row['latest_signal']} | "
            f"{row['missing_data']} | "
            f"{row['next_action']} |"
        )
    return lines


def _missing_data_table(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Signal | Missing Data | Skip Reason | Next Action |",
        "|---|---|---|---|",
    ]
    if not rows:
        lines.append("| _No missing data_ |  |  |  |")
        return lines
    for row in rows:
        lines.append(
            "| "
            f"{row['signal_name']} | "
            f"{row['missing_data']} | "
            f"{row['skip_reason']} | "
            f"{row['next_action']} |"
        )
    return lines


def _next_actions(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- No inactive expected signals."]
    actions = []
    seen: set[str] = set()
    for row in rows:
        action = row["next_action"]
        if action in seen:
            continue
        seen.add(action)
        actions.append(f"- {action}")
    return actions
