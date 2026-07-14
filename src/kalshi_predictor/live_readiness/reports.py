from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.live_readiness.catalog import catalog_summary
from kalshi_predictor.live_readiness.service import (
    evaluate_live_readiness,
    live_readiness_card,
)
from kalshi_predictor.utils.time import utc_now


def generate_live_readiness_report(
    session: Session,
    *,
    output_path: str | Path = "reports/live_readiness_report.md",
    json_output_path: str | Path | None = "reports/live_readiness_decision.json",
    settings: Settings | None = None,
    target_stage: str | None = None,
    persist: bool = True,
) -> Path:
    resolved = settings or get_settings()
    result = evaluate_live_readiness(
        session,
        settings=resolved,
        target_stage=target_stage,
        persist=persist,
    )
    if persist:
        session.commit()
    decision = result["review"]
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_live_readiness_report(decision), encoding="utf-8")
    if json_output_path is not None:
        json_output = Path(json_output_path)
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(
            __import__("json").dumps(decision, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
    return output


def render_live_readiness_report(decision: dict[str, Any]) -> str:
    summary = catalog_summary()
    blockers = [
        row
        for row in decision["control_results"]
        if row["severity"] in {"CRITICAL", "HIGH"} and row["status"] != "PASS"
    ]
    lines = [
        "# Phase 3V Live Trading Readiness Review",
        "",
        f"- Generated at: `{utc_now().isoformat()}`",
        f"- Review id: `{decision['review_id']}`",
        f"- Decision: `{decision['decision']}`",
        f"- Target environment: `{decision['target_environment']}`",
        f"- Target stage: `{decision['target_stage']}`",
        f"- Diagnostic score: `{decision['diagnostic_score']['score']} / 100`",
        f"- Controls: `{summary['control_count']}` total, `{summary['critical_count']}` critical",
        f"- Evidence manifest: `{decision['lineage']['evidence_manifest_id']}`",
        "",
        "## Safety Boundary",
        "",
        "- This review does not enable live trading.",
        "- This review does not enable demo execution.",
        "- This review does not create, submit, cancel, or replace orders.",
        "- Diagnostic score is informational only; hard-veto controls override it.",
        "- Invalid, expired, revoked, or mismatched certificates must allow cancel-only at most.",
        "",
        "## Decision Reasons",
        "",
    ]
    if decision["reason_codes"]:
        lines.extend(f"- `{code}`" for code in decision["reason_codes"])
    else:
        lines.append("- No blocking reason codes recorded.")
    lines.extend(
        [
            "",
            "## Mandatory Blockers",
            "",
            "| Control | Severity | Status | Title |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in blockers[:40]:
        lines.append(
            f"| {row['control_id']} | {row['severity']} | {row['status']} | "
            f"{row['title']} |"
        )
    if not blockers:
        lines.append("| n/a | n/a | n/a | No critical or high blockers. |")
    lines.extend(
        [
            "",
            "## Gate Summary",
            "",
            "| Gate | Status | Critical | Reasons |",
            "| --- | --- | --- | --- |",
        ]
    )
    for gate in decision["gates"]:
        lines.append(
            f"| {gate['family']} | {gate['status']} | {gate['critical']} | "
            f"{', '.join(gate['reason_codes']) or 'none'} |"
        )
    lines.extend(
        [
            "",
            "## Launch Envelope",
            "",
        ]
    )
    if decision["launch_envelope"]:
        envelope = decision["launch_envelope"]
        lines.extend(
            [
                f"- Max contracts per order: `{envelope['max_contracts_per_order']}`",
                f"- Max total live contracts: `{envelope['max_total_live_contracts']}`",
                "- Phase 3N final authority required: `true`",
                "- New risk requires active certificate: `true`",
            ]
        )
    else:
        lines.append("- No launch envelope was issued.")
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            _recommended_next_action(decision),
            "",
            "## Reference Sources",
            "",
            "- Kalshi API documentation: https://docs.kalshi.com/welcome",
            "- Kalshi rate-limit documentation: https://docs.kalshi.com/getting_started/rate_limits",
            "- Kalshi API key documentation: https://docs.kalshi.com/getting_started/api_keys",
            "- Kalshi WebSocket documentation: https://docs.kalshi.com/websockets/websocket-connection",
            "- NIST SP 800-61 Rev. 3: https://csrc.nist.gov/pubs/sp/800/61/r3/final",
            "",
        ]
    )
    return "\n".join(lines)


def live_readiness_dashboard_card(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    return live_readiness_card(session, settings=settings)


def _recommended_next_action(decision: dict[str, Any]) -> str:
    if decision["decision"] == "GO":
        return (
            "Real-world launch still requires current external evidence, human approval, "
            "and an active short-lived readiness certificate."
        )
    if decision["decision"] == "CONDITIONAL_GO":
        return "Resolve or expire all conditional exceptions before increasing launch scope."
    if decision["decision"] == "NO_GO":
        return "Fix failed mandatory controls, collect fresh evidence, and rerun the review."
    return "Supply missing evidence for all critical and high controls, then rerun the review."

