from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.backtesting.metrics import calculate_backtest_metrics
from kalshi_predictor.config import get_settings
from kalshi_predictor.crypto.backtest import run_crypto_model_backtest
from kalshi_predictor.crypto.repository import (
    get_crypto_links,
    get_latest_crypto_features,
    get_latest_crypto_price,
)
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.utils.time import utc_now


def generate_crypto_report(
    session: Session,
    *,
    symbols: list[str],
    output_path: str | Path,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_crypto_report(session, symbols), encoding="utf-8")
    return output


def generate_crypto_backtest_report(
    session: Session,
    *,
    days: int,
    output_path: str | Path,
) -> Path:
    crypto_v2 = run_crypto_model_backtest(session, model_name="crypto_v2", days=days)
    market_implied = run_crypto_model_backtest(
        session,
        model_name="market_implied_v1",
        days=days,
    )
    crypto_metrics = calculate_backtest_metrics(crypto_v2["trades"])
    implied_metrics = calculate_backtest_metrics(market_implied["trades"])
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        _render_crypto_backtest(crypto_v2, market_implied, crypto_metrics, implied_metrics, days),
        encoding="utf-8",
    )
    return output


def _render_crypto_report(session: Session, symbols: list[str]) -> str:
    settings = get_settings()
    links = get_crypto_links(session, limit=20)
    lines = [
        "# Crypto Features",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "",
        "## Latest Prices And Features",
        "",
        "| Symbol | Price | Momentum | Trend | History | Windows Available | Forecast readiness |",
        "|---|---:|---:|---|---:|---|---|",
    ]
    for symbol in symbols:
        price = get_latest_crypto_price(session, symbol)
        features = get_latest_crypto_features(session, symbol)
        lines.append(
            "| "
            f"{symbol} | "
            f"{price.price_usd if price else 'n/a'} | "
            f"{features.momentum_score if features else 'n/a'} | "
            f"{features.trend_direction if features else 'UNKNOWN'} | "
            f"{_history_minutes(features)} | "
            f"{_available_windows(features)} | "
            f"{_forecast_readiness(price, features, settings.crypto_v2_min_history_minutes)} |"
        )
    lines.extend(
        [
            "",
            "## Latest Linked Markets",
            "",
            "| Ticker | Symbol | Confidence | Reason |",
            "|---|---|---:|---|",
        ]
    )
    if not links:
        lines.append("| _No linked crypto markets_ |  |  |  |")
    else:
        for link in links:
            lines.append(
                f"| {link.ticker} | {link.symbol} | {link.confidence} | {link.reason} |"
            )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Feature builder uses stored crypto prices only.",
            "- Forecast readiness requires a linked market and enough feature history.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_crypto_backtest(
    crypto_v2: dict[str, Any],
    market_implied: dict[str, Any],
    crypto_metrics: dict[str, Any],
    implied_metrics: dict[str, Any],
    days: int,
) -> str:
    delta = _decimal_delta(crypto_metrics["total_pnl"], implied_metrics["total_pnl"])
    lines = [
        "# Crypto Backtest",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Window: {days} days",
        f"- Crypto-linked markets: {crypto_v2['linked_market_count']}",
        "",
        "## Summary",
        "",
        "| Model | Evaluated trades | Total P&L | Brier score | Log loss |",
        "|---|---:|---:|---:|---:|",
        _summary_row("crypto_v2", crypto_metrics),
        _summary_row("market_implied_v1", implied_metrics),
        "",
        f"- P&L delta: {delta}",
        "",
        "## Notes",
        "",
        f"- crypto_v2 evaluated forecasts: {crypto_v2['evaluated_forecasts']}",
        f"- market_implied_v1 evaluated forecasts: {market_implied['evaluated_forecasts']}",
        "- If no evaluated trades appear, link markets, build features, forecast, "
        "and sync settlements.",
        "",
    ]
    return "\n".join(lines)


def _available_windows(features: Any) -> str:
    if features is None:
        return "none"
    windows = []
    for label, value in (
        ("5m", features.return_5m),
        ("15m", features.return_15m),
        ("1h", features.return_1h),
        ("4h", features.return_4h),
        ("24h", features.return_24h),
    ):
        if value is not None:
            windows.append(label)
    return ", ".join(windows) if windows else "none"


def _forecast_readiness(price: Any, features: Any, min_history_minutes: int) -> str:
    if price is None:
        return "missing price"
    if features is None:
        return "missing features"
    history_minutes = _history_minutes(features)
    if not isinstance(history_minutes, int):
        return "unknown history"
    if isinstance(history_minutes, int) and history_minutes < min_history_minutes:
        return "insufficient history"
    if features.momentum_score is None:
        return "insufficient history"
    return "ready"


def _history_minutes(features: Any) -> int | str:
    if features is None:
        return "n/a"
    raw_features = decode_json(features.raw_json)
    value = raw_features.get("history_minutes")
    if value is None:
        return "n/a"
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return "n/a"


def _summary_row(model_name: str, metrics: dict[str, Any]) -> str:
    return (
        "| "
        f"{model_name} | "
        f"{metrics['total_trades']} | "
        f"{metrics['total_pnl']} | "
        f"{_metric(metrics.get('brier_score'))} | "
        f"{_metric(metrics.get('log_loss'))} |"
    )


def _metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _decimal_delta(left: Any, right: Any) -> str:
    from decimal import Decimal

    from kalshi_predictor.utils.decimals import to_decimal

    return str((to_decimal(left) or Decimal("0")) - (to_decimal(right) or Decimal("0")))
