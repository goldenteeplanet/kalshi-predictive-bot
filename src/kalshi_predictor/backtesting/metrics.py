from decimal import Decimal
from typing import Any

from kalshi_predictor.evaluation.metrics import brier_score, log_loss
from kalshi_predictor.utils.decimals import to_decimal


def calculate_backtest_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    total_trades = len(trades)
    wins = sum(1 for trade in trades if (to_decimal(trade["pnl"]) or Decimal("0")) > 0)
    losses = sum(1 for trade in trades if (to_decimal(trade["pnl"]) or Decimal("0")) < 0)
    total_pnl = sum(
        ((to_decimal(trade["pnl"]) or Decimal("0")) for trade in trades),
        Decimal("0"),
    )
    total_edge = sum(
        ((to_decimal(trade["edge"]) or Decimal("0")) for trade in trades),
        Decimal("0"),
    )
    total_exposure = sum(
        ((to_decimal(trade["exposure"]) or Decimal("0")) for trade in trades),
        Decimal("0"),
    )
    avg_edge = total_edge / total_trades if total_trades else Decimal("0")
    avg_pnl = total_pnl / total_trades if total_trades else Decimal("0")
    roi = total_pnl / total_exposure if total_exposure else Decimal("0")
    y_true = [int(trade["y_true"]) for trade in trades]
    y_prob = [float(trade["yes_probability"]) for trade in trades]

    return {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / total_trades if total_trades else 0.0,
        "average_edge": str(avg_edge),
        "average_pnl": str(avg_pnl),
        "total_pnl": str(total_pnl),
        "max_drawdown": str(_max_drawdown(trades)),
        "roi_on_exposure": str(roi),
        "brier_score": brier_score(y_true, y_prob) if y_true else None,
        "log_loss": log_loss(y_true, y_prob) if y_true else None,
        "total_exposure": str(total_exposure),
    }


def _max_drawdown(trades: list[dict[str, Any]]) -> Decimal:
    peak = Decimal("0")
    cumulative = Decimal("0")
    max_drawdown = Decimal("0")
    for trade in trades:
        cumulative += to_decimal(trade["pnl"]) or Decimal("0")
        if cumulative > peak:
            peak = cumulative
        drawdown = peak - cumulative
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    return max_drawdown

