from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.backtesting.engine import run_backtest
from kalshi_predictor.backtesting.metrics import calculate_backtest_metrics
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import Forecast, Market, MarketSnapshot, NewsFeature
from kalshi_predictor.news.repository import (
    feature_linked_news,
    latest_news_features,
    latest_news_signals_for_ticker,
    news_dashboard_summary,
)
from kalshi_predictor.opportunities.market_identity import annotated_opportunity_row
from kalshi_predictor.ui.market_display import summarize_market_title
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now


def generate_news_report(
    session: Session,
    *,
    output_path: str | Path,
    settings: Settings | None = None,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        _render_news_report(session, settings=settings or get_settings()),
        encoding="utf-8",
    )
    return output


def generate_news_opportunities_report(
    session: Session,
    *,
    model_name: str,
    limit: int,
    output_path: str | Path,
) -> Path:
    rows = news_opportunity_rows(session, model_name=model_name, limit=limit)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_news_opportunities(rows, model_name=model_name), encoding="utf-8")
    return output


def generate_news_backtest_report(
    session: Session,
    *,
    days: int,
    output_path: str | Path,
) -> Path:
    feature_tickers = {feature.ticker for feature in latest_news_features(session)}
    rows: list[dict[str, Any]] = []
    for model_name in ("news_v1", "market_implied_v1", "ensemble_v2"):
        result = run_backtest(
            session,
            model_name=model_name,
            strategy_name="paper_v1",
            days=days,
            persist=False,
        )
        filtered_trades = [
            trade for trade in result.trades if trade.get("ticker") in feature_tickers
        ]
        metrics = calculate_backtest_metrics(filtered_trades)
        rows.append(
            {
                "model_name": model_name,
                "forecasts_scanned": result.forecasts_scanned,
                "evaluated_forecasts": result.evaluated_forecasts,
                "news_feature_trades": len(filtered_trades),
                "metrics": metrics,
            }
        )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        _render_news_backtest(rows, days=days, news_feature_markets=len(feature_tickers)),
        encoding="utf-8",
    )
    return output


def news_opportunity_rows(
    session: Session,
    *,
    model_name: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for feature in latest_news_features(session, limit=max(limit * 4, 20)):
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
        signals = latest_news_signals_for_ticker(session, feature.ticker, limit=1)
        rows.append(
            annotated_opportunity_row(
                session,
                {
                    "ticker": feature.ticker,
                    "market": summarize_market_title(
                        (market.title if market else None) or feature.ticker
                    ),
                    "linked_news": feature_linked_news(feature),
                    "signal_strength": signals[0].signal_strength if signals else "n/a",
                    "signal_name": signals[0].signal_name if signals else "News Signal",
                    "news_v1_probability": forecast.yes_probability if forecast else "n/a",
                    "market_price": decimal_to_str(market_price) or "n/a",
                    "edge": decimal_to_str(edge) or "n/a",
                    "risk": _news_risk(feature, forecast, snapshot),
                    "recommendation": _recommendation(edge),
                    "score": _row_score(feature, edge),
                },
                ticker=feature.ticker,
                market=market,
            )
        )
    rows.sort(key=lambda row: row["score"], reverse=True)
    return rows[:limit]


def _render_news_report(session: Session, *, settings: Settings) -> str:
    context = news_dashboard_summary(session, limit=20)
    summary = context["summary"]
    items = context["latest_items"]
    links = context["latest_links"]
    signals = context["latest_signals"]
    high_importance = [
        item for item in items if (to_decimal(item["importance_score"]) or 0) >= Decimal("0.70")
    ]
    opportunities = news_opportunity_rows(session, model_name="news_v1", limit=10)
    lines = [
        "# News Intelligence Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "- Mode: PAPER / DEMO ONLY",
        "",
        "## Summary",
        "",
        f"- News items ingested: {summary['items']}",
        f"- Linked markets: {summary['links']}",
        f"- News feature rows: {summary['features']}",
        f"- News signals generated: {summary['signals']}",
        "",
        "## News By Category",
        "",
        "| Category | Count |",
        "|---|---:|",
    ]
    categories = summary["categories"]
    if not categories:
        lines.append("| _No news categories yet_ | 0 |")
    for category, count in sorted(categories.items()):
        lines.append(f"| {category} | {count} |")
    lines.extend(
        [
            "",
            "## High Importance Items",
            "",
            "| Published | Category | Importance | Title |",
            "|---|---|---:|---|",
        ]
    )
    if not high_importance:
        lines.append("| _No high importance news yet_ |  |  |  |")
    for item in high_importance[:10]:
        lines.append(
            f"| {item['published_at'] or 'n/a'} | {item['category']} | "
            f"{item['importance_score']} | {item['title']} |"
        )
    lines.extend(
        [
            "",
            "## Linked Markets",
            "",
            "| Ticker | Confidence | Reason |",
            "|---|---:|---|",
        ]
    )
    if not links:
        lines.append("| _No linked markets yet_ |  |  |")
    for link in links[:10]:
        lines.append(f"| {link['ticker']} | {link['confidence']} | {link['reason']} |")
    lines.extend(
        [
            "",
            "## News Signals Generated",
            "",
            "| Ticker | Signal | Strength | Direction | Confidence |",
            "|---|---|---:|---|---:|",
        ]
    )
    if not signals:
        lines.append("| _No news signals yet_ |  |  |  |  |")
    for signal in signals[:10]:
        lines.append(
            f"| {signal['ticker']} | {signal['signal_name']} | "
            f"{signal['signal_strength']} | {signal['signal_direction'] or 'neutral'} | "
            f"{signal['confidence']} |"
        )
    lines.extend(
        [
            "",
            "## Top News-Driven Opportunities",
            "",
            "| Ticker | Kalshi URL | Signal | Probability | Market price | Edge | Recommendation |",
            "|---|---|---|---:|---:|---:|---|",
        ]
    )
    if not opportunities:
        lines.append(
            "| _No news-driven opportunities yet_ |  |  |  |  |  | Run news_v1 forecasts. |"
        )
    for row in opportunities[:10]:
        link = row["kalshi_url"] if row.get("kalshi_url_verified") else row["kalshi_url_status"]
        lines.append(
            f"| {row['ticker']} | {link} | {row['signal_name']} | "
            f"{row['news_v1_probability']} | {row['market_price']} | {row['edge']} | "
            f"{row['recommendation']} |"
        )
    lines.extend(
        [
            "",
            "## Missing Configuration",
            "",
            f"- NEWS_ENABLED: {settings.news_enabled}",
            f"- RSS feeds configured: {'yes' if settings.news_rss_feeds_json.strip() else 'no'}",
            "- If feeds are missing, use manual JSON/CSV ingestion first.",
            "",
            "## Recommended Next Action",
            "",
            "Ingest news, link markets, build features, generate news signals, run news_v1 "
            "forecasts, then review paper/demo opportunities.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_news_opportunities(rows: list[dict[str, Any]], *, model_name: str) -> str:
    lines = [
        "# News-Driven Opportunities",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Model: `{model_name}`",
        "- Mode: PAPER / DEMO ONLY",
        "",
        "| Ticker | Market | Kalshi URL | Linked news | Signal | Probability | Market | Edge | "
        "Risk | Recommendation |",
        "|---|---|---|---|---|---:|---:|---:|---|---|",
    ]
    if not rows:
        lines.append(
            "| _No news-driven rows_ |  |  |  |  |  |  |  | Need features/forecasts. |  |"
        )
    for row in rows:
        linked_titles = "; ".join(
            item.get("title", "untitled") for item in row["linked_news"][:3]
        )
        link = row["kalshi_url"] if row.get("kalshi_url_verified") else row["kalshi_url_status"]
        lines.append(
            f"| {row['ticker']} | {row['market']} | {link} | {linked_titles or 'n/a'} | "
            f"{row['signal_name']} {row['signal_strength']} | "
            f"{row['news_v1_probability']} | {row['market_price']} | {row['edge']} | {row['risk']} | "
            f"{row['recommendation']} |"
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


def _render_news_backtest(
    rows: list[dict[str, Any]],
    *,
    days: int,
    news_feature_markets: int,
) -> str:
    lines = [
        "# News Backtest",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Window: {days} days",
        f"- Markets with news features: {news_feature_markets}",
        "",
        "## Comparison On News-Feature Markets",
        "",
        "| Model | Forecasts scanned | Evaluated | Trades | Total P&L | Brier | Log loss |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        metrics = row["metrics"]
        lines.append(
            f"| {row['model_name']} | {row['forecasts_scanned']} | "
            f"{row['evaluated_forecasts']} | {row['news_feature_trades']} | "
            f"{metrics['total_pnl']} | {_metric(metrics.get('brier_score'))} | "
            f"{_metric(metrics.get('log_loss'))} |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- The comparison filters simulated trades to tickers that have news feature rows.",
            "- If there are no evaluated trades, sync settlements and run news_v1 forecasts first.",
            "- No live API calls or real orders are used.",
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


def _row_score(feature: NewsFeature, edge: Any) -> float:
    edge_value = abs(float(to_decimal(edge) or Decimal("0")))
    importance = float(to_decimal(feature.max_importance) or Decimal("0"))
    freshness = float(to_decimal(feature.freshness_score) or Decimal("0"))
    return edge_value * 10 + importance + freshness


def _recommendation(edge: Any) -> str:
    edge_value = to_decimal(edge)
    if edge_value is None:
        return "Needs news_v1 forecast"
    if edge_value >= Decimal("0.05"):
        return "Paper review: YES side edge"
    if edge_value <= Decimal("-0.05"):
        return "Paper review: NO side edge"
    return "Watchlist only"


def _news_risk(
    feature: NewsFeature,
    forecast: Forecast | None,
    snapshot: MarketSnapshot | None,
) -> str:
    risks = []
    if forecast is None:
        risks.append("missing news_v1 forecast")
    if snapshot is None:
        risks.append("missing market snapshot")
    if (to_decimal(feature.freshness_score) or Decimal("0")) < Decimal("0.40"):
        risks.append("stale news")
    if feature.news_count < 2:
        risks.append("single news item")
    return "; ".join(risks) if risks else "standard paper review"


def _metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)
