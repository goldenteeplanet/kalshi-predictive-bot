from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.personal_trader.contracts import READ_ONLY_BOUNDARY
from kalshi_predictor.personal_trader.service import (
    build_personal_trade_brief,
    conversational_response,
    personal_trader_status,
    recommendation_audit_events,
)


def generate_personal_trader_report(
    session: Session,
    *,
    output_path: Path = Path("reports/personal_trader_brief.md"),
    settings: Settings | None = None,
    natural_language_query: str = "What should I trade today?",
    persist: bool = False,
) -> Path:
    brief = build_personal_trade_brief(
        session,
        settings=settings or get_settings(),
        natural_language_query=natural_language_query,
        persist=persist,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_personal_trader_report(brief), encoding="utf-8")
    return output_path


def render_personal_trader_report(brief: dict[str, Any]) -> str:
    lines = [
        "# Phase 3U Personal AI Trader Brief",
        "",
        "## Conversational Answer",
        "",
        "```text",
        conversational_response(brief),
        "```",
        "",
        "## Summary",
        "",
        f"- Brief ID: {brief['brief_id']}",
        f"- Schema version: {brief['schema_version']}",
        f"- Mode: {brief['execution_mode']}",
        f"- As of: {brief['as_of']} {brief['timezone']}",
        f"- Ranking policy: {brief['ranking_policy_version']}",
        f"- Markets scanned: {brief['summary']['markets_scanned']}",
        f"- Candidates considered: {brief['summary']['candidates_considered']}",
        f"- Eligible candidates: {brief['summary']['eligible_count']}",
        f"- Recommendations: {brief['summary']['recommended_count']}",
        f"- No-trade active: {brief['no_trade']['active']}",
        "",
        "## Recommendations",
        "",
    ]
    if brief["recommendations"]:
        lines.append(
            "| Rank | Ticker | Side | Size | Net EV | Risk LCB | Expires |"
        )
        lines.append("|---:|---|---|---:|---:|---:|---|")
        for card in brief["recommendations"]:
            lines.append(
                " | ".join(
                    [
                        f"| {card['slate_rank']}",
                        card["market"]["market_ticker"],
                        card["market"]["side"],
                        str(card["economics"]["approved_quantity"]),
                        card["economics"]["expected_net_ev_total"],
                        card["economics"]["risk_adjusted_ev_lcb_total"],
                        f"{card['timing']['recommendation_expires_at']} |",
                    ]
                )
            )
    else:
        lines.append(f"Trade nothing: {brief['no_trade']['message']}")
    lines.extend(
        [
            "",
            "## Rejection Summary",
            "",
        ]
    )
    for key, value in brief["rejection_summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Watchlist And Rejected Candidates",
            "",
        ]
    )
    if brief["watchlist"]:
        for row in brief["watchlist"][:20]:
            reasons = ", ".join(row["reason_codes"])
            lines.append(f"- {row['market_id']} ({row['status']}): {reasons}")
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Source Health",
            "",
            "| Source | Status | As of | Lag ms |",
            "|---|---|---|---:|",
        ]
    )
    for row in brief["source_health"]:
        lines.append(f"| {row['source']} | {row['status']} | {row['as_of']} | {row['lag_ms']} |")
    lines.extend(
        [
            "",
            "## No-Write Proof",
            "",
        ]
    )
    for key, value in READ_ONLY_BOUNDARY.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Rollout Stage",
            "",
            (
                "- Current implementation stage: Stage 0/1 contracts, "
                "disabled-by-default local advisory."
            ),
            (
                "- Live advisory remains unavailable without separate security, data, "
                "risk, and audit sign-off."
            ),
            "",
            "## Rollback",
            "",
            "- Set `PHASE_3U_PERSONAL_AI_TRADER_ENABLED=false`.",
            "- Downgrade `PHASE_3U_MODE=DISABLED`.",
            "- Keep immutable audit rows; do not rewrite historical briefs.",
            (
                "- Core forecasting, paper learning, sizing, risk, and execution "
                "paths are independent."
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def personal_trader_status_report(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    status = personal_trader_status(session, settings=settings or get_settings())
    events = recommendation_audit_events(session)
    return {
        **status,
        "audit_event_count": len(events),
        "read_only": all(
            value is False
            for key, value in READ_ONLY_BOUNDARY.items()
            if key.startswith("allow_")
        ),
    }
