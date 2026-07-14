from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.backtesting.metrics import calculate_backtest_metrics
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.backtest import run_weather_model_backtest
from kalshi_predictor.weather.repository import (
    get_latest_weather_forecasts,
    get_weather_features,
    get_weather_links,
    normalize_location_key,
)


def generate_weather_report(
    session: Session,
    *,
    location_key: str,
    output_path: str | Path,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        _render_weather_report(session, normalize_location_key(location_key)),
        encoding="utf-8",
    )
    return output


def generate_weather_backtest_report(
    session: Session,
    *,
    days: int,
    output_path: str | Path,
) -> Path:
    weather_v2 = run_weather_model_backtest(session, model_name="weather_v2", days=days)
    market_implied = run_weather_model_backtest(
        session,
        model_name="market_implied_v1",
        days=days,
    )
    weather_metrics = calculate_backtest_metrics(weather_v2["trades"])
    implied_metrics = calculate_backtest_metrics(market_implied["trades"])
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        _render_weather_backtest(
            weather_v2,
            market_implied,
            weather_metrics,
            implied_metrics,
            days,
        ),
        encoding="utf-8",
    )
    return output


def _render_weather_report(session: Session, location_key: str) -> str:
    forecasts = get_latest_weather_forecasts(session, location_key, limit=8)
    features = get_weather_features(session, location_key, limit=8)
    links = [
        link for link in get_weather_links(session, limit=50) if link.location_key == location_key
    ]
    lines = [
        "# Weather Features",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Location: {location_key}",
        "",
        "## Latest Forecasts",
        "",
        "| Target time | Temp | Rain % | Wind | Gust | Summary |",
        "|---|---:|---:|---:|---:|---|",
    ]
    if not forecasts:
        lines.append("| _No stored forecasts_ |  |  |  |  |  |")
    else:
        for forecast in forecasts:
            lines.append(
                "| "
                f"{forecast.forecast_time.isoformat()} | "
                f"{forecast.temperature_f or 'n/a'} | "
                f"{forecast.precipitation_probability or 'n/a'} | "
                f"{forecast.wind_speed_mph or 'n/a'} | "
                f"{forecast.wind_gust_mph or 'n/a'} | "
                f"{forecast.short_forecast or 'n/a'} |"
            )
    lines.extend(
        [
            "",
            "## Latest Features",
            "",
            "| Target time | Freeze | Rain | Wind | Confidence | Readiness |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    if not features:
        lines.append("| _No stored features_ |  |  |  |  |  |")
    else:
        for feature in features:
            lines.append(
                "| "
                f"{feature.target_time.isoformat()} | "
                f"{feature.freeze_risk_score or 'n/a'} | "
                f"{feature.rain_risk_score or 'n/a'} | "
                f"{feature.wind_risk_score or 'n/a'} | "
                f"{feature.weather_confidence_score or 'n/a'} | "
                f"{_feature_readiness(feature)} |"
            )
    lines.extend(
        [
            "",
            "## Linked Markets",
            "",
            "| Ticker | Metric | Operator | Target | Confidence | Reason |",
            "|---|---|---|---:|---:|---|",
        ]
    )
    if not links:
        lines.append("| _No linked weather markets_ |  |  |  |  |  |")
    else:
        for link in links:
            lines.append(
                "| "
                f"{link.ticker} | {link.weather_metric} | {link.target_operator} | "
                f"{link.target_value or 'n/a'} | {link.confidence} | {link.reason} |"
            )
    lines.extend(
        [
            "",
            "## Forecast Readiness",
            "",
            f"- Stored forecast rows: {len(forecasts)}",
            f"- Stored feature rows shown: {len(features)}",
            f"- Linked markets shown: {len(links)}",
            "",
            "## Known Limitations",
            "",
            "- Feature builder uses stored weather forecasts only.",
            "- NOAA availability, forecast freshness, and market text ambiguity can limit "
            "coverage.",
            "- Outputs are diagnostic and simulated, not live-trading signals.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_weather_backtest(
    weather_v2: dict[str, Any],
    market_implied: dict[str, Any],
    weather_metrics: dict[str, Any],
    implied_metrics: dict[str, Any],
    days: int,
) -> str:
    delta = _decimal_delta(weather_metrics["total_pnl"], implied_metrics["total_pnl"])
    lines = [
        "# Weather Backtest",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Window: {days} days",
        f"- Weather-linked markets: {weather_v2['linked_market_count']}",
        "",
        "## Summary",
        "",
        "| Model | Evaluated trades | Total P&L | Brier score | Log loss |",
        "|---|---:|---:|---:|---:|",
        _summary_row("weather_v2", weather_metrics),
        _summary_row("market_implied_v1", implied_metrics),
        "",
        f"- P&L delta: {delta}",
        "",
        "## Notes",
        "",
        f"- weather_v2 evaluated forecasts: {weather_v2['evaluated_forecasts']}",
        f"- market_implied_v1 evaluated forecasts: {market_implied['evaluated_forecasts']}",
        "- If no evaluated trades appear, link markets, build features, forecast, "
        "and sync settlements.",
        "",
    ]
    return "\n".join(lines)


def _feature_readiness(feature: Any) -> str:
    raw = decode_json(feature.raw_json)
    if raw.get("forecast_age_hours") is None:
        return "unknown age"
    if feature.weather_confidence_score is None:
        return "missing confidence"
    return "ready"


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
    return str((to_decimal(left) or Decimal("0")) - (to_decimal(right) or Decimal("0")))
