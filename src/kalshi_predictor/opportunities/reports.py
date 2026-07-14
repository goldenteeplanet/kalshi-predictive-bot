from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import Market
from kalshi_predictor.opportunities.market_identity import (
    market_identity_fields,
    verify_market_identity,
)
from kalshi_predictor.opportunities.payout_scoring import (
    is_acceptable_best_payout,
    payout_metrics_from_ranking,
)
from kalshi_predictor.opportunities.repository import get_recent_rankings
from kalshi_predictor.opportunities.scanner import OpportunityScanSummary, scan_opportunities
from kalshi_predictor.ui.market_display import recommendation_label, summarize_market_title
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now


def generate_opportunities_report(
    session: Session,
    *,
    model_name: str,
    limit: int,
    output_path: str | Path,
    settings: Settings | None = None,
    min_edge: Any = None,
    min_score: Any = None,
    ticker_scope: set[str] | list[str] | tuple[str, ...] | None = None,
    scan_mode: str = "HISTORICAL_RESEARCH_SCAN",
) -> tuple[Path, OpportunityScanSummary]:
    summary = scan_opportunities(
        session,
        model_name=model_name,
        limit=limit,
        settings=settings,
        min_edge=min_edge,
        min_score=min_score,
        ticker_scope=ticker_scope,
        scan_mode=scan_mode,
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_opportunities(summary, settings or get_settings()), encoding="utf-8")
    return output, summary


def generate_market_rankings_report(
    session: Session,
    *,
    limit: int,
    output_path: str | Path,
) -> Path:
    rankings = get_recent_rankings(session, limit=limit)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_rankings(rankings), encoding="utf-8")
    return output


def generate_best_payouts_report(
    session: Session,
    *,
    model_name: str,
    limit: int,
    output_path: str | Path,
) -> Path:
    rows = best_payout_rows(session, model_name=model_name, limit=limit)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_best_payouts(rows, model_name=model_name), encoding="utf-8")
    return output


def best_payout_rows(
    session: Session,
    *,
    model_name: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rankings = get_recent_rankings(session, limit=max(limit * 20, 200))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ranking in rankings:
        if model_name and ranking.forecast_model != model_name:
            continue
        if ranking.ticker in seen:
            continue
        seen.add(ranking.ticker)
        metrics = payout_metrics_from_ranking(ranking)
        if not is_acceptable_best_payout(ranking, metrics):
            continue
        market = session.get(Market, ranking.ticker)
        identity = verify_market_identity(session, ranking=ranking, market=market)
        if not identity.tradeable:
            continue
        identity_fields = market_identity_fields(identity)
        rows.append(
            {
                "ticker": ranking.ticker,
                **identity_fields,
                "market_identity": identity.as_dict(),
                "market": summarize_market_title(ranking.title or ranking.ticker),
                "full_title": ranking.title or ranking.ticker,
                "model_name": ranking.forecast_model,
                "recommendation": recommendation_label(ranking.best_side),
                "price": ranking.best_price or "n/a",
                "estimated_edge": ranking.estimated_edge or "n/a",
                "opportunity_score": ranking.opportunity_score,
                "expected_value": decimal_to_str(metrics.expected_value) or "0",
                "payout_to_risk_ratio": decimal_to_str(metrics.payout_to_risk_ratio) or "0",
                "payout_adjusted_score": decimal_to_str(metrics.payout_adjusted_score) or "0",
                "confidence": ranking.model_confidence_score,
                "liquidity_score": ranking.liquidity_score,
                "spread": ranking.spread or "n/a",
                "why": ranking.reason,
                "risks": _best_payout_risks(ranking),
            }
        )
    rows.sort(
        key=lambda row: (
            to_decimal(row["expected_value"]) or 0,
            to_decimal(row["payout_to_risk_ratio"]) or 0,
            to_decimal(row["opportunity_score"]) or 0,
        ),
        reverse=True,
    )
    return rows[:limit]


def _render_opportunities(summary: OpportunityScanSummary, settings: Settings) -> str:
    lines = [
        "# Opportunities",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "",
        "## Scanner Config",
        "",
        f"- Minimum edge: `{settings.opportunity_min_edge}`",
        f"- Minimum score: `{settings.opportunity_min_score}`",
        f"- Maximum spread: `{settings.opportunity_max_spread}`",
        f"- Minimum liquidity: `{settings.opportunity_min_liquidity}`",
        f"- Minimum time to close minutes: `{settings.opportunity_min_time_to_close_minutes}`",
        f"- Scan mode: `{summary.scan_mode}`",
        f"- Current ticker scope count: `{summary.current_ticker_scope_count}`",
        "",
        "## Summary",
        "",
        f"- Markets scanned: {summary.markets_scanned}",
        f"- Rankings inserted: {summary.rankings_inserted}",
        f"- Opportunities detected: {summary.opportunities_detected}",
        f"- Historical rows excluded: {summary.historical_rows_excluded}",
        f"- First hard blocker: {summary.first_hard_blocker or 'n/a'}",
        f"- Top ticker: {summary.top_opportunity_ticker or 'n/a'}",
        f"- Top score: {summary.top_opportunity_score or 'n/a'}",
        "",
        "## Top Opportunities",
        "",
        "| Ticker | Side | Price | Edge | Score | Reason |",
        "|---|---|---:|---:|---:|---|",
    ]
    if not summary.opportunities:
        lines.append("| _No qualifying opportunities_ |  |  |  |  | Thresholds not met |")
    else:
        for opportunity in summary.opportunities:
            lines.append(
                "| "
                f"{opportunity['ticker']} | "
                f"{opportunity['side']} | "
                f"{opportunity['price']} | "
                f"{opportunity['estimated_edge']} | "
                f"{opportunity['opportunity_score']} | "
                f"{opportunity['reason']} |"
            )
    lines.extend(
        [
            "",
            "## Top Ranked Markets",
            "",
            "| Ticker | Model | Side | Edge | Score | Spread | Liquidity |",
            "|---|---|---|---:|---:|---:|---:|",
        ]
    )
    if not summary.rankings:
        lines.append("| _No rankings_ |  |  |  |  |  |  |")
    else:
        for ranking in summary.rankings:
            lines.append(_ranking_row(ranking))
    lines.extend(
        [
            "",
            "## Skipped/Low Confidence Notes",
            "",
            "- Markets below the configured edge or score thresholds are ranked but not flagged.",
            "- Missing liquidity, spread, or close-time fields are scored conservatively.",
            "",
            "## Recommended Next Action",
            "",
            "Review top ranked markets manually, then run paper trading and backtests before any "
            "future phase discussions.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_best_payouts(rows: list[dict[str, Any]], *, model_name: str) -> str:
    lines = [
        "# Best Payout-Adjusted Opportunities",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Model: `{model_name}`",
        "- Mode: PAPER / DEMO ONLY",
        "",
        "## Best payout-adjusted opportunities",
        "",
        "| Ticker | Kalshi URL | Market | Recommendation | Expected value | Payout/risk | Score | Why | Risks |",
        "|---|---|---|---|---:|---:|---:|---|---|",
    ]
    if not rows:
        lines.append(
            "| _No acceptable payout opportunities_ |  |  |  |  |  | "
            "Run fresh scans or wait for better confidence/liquidity and verified links. |  |  |"
        )
    for row in rows:
        lines.append(
            "| "
            f"{row['ticker']} | "
            f"{row['kalshi_url']} | "
            f"{row['market']} | "
            f"{row['recommendation']} | "
            f"{row['expected_value']} | "
            f"{row['payout_to_risk_ratio']} | "
            f"{row['payout_adjusted_score']} | "
            f"{row['why']} | "
            f"{'; '.join(row['risks'])} |"
        )
    lines.extend(
        [
            "",
            "## Reminder",
            "",
            "These are payout-adjusted paper/demo opportunities only. "
            "They are not live trading instructions.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_rankings(rankings: list[Any]) -> str:
    lines = [
        "# Market Rankings",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "",
        "| Ranked at | Ticker | Model | Side | Edge | Score | Reason |",
        "|---|---|---|---|---:|---:|---|",
    ]
    if not rankings:
        lines.append("| _No rankings found_ |  |  |  |  |  |  |")
    else:
        for ranking in rankings:
            lines.append(
                "| "
                f"{ranking.ranked_at.isoformat()} | "
                f"{ranking.ticker} | "
                f"{ranking.forecast_model} | "
                f"{ranking.best_side or ''} | "
                f"{ranking.estimated_edge or ''} | "
                f"{ranking.opportunity_score} | "
                f"{ranking.reason} |"
            )
    lines.append("")
    return "\n".join(lines)


def _ranking_row(ranking: dict[str, Any]) -> str:
    return (
        "| "
        f"{ranking['ticker']} | "
        f"{ranking['forecast_model']} | "
        f"{ranking.get('best_side') or ''} | "
        f"{ranking.get('estimated_edge') or ''} | "
        f"{ranking['opportunity_score']} | "
        f"{ranking.get('spread') or ''} | "
        f"{ranking.get('liquidity') or ''} |"
    )


def _best_payout_risks(ranking: Any) -> list[str]:
    risks = []
    spread = to_decimal(ranking.spread)
    confidence = to_decimal(ranking.model_confidence_score) or 0
    liquidity = to_decimal(ranking.liquidity_score) or 0
    if spread is not None and spread > 0:
        risks.append(f"spread {ranking.spread}")
    if confidence < 60:
        risks.append("confidence not yet high")
    if liquidity < 60:
        risks.append("liquidity needs review")
    return risks or ["standard paper review"]
