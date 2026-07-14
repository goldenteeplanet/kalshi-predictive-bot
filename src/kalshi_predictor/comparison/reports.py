from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.comparison.compare import StrategyComparison, compare_strategies
from kalshi_predictor.utils.time import utc_now


def generate_strategy_comparison_report(
    session: Session,
    *,
    days: int,
    output_path: str | Path,
) -> Path:
    comparison = compare_strategies(session, days=days)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_report(comparison), encoding="utf-8")
    return output


def _render_report(comparison: StrategyComparison) -> str:
    lines = [
        "# Strategy Comparison",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Window: {comparison.days} days",
        "",
        "| Model | Forecasts | Evaluated | Trades | Win rate | Total P&L | "
        "ROI | Brier | Log loss | Notes |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in comparison.rows:
        lines.append(_row(row))
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Comparison uses only locally stored data.",
            "- Missing models are reported instead of failing.",
            "- Simulated trades use the Phase 2 paper strategy assumptions.",
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
        f"{row['simulated_trades']} | "
        f"{float(row['win_rate']):.4f} | "
        f"{row['total_pnl']} | "
        f"{row['roi']} | "
        f"{_metric(row.get('brier_score'))} | "
        f"{_metric(row.get('log_loss'))} | "
        f"{row['notes']} |"
    )


def _metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)
