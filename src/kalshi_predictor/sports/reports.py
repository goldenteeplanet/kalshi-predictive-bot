from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.backtesting.engine import run_backtest
from kalshi_predictor.backtesting.metrics import calculate_backtest_metrics
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import Forecast, Market, MarketSnapshot
from kalshi_predictor.sports.repository import (
    feature_row,
    latest_sports_features,
    latest_sports_signals_for_ticker,
    sports_dashboard_summary,
    sports_market_links,
)
from kalshi_predictor.opportunities.market_identity import annotated_opportunity_row
from kalshi_predictor.ui.market_display import summarize_market_title
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now


def generate_sports_report(
    session: Session,
    *,
    league: str,
    output_path: str | Path,
    settings: Settings | None = None,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        _render_sports_report(
            session,
            league=league,
            settings=settings or get_settings(),
        ),
        encoding="utf-8",
    )
    return output


def generate_sports_opportunities_report(
    session: Session,
    *,
    model_name: str,
    league: str,
    limit: int,
    output_path: str | Path,
) -> Path:
    rows = sports_opportunity_rows(
        session,
        model_name=model_name,
        league=league,
        limit=limit,
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        _render_sports_opportunities(rows, model_name=model_name, league=league),
        encoding="utf-8",
    )
    return output


def generate_sports_backtest_report(
    session: Session,
    *,
    league: str,
    days: int,
    output_path: str | Path,
) -> Path:
    linked_tickers = {link.ticker for link in sports_market_links(session, league=league)}
    rows: list[dict[str, Any]] = []
    for model_name in ("sports_v1", "mlb_v1", "nba_v1", "nfl_v1", "nhl_v1", "ensemble_v2"):
        result = run_backtest(
            session,
            model_name=model_name,
            strategy_name="paper_v1",
            days=days,
            persist=False,
        )
        filtered_trades = [
            trade for trade in result.trades if trade.get("ticker") in linked_tickers
        ]
        rows.append(
            {
                "model_name": model_name,
                "forecasts_scanned": result.forecasts_scanned,
                "evaluated_forecasts": result.evaluated_forecasts,
                "sports_feature_trades": len(filtered_trades),
                "metrics": calculate_backtest_metrics(filtered_trades),
            }
        )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        _render_sports_backtest(rows, days=days, linked_markets=len(linked_tickers)),
        encoding="utf-8",
    )
    return output


def sports_opportunity_rows(
    session: Session,
    *,
    model_name: str,
    league: str = "ALL",
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for feature in latest_sports_features(session, league=league, limit=max(limit * 4, 20)):
        if feature.ticker is None:
            continue
        forecast = _latest_forecast(session, ticker=feature.ticker, model_name=model_name)
        snapshot = _latest_snapshot(session, ticker=feature.ticker)
        market = session.get(Market, feature.ticker)
        market_price = to_decimal(forecast.market_mid_probability if forecast else None)
        if market_price is None and snapshot is not None:
            market_price = to_decimal(snapshot.best_yes_ask) or to_decimal(
                snapshot.last_price_dollars
            )
        probability = to_decimal(forecast.yes_probability if forecast else None)
        edge = (
            probability - market_price
            if probability is not None and market_price is not None
            else None
        )
        signals = latest_sports_signals_for_ticker(session, feature.ticker, limit=1)
        rows.append(
            annotated_opportunity_row(
                session,
                {
                    "ticker": feature.ticker,
                    "market": summarize_market_title(
                        (market.title if market else None) or feature.ticker
                    ),
                    "league": feature.league,
                    "game_key": feature.game_key,
                    "signal_strength": signals[0].signal_strength if signals else "n/a",
                    "signal_name": signals[0].signal_name if signals else "Sports Signal",
                    "sports_probability": forecast.yes_probability if forecast else "n/a",
                    "market_price": decimal_to_str(market_price) or "n/a",
                    "edge": decimal_to_str(edge) or "n/a",
                    "risk": _sports_risk(feature, forecast, snapshot),
                    "recommendation": _recommendation(edge),
                    "feature": feature_row(feature),
                    "score": _row_score(feature, edge),
                },
                ticker=feature.ticker,
                market=market,
            )
        )
    rows.sort(key=lambda row: row["score"], reverse=True)
    return rows[:limit]


def _render_sports_report(
    session: Session,
    *,
    league: str,
    settings: Settings,
) -> str:
    context = sports_dashboard_summary(session, league=league, limit=20)
    summary = context["summary"]
    opportunities = sports_opportunity_rows(
        session,
        model_name="sports_v1",
        league=league,
        limit=10,
    )
    lines = [
        "# Sports Intelligence Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- League: {summary['league']}",
        "- Mode: PAPER / DEMO ONLY",
        "",
        "## Summary",
        "",
        f"- Sports teams: {summary['teams']}",
        f"- Sports games: {summary['games']}",
        f"- Sports feature rows: {summary['features']}",
        f"- Linked markets: {summary['links']}",
        f"- Sports signals generated: {summary['signals']}",
        "",
        "## Leagues",
        "",
        "| League | Games |",
        "|---|---:|",
    ]
    if not summary["league_counts"]:
        lines.append("| _No sports games yet_ | 0 |")
    for league_name, count in sorted(summary["league_counts"].items()):
        lines.append(f"| {league_name} | {count} |")
    lines.extend(
        [
            "",
            "## Latest Games",
            "",
            "| League | Scheduled | Game | Status |",
            "|---|---|---|---|",
        ]
    )
    if not context["latest_games"]:
        lines.append("| _No games ingested yet_ |  |  |  |")
    for game in context["latest_games"][:10]:
        lines.append(
            f"| {game['league']} | {game['scheduled_at'] or 'n/a'} | "
            f"{game['away_team_key']} at {game['home_team_key']} | {game['status']} |"
        )
    lines.extend(
        [
            "",
            "## Linked Sports Markets",
            "",
            "| Ticker | League | Type | Confidence | Reason |",
            "|---|---|---|---:|---|",
        ]
    )
    if not context["latest_links"]:
        lines.append("| _No linked markets yet_ |  |  |  |  |")
    for link in context["latest_links"][:10]:
        lines.append(
            f"| {link['ticker']} | {link['league']} | {link['market_type']} | "
            f"{link['confidence']} | {link['reason']} |"
        )
    lines.extend(
        [
            "",
            "## Top Sports Opportunities",
            "",
            "| Ticker | Kalshi URL | League | Signal | Probability | Market | Edge | Recommendation |",
            "|---|---|---|---|---:|---:|---:|---|",
        ]
    )
    if not opportunities:
        lines.append(
            "| _No sports-driven opportunities yet_ |  |  |  |  |  |  | Build features. |"
        )
    for row in opportunities:
        link = row["kalshi_url"] if row.get("kalshi_url_verified") else row["kalshi_url_status"]
        lines.append(
            f"| {row['ticker']} | {link} | {row['league']} | {row['signal_name']} | "
            f"{row['sports_probability']} | {row['market_price']} | {row['edge']} | "
            f"{row['recommendation']} |"
        )
    lines.extend(
        [
            "",
            "## Missing Configuration",
            "",
            f"- SPORTS_ENABLED: {settings.sports_enabled}",
            f"- SPORTS_ODDS_ENABLED: {settings.sports_odds_enabled}",
            f"- SPORTS_WEATHER_ENABLED: {settings.sports_weather_enabled}",
            "- Paid sports data APIs are intentionally not required.",
            "",
            "## Recommended Next Action",
            "",
            "Ingest manual sports data, link markets, build features, run sports forecasts, "
            "then review paper/demo opportunities.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_sports_opportunities(
    rows: list[dict[str, Any]],
    *,
    model_name: str,
    league: str,
) -> str:
    lines = [
        "# Sports Opportunities",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Model: `{model_name}`",
        f"- League: {league}",
        "- Mode: PAPER / DEMO ONLY",
        "",
        "| Ticker | League | Market | Kalshi URL | Signal | Probability | Market | Edge | Risk | "
        "Recommendation |",
        "|---|---|---|---|---|---:|---:|---:|---|---|",
    ]
    if not rows:
        lines.append("| _No sports rows_ |  |  |  |  |  |  |  | Need links/features/forecasts. |  |")
    for row in rows:
        link = row["kalshi_url"] if row.get("kalshi_url_verified") else row["kalshi_url_status"]
        lines.append(
            f"| {row['ticker']} | {row['league']} | {row['market']} | {link} | "
            f"{row['signal_name']} {row['signal_strength']} | "
            f"{row['sports_probability']} | {row['market_price']} | {row['edge']} | "
            f"{row['risk']} | {row['recommendation']} |"
        )
    lines.extend(
        [
            "",
            "## Reminder",
            "",
            "These are local paper/demo diagnostics. They are not live trading instructions.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_sports_backtest(
    rows: list[dict[str, Any]],
    *,
    days: int,
    linked_markets: int,
) -> str:
    lines = [
        "# Sports Backtest",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Window: {days} days",
        f"- Sports-linked markets: {linked_markets}",
        "",
        "| Model | Forecasts scanned | Evaluated | Trades | Total P&L | Brier | Log loss |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        metrics = row["metrics"]
        lines.append(
            f"| {row['model_name']} | {row['forecasts_scanned']} | "
            f"{row['evaluated_forecasts']} | {row['sports_feature_trades']} | "
            f"{metrics['total_pnl']} | {_metric(metrics.get('brier_score'))} | "
            f"{_metric(metrics.get('log_loss'))} |"
        )
    lines.extend(
        [
            "",
            "No live API calls or real orders are used.",
            "",
        ]
    )
    return "\n".join(lines)


def _latest_forecast(session: Session, *, ticker: str, model_name: str) -> Forecast | None:
    return session.scalar(
        select(Forecast)
        .where(Forecast.ticker == ticker, Forecast.model_name == model_name)
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(1)
    )


def _latest_snapshot(session: Session, *, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _row_score(feature: Any, edge: Any) -> float:
    edge_value = abs(float(to_decimal(edge) or Decimal("0")))
    confidence = float(to_decimal(feature.confidence_score) or Decimal("0")) / 100
    return edge_value * 10 + confidence


def _recommendation(edge: Any) -> str:
    edge_value = to_decimal(edge)
    if edge_value is None:
        return "Needs sports forecast"
    if edge_value >= Decimal("0.05"):
        return "Paper review: YES side edge"
    if edge_value <= Decimal("-0.05"):
        return "Paper review: NO side edge"
    return "Watchlist only"


def _sports_risk(
    feature: Any,
    forecast: Forecast | None,
    snapshot: MarketSnapshot | None,
) -> str:
    risks = []
    if forecast is None:
        risks.append("missing sports forecast")
    if snapshot is None:
        risks.append("missing market snapshot")
    if (to_decimal(feature.confidence_score) or Decimal("0")) < Decimal("50"):
        risks.append("low sports feature confidence")
    return "; ".join(risks) if risks else "standard paper review"


def _metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)
