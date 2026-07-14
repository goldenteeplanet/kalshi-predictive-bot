from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.backtesting.engine import run_backtest
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import MicrostructureEvent, MicrostructureFeature
from kalshi_predictor.microstructure.repository import (
    feature_to_dict,
    latest_microstructure_feature,
    recent_microstructure_events,
    recent_microstructure_features,
    recent_microstructure_signals,
)
from kalshi_predictor.opportunities.reports import generate_opportunities_report
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


def microstructure_dashboard(
    session: Session,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    features = recent_microstructure_features(session, limit=limit)
    events = recent_microstructure_events(session, limit=limit)
    return {
        "summary": {
            "markets_analyzed": _count(session, MicrostructureFeature),
            "events": _count(session, MicrostructureEvent),
            "latest_feature_at": features[0].created_at.isoformat() if features else "n/a",
        },
        "features": [_feature_row(row) for row in features],
        "events": [_event_row(row) for row in events],
        "spread_changes": [
            _event_row(row) for row in events if row.event_type.startswith("SPREAD")
        ],
        "liquidity_changes": [
            _event_row(row) for row in events if row.event_type.startswith("LIQUIDITY")
        ],
        "imbalances": [
            _event_row(row)
            for row in events
            if row.event_type in {"YES_PRESSURE", "NO_PRESSURE", "IMBALANCE_FLIP"}
        ],
        "price_dislocations": [
            _event_row(row)
            for row in events
            if "DISLOCATION" in row.event_type or row.event_type == "MODEL_MARKET_DIVERGENCE"
        ],
        "late_moves": [_event_row(row) for row in events if row.event_type.startswith("LATE")],
        "smart_money_events": [
            _event_row(row)
            for row in events
            if "SMART_MONEY" in row.event_type or row.event_type == "POSSIBLE_INFORMED_FLOW"
        ],
    }


def microstructure_detail(session: Session, ticker: str) -> dict[str, Any] | None:
    feature = latest_microstructure_feature(session, ticker)
    if feature is None:
        return None
    features = recent_microstructure_features(session, ticker=ticker, limit=50)
    events = recent_microstructure_events(session, ticker=ticker, limit=50)
    signals = recent_microstructure_signals(session, ticker=ticker, limit=50)
    return {
        "ticker": ticker,
        "latest_feature": feature_to_dict(feature),
        "features": [_feature_row(row) for row in features],
        "events": [_event_row(row) for row in events],
        "signals": [_signal_row(row) for row in signals],
        "charts": _charts(features),
        "research_explanation": _research_explanation(feature, events),
    }


def generate_microstructure_report(
    session: Session,
    *,
    output_path: Path = Path("reports/microstructure_report.md"),
) -> Path:
    dashboard = microstructure_dashboard(session, limit=100)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_microstructure_report(dashboard), encoding="utf-8")
    return output_path


def generate_microstructure_opportunities_report(
    session: Session,
    *,
    model_name: str = "microstructure_v1",
    limit: int = 20,
    output_path: Path = Path("reports/microstructure_opportunities.md"),
    settings: Settings | None = None,
) -> Path:
    path, _summary = generate_opportunities_report(
        session,
        model_name=model_name,
        limit=limit,
        output_path=output_path,
        settings=settings or get_settings(),
    )
    return path


def generate_microstructure_backtest_report(
    session: Session,
    *,
    days: int = 30,
    output_path: Path = Path("reports/microstructure_backtest.md"),
) -> Path:
    models = ("microstructure_v1", "market_implied_v1", "ensemble_v2")
    results = [
        run_backtest(
            session,
            model_name=model_name,
            strategy_name="paper_v1",
            days=days,
            persist=True,
            name=f"microstructure_compare:{model_name}:{days}d",
        )
        for model_name in models
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_render_backtest_report(results, days=days), encoding="utf-8")
    return output_path


def render_microstructure_report(dashboard: dict[str, Any]) -> str:
    summary = dashboard["summary"]
    lines = [
        "# Microstructure Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "- Mode: PAPER / DEMO ONLY",
        f"- Markets analyzed: {summary['markets_analyzed']}",
        f"- Events: {summary['events']}",
        "",
        "## Spread Changes",
        "",
        *_event_lines(dashboard["spread_changes"]),
        "## Liquidity Changes",
        "",
        *_event_lines(dashboard["liquidity_changes"]),
        "## Orderbook Imbalances",
        "",
        *_event_lines(dashboard["imbalances"]),
        "## Price Dislocations",
        "",
        *_event_lines(dashboard["price_dislocations"]),
        "## Late Moves",
        "",
        *_event_lines(dashboard["late_moves"]),
        "## Possible Informed-Flow Events",
        "",
        *_event_lines(dashboard["smart_money_events"]),
        "## Top Microstructure Opportunities",
        "",
    ]
    if dashboard["features"]:
        lines.extend(
            [
                "| Ticker | Confidence | Spread | Liquidity | Imbalance | Late | "
                "Dislocation | Flow |",
                "|---|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for row in dashboard["features"][:20]:
            lines.append(
                "| "
                f"{row['ticker']} | {row['microstructure_confidence'] or ''} | "
                f"{row['current_spread'] or ''} | {row['current_liquidity'] or ''} | "
                f"{row['orderbook_imbalance'] or ''} | {row['late_move_score'] or ''} | "
                f"{row['dislocation_score'] or ''} | {row['smart_money_score'] or ''} |"
            )
    else:
        lines.append("No microstructure features have been generated yet.")
    lines.extend(
        [
            "",
            "## Warnings",
            "",
            "- Possible informed flow is a heuristic, not proof.",
            "- Low-liquidity markets can create false microstructure signals.",
            "- No live trading or production execution is included.",
            "",
            "## Recommended Next Action",
            "",
            "Run fresh collection, rebuild microstructure features, then compare report events "
            "against paper-only opportunities before changing thresholds.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_backtest_report(results: list[Any], *, days: int) -> str:
    lines = [
        "# Microstructure Backtest",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Window: {days} days",
        "- Mode: local simulated paper trades only",
        "",
        "| Model | Forecasts | Evaluated | Trades | Win rate | ROI | P&L | Brier |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        summary = result.summary
        lines.append(
            "| "
            f"{result.model_name} | {result.forecasts_scanned} | "
            f"{result.evaluated_forecasts} | {summary.get('total_trades')} | "
            f"{summary.get('win_rate')} | {summary.get('roi_on_exposure')} | "
            f"{summary.get('total_pnl')} | {summary.get('brier_score') or 'n/a'} |"
        )
    lines.extend(
        [
            "",
            "No live orders or live API calls are used.",
            "",
        ]
    )
    return "\n".join(lines)


def _event_lines(events: list[dict[str, Any]]) -> list[str]:
    if not events:
        return ["No events detected.", ""]
    lines = [
        "| Ticker | Event | Severity | Score | Description |",
        "|---|---|---|---:|---|",
    ]
    for event in events[:20]:
        lines.append(
            "| "
            f"{event['ticker']} | {event['event_type']} | {event['severity']} | "
            f"{event['score']} | {event['description']} |"
        )
    lines.append("")
    return lines


def _feature_row(row: MicrostructureFeature) -> dict[str, Any]:
    return {
        "created_at": row.created_at.isoformat(),
        "ticker": row.ticker,
        "snapshot_count": row.snapshot_count,
        "current_spread": row.current_spread,
        "spread_change": row.spread_change,
        "current_liquidity": row.current_liquidity,
        "liquidity_change_pct": row.liquidity_change_pct,
        "orderbook_imbalance": row.orderbook_imbalance,
        "price_velocity": row.price_velocity,
        "late_move_score": row.late_move_score,
        "dislocation_score": row.dislocation_score,
        "smart_money_score": row.smart_money_score,
        "microstructure_confidence": row.microstructure_confidence,
    }


def _event_row(row: MicrostructureEvent) -> dict[str, Any]:
    return {
        "created_at": row.created_at.isoformat(),
        "ticker": row.ticker,
        "event_type": row.event_type,
        "severity": row.severity,
        "score": row.score,
        "title": row.title,
        "description": row.description,
    }


def _signal_row(row: Any) -> dict[str, Any]:
    return {
        "created_at": row.created_at.isoformat(),
        "ticker": row.ticker,
        "signal_name": row.signal_name,
        "signal_strength": row.signal_strength,
        "signal_direction": row.signal_direction,
        "confidence": row.confidence,
        "explanation": row.explanation,
    }


def _charts(features: list[MicrostructureFeature]) -> list[dict[str, Any]]:
    rows = list(reversed(features))
    return [
        {
            "title": "Spread",
            "points": [{"x": index, "y": row.current_spread} for index, row in enumerate(rows)],
        },
        {
            "title": "Liquidity",
            "points": [{"x": index, "y": row.current_liquidity} for index, row in enumerate(rows)],
        },
        {
            "title": "Imbalance",
            "points": [
                {"x": index, "y": row.orderbook_imbalance} for index, row in enumerate(rows)
            ],
        },
    ]


def _research_explanation(feature: MicrostructureFeature, events: list[MicrostructureEvent]) -> str:
    if not events:
        return "No strong microstructure event is attached to this market yet."
    top = max(events, key=lambda event: to_decimal(event.score) or 0)
    return (
        f"Latest microstructure confidence is {feature.microstructure_confidence or 'n/a'}. "
        f"Top event is {top.event_type}: {top.description}"
    )


def _count(session: Session, table: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(table)) or 0)
