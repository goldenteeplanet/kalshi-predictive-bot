from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.advanced_risk.repository import advanced_risk_summary
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.utils.time import utc_now


def advanced_risk_card(session: Session, *, settings: Settings | None = None) -> dict[str, Any]:
    resolved = settings or get_settings()
    summary = advanced_risk_summary(session)
    latest = summary["latest_decisions"][0] if summary["latest_decisions"] else None
    return {
        "mode": resolved.advanced_risk_engine_mode.upper(),
        "paper_only": True,
        "decision_count": summary["decision_count"],
        "allow_count": summary["allow_count"],
        "reduce_count": summary["reduce_count"],
        "block_count": summary["block_count"],
        "active_reserved_contracts": summary["active_reserved_contracts"],
        "latest_action": latest["action"] if latest else "none",
        "latest_ticker": latest["ticker"] if latest else "none",
        "latest_reason": ", ".join((latest.get("reason_codes") or [])[:3]) if latest else "none",
        "rollout_cap": resolved.advanced_risk_live_max_contracts,
        "global_cap": resolved.advanced_risk_global_max_contracts,
    }


def generate_advanced_risk_report(
    session: Session,
    *,
    output_path: str | Path = "reports/advanced_risk_report.md",
    settings: Settings | None = None,
) -> Path:
    resolved = settings or get_settings()
    summary = advanced_risk_summary(session)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Advanced Risk Engine Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Mode: `{resolved.advanced_risk_engine_mode}`",
        f"- Version: `{resolved.advanced_risk_engine_version}`",
        f"- Live rollout cap: `{resolved.advanced_risk_live_max_contracts}`",
        f"- Global cap: `{resolved.advanced_risk_global_max_contracts}`",
        "- Live trading: `not enabled by Phase 3N`",
        "",
        "## Decision Counts",
        "",
        f"- Total decisions: {summary['decision_count']}",
        f"- Allow: {summary['allow_count']}",
        f"- Reduce: {summary['reduce_count']}",
        f"- Block: {summary['block_count']}",
        f"- Active reserved contracts: {summary['active_reserved_contracts']}",
        "",
        "## Implemented Policy",
        "",
        "- Phase 3M remains the only confidence-sizing proposer.",
        "- Phase 3N can only preserve, reduce, or block the Phase 3M boundary quantity.",
        "- Paper order creation is the integrated boundary for this repository.",
        "- Kelly and risk-adjusted EV are disabled by default until calibrated.",
        "- Missing optional depth or edge data uses conservative caps instead of promotion.",
        "",
        "## Recent Decisions",
        "",
        "| Time | Action | Mode | Ticker | 3M | Candidate | Executed | Reasons |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in summary["latest_decisions"][:20]:
        reasons = ", ".join((row.get("reason_codes") or [])[:5])
        lines.append(
            "| "
            f"{row['decision_timestamp']} | {row['action']} | {row['mode']} | "
            f"{row['ticker']} | {row['phase_3m_proposed_contracts']} | "
            f"{row['live_candidate_contracts']} | {row['executed_contracts']} | "
            f"{reasons} |"
        )
    if not summary["latest_decisions"]:
        lines.append("| n/a | n/a | n/a | n/a | 0 | 0 | 0 | No decisions yet |")
    lines.extend(
        [
            "",
            "## Missing Data And Deferred Integrations",
            "",
            "- Broker account equity, buying power, and margin are not available locally.",
            (
                "- Broker pending orders are not available; paper orders and local "
                "reservations are counted."
            ),
            (
                "- Venue depth is used when orderbook snapshots are present; otherwise "
                "depth is conservative."
            ),
            (
                "- Stop/target brackets do not exist in the paper ledger; binary "
                "max-loss is used as stop risk."
            ),
            (
                "- Live execution remains disabled unless separate existing execution "
                "settings permit it."
            ),
            "",
            "## Rollout",
            "",
            "1. Keep `ADVANCED_RISK_ENGINE_MODE=disabled` for no behavior change.",
            "2. Set `ADVANCED_RISK_ENGINE_MODE=shadow` to collect hypothetical caps.",
            "3. Set `ADVANCED_RISK_ENGINE_MODE=live` with `ADVANCED_RISK_LIVE_MAX_CONTRACTS=1`.",
            "4. Increase the live cap to 3 then 5 only after reviewing shadow outcomes.",
            "",
            "Rollback: set `ADVANCED_RISK_ENGINE_MODE=disabled` and restart the process.",
            "",
        ]
    )
    output.write_text("\n".join(lines), encoding="utf-8")
    return output
