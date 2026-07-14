from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import Forecast, Market, MarketSnapshot
from kalshi_predictor.meta.diagnostics import meta_diagnostics
from kalshi_predictor.meta.evaluator import evaluate_meta_model
from kalshi_predictor.meta.explanations import explain_meta_selection
from kalshi_predictor.meta.repository import (
    latest_meta_decision,
    latest_meta_performance,
    recent_meta_decisions,
    row_to_dict,
)
from kalshi_predictor.opportunities.market_identity import annotated_opportunity_row
from kalshi_predictor.utils.decimals import decimal_to_str, midpoint, to_decimal
from kalshi_predictor.utils.time import utc_now


def meta_dashboard(session: Session, *, limit: int = 50) -> dict[str, Any]:
    decisions = [row_to_dict(row) or {} for row in recent_meta_decisions(session, limit=limit)]
    performance = row_to_dict(latest_meta_performance(session))
    diagnostics = meta_diagnostics(session, limit=limit)
    opportunities = meta_opportunity_rows(session, limit=min(limit, 20))
    distribution = Counter(str(row.get("selected_model_name") or "unknown") for row in decisions)
    fallback_count = sum(1 for row in decisions if row.get("fallback_model_name"))
    return {
        "summary": {
            "decisions": len(decisions),
            "fallback_count": fallback_count,
            "fallback_rate": _percent(fallback_count, len(decisions)),
            "selected_model_distribution": dict(distribution),
            "top_selected_model": distribution.most_common(1)[0][0] if distribution else "n/a",
        },
        "latest_decisions": decisions[:limit],
        "performance": performance,
        "diagnostics": diagnostics,
        "opportunities": opportunities,
        "category_breakdown": _category_breakdown(decisions),
    }


def meta_detail(session: Session, ticker: str) -> dict[str, Any] | None:
    decision = row_to_dict(latest_meta_decision(session, ticker))
    if decision is None:
        return None
    forecasts = list(
        session.scalars(
            select(Forecast)
            .where(Forecast.ticker == ticker)
            .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
            .limit(25)
        )
    )
    explanation = explain_meta_selection(session, ticker)
    return {
        "ticker": ticker,
        "decision": decision,
        "explanation": explanation,
        "trust_scores": decision.get("trust_scores_json") or {},
        "competing_models": decision.get("competing_models_json") or {},
        "forecast_history": [
            {
                "forecasted_at": row.forecasted_at.isoformat(),
                "model_name": row.model_name,
                "yes_probability": row.yes_probability,
                "notes": row.notes,
            }
            for row in forecasts
        ],
        "diagnostics": explanation.get("diagnostics", []),
    }


def generate_meta_evaluation_report(
    session: Session,
    *,
    days: int = 90,
    output_path: Path = Path("reports/meta_evaluation.md"),
) -> Path:
    result = evaluate_meta_model(session, days=days, persist=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_meta_evaluation_report(result.rows, days=days), encoding="utf-8")
    return output_path


def render_meta_evaluation_report(rows: dict[str, dict[str, Any]], *, days: int) -> str:
    lines = [
        "# Meta Model Evaluation",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Lookback days: {days}",
        "- Scope: local forecasts and paper/demo records only",
        "",
        "| Model | Forecasts | Evaluated | Brier | Log loss | ROI | Win rate |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for model_name, row in rows.items():
        lines.append(
            "| "
            f"{model_name} | {row['forecast_count']} | {row['evaluated_count']} | "
            f"{_display(row['brier_score'])} | {_display(row['log_loss'])} | "
            f"{_display(row['roi'])} | {_display(row['win_rate'])} |"
        )
    lines.extend(
        [
            "",
            "No live trading was evaluated or enabled.",
            "",
        ]
    )
    return "\n".join(lines)


def generate_meta_report(
    session: Session,
    *,
    output_path: Path = Path("reports/meta_report.md"),
) -> Path:
    context = meta_dashboard(session)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_meta_report(context), encoding="utf-8")
    return output_path


def render_meta_report(context: dict[str, Any]) -> str:
    summary = context["summary"]
    lines = [
        "# Meta Model Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "- Mode: paper/demo only",
        "",
        "## Selected Model Distribution",
        "",
    ]
    if summary["selected_model_distribution"]:
        for model_name, count in summary["selected_model_distribution"].items():
            lines.append(f"- {model_name}: {count}")
    else:
        lines.append("- No meta decisions yet.")
    lines.extend(
        [
            "",
            "## Fallback Usage",
            "",
            f"- Fallback decisions: {summary['fallback_count']}",
            f"- Fallback rate: {summary['fallback_rate']}",
            "",
            "## High-Disagreement Markets",
            "",
        ]
    )
    high_disagreement = [
        row for row in context["latest_decisions"] if _decision_disagreement(row) >= Decimal("0.20")
    ]
    if high_disagreement:
        for row in high_disagreement[:10]:
            lines.append(
                f"- {row['ticker']}: {row['selected_model_name']} "
                f"trust {row.get('selected_confidence') or 'n/a'}"
            )
    else:
        lines.append("- None detected in recent decisions.")
    lines.extend(
        [
            "",
            "## Meta Model Opportunities",
            "",
            "| Ticker | Kalshi URL | Selected model | Trust | Probability | Edge | Score |",
            "|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in context["opportunities"]:
        link = row["kalshi_url"] if row.get("kalshi_url_verified") else row["kalshi_url_status"]
        lines.append(
            "| "
            f"{row['ticker']} | {link} | {row['selected_model']} | {row['trust_score']} | "
            f"{row['probability']} | {row['edge']} | {row['opportunity_score']} |"
        )
    if not context["opportunities"]:
        lines.append("| _No meta opportunities yet_ | | | | | | |")
    lines.extend(
        [
            "",
            "## Diagnostics",
            "",
        ]
    )
    for diagnostic in context["diagnostics"]:
        lines.append(
            f"- [{diagnostic['severity']}] {diagnostic['title']}: {diagnostic['message']}"
        )
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            _recommended_next_action(context),
            "",
        ]
    )
    return "\n".join(lines)


def generate_meta_opportunities_report(
    session: Session,
    *,
    limit: int = 20,
    output_path: Path = Path("reports/meta_opportunities.md"),
) -> Path:
    rows = meta_opportunity_rows(session, limit=limit)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_meta_opportunities_report(rows), encoding="utf-8")
    return output_path


def render_meta_opportunities_report(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Meta Model Opportunities",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "",
        "| Ticker | Kalshi URL | Selected model | Trust | Probability | Market price | Edge | Score | Reason |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        link = row["kalshi_url"] if row.get("kalshi_url_verified") else row["kalshi_url_status"]
        lines.append(
            "| "
            f"{row['ticker']} | {link} | {row['selected_model']} | {row['trust_score']} | "
            f"{row['probability']} | {row['market_price']} | {row['edge']} | "
            f"{row['opportunity_score']} | {row['reason']} |"
        )
    if not rows:
        lines.append("| _No meta opportunities yet_ | | | | | | | | |")
    lines.extend(["", "Paper/demo only. No production execution is enabled.", ""])
    return "\n".join(lines)


def meta_opportunity_rows(session: Session, *, limit: int = 20) -> list[dict[str, Any]]:
    decisions = [row_to_dict(row) or {} for row in recent_meta_decisions(session, limit=limit * 3)]
    rows = []
    for decision in decisions:
        probability = to_decimal(decision.get("selected_probability"))
        trust = to_decimal(decision.get("selected_confidence")) or Decimal("0")
        snapshot = _latest_snapshot(session, str(decision.get("ticker")))
        market_price = _market_price(snapshot)
        if probability is None or market_price is None:
            continue
        if probability >= market_price:
            side = "BUY_YES"
            edge = probability - market_price
        else:
            side = "BUY_NO"
            edge = market_price - probability
        score = edge * Decimal("100") + trust * Decimal("0.6")
        ticker = str(decision["ticker"])
        rows.append(
            annotated_opportunity_row(
                session,
                {
                    "ticker": ticker,
                    "selected_model": decision["selected_model_name"],
                    "trust_score": decision.get("selected_confidence") or "0",
                    "probability": decision.get("selected_probability") or "n/a",
                    "market_price": decimal_to_str(market_price) or "n/a",
                    "side": side,
                    "edge": decimal_to_str(edge) or "0",
                    "opportunity_score": decimal_to_str(score) or "0",
                    "reason": decision.get("decision_reason") or "",
                },
                ticker=ticker,
                market=session.get(Market, ticker),
            )
        )
    rows.sort(
        key=lambda row: to_decimal(row["opportunity_score"]) or Decimal("0"),
        reverse=True,
    )
    return rows[:limit]


def _latest_snapshot(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _market_price(snapshot: MarketSnapshot | None) -> Decimal | None:
    if snapshot is None:
        return None
    bid = to_decimal(snapshot.best_yes_bid)
    ask = to_decimal(snapshot.best_yes_ask)
    if bid is not None and ask is not None:
        return midpoint(bid, ask)
    return to_decimal(snapshot.last_price_dollars)


def _category_breakdown(decisions: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in decisions:
        raw = row.get("raw_json") or {}
        counts[str(raw.get("category") or "unknown")] += 1
    return dict(counts)


def _decision_disagreement(row: dict[str, Any]) -> Decimal:
    raw = row.get("raw_json") or {}
    return to_decimal(raw.get("model_disagreement_score")) or Decimal("0")


def _recommended_next_action(context: dict[str, Any]) -> str:
    if not context["latest_decisions"]:
        return "Run build-meta-features, then forecast meta_model_v1."
    performance = context.get("performance") or {}
    if not performance.get("meta_brier_score"):
        return "Let meta_model_v1 forecasts settle, then run meta-evaluate."
    if context["summary"]["fallback_count"]:
        return "Build more specialized features and settled examples to reduce fallback usage."
    return "Review high-disagreement markets before changing autopilot thresholds."


def _percent(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0%"
    value = Decimal(numerator) / Decimal(denominator) * Decimal("100")
    return f"{value.quantize(Decimal('0.1'))}%"


def _display(value: Any) -> str:
    return decimal_to_str(value) or "n/a"
