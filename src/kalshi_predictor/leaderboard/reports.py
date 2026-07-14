from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.leaderboard.builder import (
    LeaderboardResult,
    build_model_leaderboard,
    display_decimal,
)
from kalshi_predictor.utils.time import utc_now


def generate_leaderboard_report(
    session: Session,
    *,
    days: int,
    output_path: str | Path,
) -> tuple[Path, LeaderboardResult]:
    result = build_model_leaderboard(session, days=days, persist=True)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_report(result), encoding="utf-8")
    return output, result


def _render_report(result: LeaderboardResult) -> str:
    rows = result.rows
    best_calibration = _best_model(rows, "brier_score", lower_is_better=True)
    best_pnl = _best_model(rows, "total_pnl", lower_is_better=False)
    insufficient = [row["model_name"] for row in rows if row["forecast_count"] == 0]
    lines = [
        "# Model Leaderboard",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Date window: last {result.days} days",
        "",
        "## Summary",
        "",
        f"- Models compared: {len(rows)}",
        f"- Best model by calibration: {best_calibration or 'n/a'}",
        f"- Best model by P&L: {best_pnl or 'n/a'}",
        "",
        "## Leaderboard Table",
        "",
        "| Model | Forecasts | Evaluated | Paper trades | Settled trades | Brier | "
        "Log loss | Win rate | Total P&L | ROI | Avg edge | Max DD | Tournament rank | "
        "Category winner | Notes |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:
        lines.append(_row(row))
    lines.extend(
        [
            "",
            "## Best Model By Calibration",
            "",
            best_calibration or "No model has evaluated calibration data yet.",
            "",
            "## Best Model By P&L",
            "",
            best_pnl or "No model has settled trade P&L data yet.",
            "",
            "## Models With Insufficient Data",
            "",
            ", ".join(insufficient) if insufficient else "All models have at least one forecast.",
            "",
            "## Recommendations",
            "",
            "Collect more snapshots, run forecasts for all models, sync settlements, and rerun "
            "backtests before using leaderboard results for strategy decisions.",
            "",
        ]
    )
    return "\n".join(lines)


def _row(row: dict[str, Any]) -> str:
    return (
        "| "
        f"{row['model_name']} | "
        f"{row['forecast_count']} | "
        f"{row['evaluated_forecast_count']} | "
        f"{row['paper_trade_count']} | "
        f"{row['settled_trade_count']} | "
        f"{display_decimal(row['brier_score'])} | "
        f"{display_decimal(row['log_loss'])} | "
        f"{display_decimal(row['win_rate'])} | "
        f"{display_decimal(row['total_pnl'])} | "
        f"{display_decimal(row['roi_on_exposure'])} | "
        f"{display_decimal(row['avg_edge'])} | "
        f"{display_decimal(row['max_drawdown'])} | "
        f"{row.get('tournament_rank') or 'n/a'} | "
        f"{'yes' if row.get('tournament_category_winner') else 'no'} | "
        f"{row['notes']} |"
    )


def _best_model(
    rows: list[dict[str, Any]],
    metric: str,
    *,
    lower_is_better: bool,
) -> str | None:
    usable = [row for row in rows if row.get(metric) is not None]
    if not usable:
        return None
    selected = sorted(usable, key=lambda row: row[metric], reverse=not lower_is_better)[0]
    return str(selected["model_name"])
