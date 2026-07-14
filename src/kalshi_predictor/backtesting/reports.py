from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.backtesting.engine import BacktestResult, run_backtest
from kalshi_predictor.utils.time import utc_now


def generate_backtest_report(
    session: Session,
    *,
    model_name: str,
    strategy_name: str,
    days: int,
    output_path: str | Path,
) -> Path:
    result = run_backtest(
        session,
        model_name=model_name,
        strategy_name=strategy_name,
        days=days,
        persist=True,
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_report(result, days=days), encoding="utf-8")
    return output


def _render_report(result: BacktestResult, *, days: int) -> str:
    summary = result.summary
    lines = [
        f"# Backtest: {result.model_name}",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Strategy: {result.strategy_name}",
        f"- Window: {days} days",
        f"- Forecasts scanned: {result.forecasts_scanned}",
        f"- Evaluated forecasts: {result.evaluated_forecasts}",
        f"- Simulated trades: {summary['total_trades']}",
        f"- Wins: {summary['wins']}",
        f"- Losses: {summary['losses']}",
        f"- Win rate: {summary['win_rate']:.4f}",
        f"- Average edge: {summary['average_edge']}",
        f"- Average P&L: {summary['average_pnl']}",
        f"- Total P&L: {summary['total_pnl']}",
        f"- Max drawdown: {summary['max_drawdown']}",
        f"- ROI on exposure: {summary['roi_on_exposure']}",
        f"- Brier score: {_metric(summary.get('brier_score'))}",
        f"- Log loss: {_metric(summary.get('log_loss'))}",
        "",
        "## Recent Simulated Trades",
        "",
        "| Simulated at | Ticker | Side | Price | Qty | Edge | Result | P&L |",
        "|---|---|---|---:|---:|---:|---|---:|",
    ]
    if not result.trades:
        lines.append("| _No evaluated trades_ |  |  |  | 0 |  |  | 0 |")
    else:
        for trade in result.trades[-20:]:
            lines.append(_trade_row(trade))
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- This backtest uses only locally stored forecasts, snapshots, and settlements.",
            "- No live Kalshi API calls, authentication, or real orders are used.",
            "- Immediate paper fills are a simplifying assumption.",
            "",
        ]
    )
    return "\n".join(lines)


def _trade_row(trade: dict[str, Any]) -> str:
    return (
        "| "
        f"{trade['simulated_at']} | "
        f"{trade['ticker']} | "
        f"{trade['side']} | "
        f"{trade['price']} | "
        f"{trade['quantity']} | "
        f"{trade['edge']} | "
        f"{trade['settlement_result']} | "
        f"{trade['pnl']} |"
    )


def _metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)

