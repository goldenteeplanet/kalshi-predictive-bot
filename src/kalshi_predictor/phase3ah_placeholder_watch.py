from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE_3AH_PLACEHOLDER_WATCH_VERSION = "phase3ah_sports_placeholder_watch_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3ah_sports")
DEFAULT_PLACEHOLDER_REPORT_PATH = (
    DEFAULT_OUTPUT_DIR / "phase3ah_round_placeholder_resolution_report.json"
)
DEFAULT_SPORTS_EVIDENCE_PATH = DEFAULT_OUTPUT_DIR / "phase3ah_sports_evidence_backfill.json"
DEFAULT_SETTLEMENT_HARVEST_PATH = (
    Path("reports/phase3aa_r2") / "phase3aa_r2_exact_settlement_harvest.json"
)
DEFAULT_PHASE3AA_REPORT_PATH = Path("reports/phase3aa") / "phase3aa_outcome_realizer.json"
DEFAULT_PHASE3AA_R3_REPORT_PATH = (
    Path("reports/phase3aa_r3") / "phase3aa_r3_residual_settlement_audit.json"
)
DEFAULT_PAPER_SETTLEMENT_PATH = (
    Path("reports/paper_settlement_reconciliation")
    / "paper_settlement_reconciliation.json"
)
SETTLEMENT_HARVEST_COMMAND = (
    "kalshi-bot phase3aa-r2-exact-settlement-harvest --output-dir reports/phase3aa_r2"
)
SETTLEMENT_REALIZE_COMMAND = "kalshi-bot phase3aa-realize --no-dry-run --no-sync-settlements"


@dataclass(frozen=True)
class Phase3AHPlaceholderWatchArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


def build_phase3ah_sports_placeholder_watch(
    *,
    placeholder_report_path: Path = DEFAULT_PLACEHOLDER_REPORT_PATH,
    sports_evidence_path: Path = DEFAULT_SPORTS_EVIDENCE_PATH,
    settlement_harvest_path: Path = DEFAULT_SETTLEMENT_HARVEST_PATH,
    phase3aa_report_path: Path = DEFAULT_PHASE3AA_REPORT_PATH,
    phase3aa_r3_report_path: Path = DEFAULT_PHASE3AA_R3_REPORT_PATH,
    paper_settlement_path: Path = DEFAULT_PAPER_SETTLEMENT_PATH,
) -> dict[str, Any]:
    placeholder_report = _load_json_report(placeholder_report_path)
    sports_evidence = _load_json_report(sports_evidence_path)
    settlement_harvest = _load_json_report(settlement_harvest_path)
    phase3aa_report = _load_json_report(phase3aa_report_path)
    phase3aa_r3_report = _load_json_report(phase3aa_r3_report_path)
    paper_settlement = _load_json_report(paper_settlement_path)

    placeholder_summary = _summary(placeholder_report)
    sports_summary = _summary(sports_evidence)
    settlement_summary = _summary(settlement_harvest)
    settlement_freshness = _settlement_freshness(
        phase3aa=phase3aa_report,
        phase3aa_r3=phase3aa_r3_report,
        paper=paper_settlement,
    )
    rows = _placeholder_rows(placeholder_report)
    watch_rows = [_watch_row(row) for row in rows]
    gate = _phase3ae_gate(placeholder_summary)
    settlement_loop = _settlement_loop(settlement_summary, settlement_freshness)
    summary = {
        "placeholder_rows_reviewed": int(placeholder_summary.get("rows_reviewed") or 0),
        "safe_to_apply_rows": int(placeholder_summary.get("safe_to_apply_rows") or 0),
        "still_placeholder_rows": int(placeholder_summary.get("still_placeholder_rows") or 0),
        "fetch_error_rows": int(placeholder_summary.get("fetch_error_rows") or 0),
        "unsupported_rows": int(placeholder_summary.get("unsupported_rows") or 0),
        "phase3ae_ready_placeholder_rows": gate["phase3ae_ready_placeholder_rows"],
        "phase3ae_blocked_placeholder_rows": gate["phase3ae_blocked_placeholder_rows"],
        "phase3ae_gate_status": gate["status"],
        "sports_partial_links_without_upgrade": sports_summary.get(
            "partial_links_without_upgrade",
            sports_summary.get("repair_rows_reviewed"),
        ),
        "sports_round_placeholder_rows": sports_summary.get(
            "round_placeholder_resolution_rows",
            summary_value(placeholder_summary, "rows_reviewed"),
        ),
        "settlement_exact_settlements_written": int(
            settlement_summary.get("exact_settlements_written") or 0
        ),
        "settlement_fetch_errors": int(settlement_summary.get("fetch_errors") or 0),
        "settlement_eligible_after_harvest": int(
            settlement_summary.get("eligible_exact_settlements_after") or 0
        ),
        "settlement_realization_cleared_by_fresher_reports": settlement_freshness[
            "realization_cleared_by_fresher_reports"
        ],
        "settlement_stale_realize_prompt_suppressed": settlement_loop[
            "stale_realize_prompt_suppressed"
        ],
        "auto_upgrades_created": 0,
    }
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AH_PLACEHOLDER_WATCH",
        "phase_version": PHASE_3AH_PLACEHOLDER_WATCH_VERSION,
        "mode": "PAPER_ONLY_SPORTS_PLACEHOLDER_WATCH",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "input_paths": {
            "placeholder_report": str(placeholder_report_path),
            "sports_evidence": str(sports_evidence_path),
            "settlement_harvest": str(settlement_harvest_path),
            "phase3aa_report": str(phase3aa_report_path),
            "phase3aa_r3_report": str(phase3aa_r3_report_path),
            "paper_settlement": str(paper_settlement_path),
        },
        "source_availability": {
            "placeholder_report": placeholder_report is not None,
            "sports_evidence": sports_evidence is not None,
            "settlement_harvest": settlement_harvest is not None,
            "phase3aa_report": phase3aa_report is not None,
            "phase3aa_r3_report": phase3aa_r3_report is not None,
            "paper_settlement": paper_settlement is not None,
        },
        "auto_upgrade_policy": {
            "phase3ah_placeholder_watch_creates_verified_links": False,
            "phase3ah_placeholder_watch_realizes_pnl": False,
            "auto_upgrades_created": 0,
            "policy": (
                "Phase 3AH watch is evidence only. Phase 3AE must still apply the "
                "clean team + time + market-type gate, and Phase 3AA must still "
                "realize paper P&L only from exact ticker settlements."
            ),
        },
        "summary": summary,
        "phase3ae_gate": gate,
        "settlement_freshness": settlement_freshness,
        "settlement_watch": settlement_loop,
        "placeholder_watch_rows": watch_rows,
        "next_commands": _next_commands(gate, settlement_loop, watch_rows),
        "recommended_next_action": _recommended_next_action(gate, settlement_loop),
    }


def write_phase3ah_sports_placeholder_watch_report(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    placeholder_report_path: Path = DEFAULT_PLACEHOLDER_REPORT_PATH,
    sports_evidence_path: Path = DEFAULT_SPORTS_EVIDENCE_PATH,
    settlement_harvest_path: Path = DEFAULT_SETTLEMENT_HARVEST_PATH,
    phase3aa_report_path: Path = DEFAULT_PHASE3AA_REPORT_PATH,
    phase3aa_r3_report_path: Path = DEFAULT_PHASE3AA_R3_REPORT_PATH,
    paper_settlement_path: Path = DEFAULT_PAPER_SETTLEMENT_PATH,
) -> Phase3AHPlaceholderWatchArtifactSet:
    payload = build_phase3ah_sports_placeholder_watch(
        placeholder_report_path=placeholder_report_path,
        sports_evidence_path=sports_evidence_path,
        settlement_harvest_path=settlement_harvest_path,
        phase3aa_report_path=phase3aa_report_path,
        phase3aa_r3_report_path=phase3aa_r3_report_path,
        paper_settlement_path=paper_settlement_path,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ah_sports_placeholder_watch.json"
    markdown_path = output_dir / "phase3ah_sports_placeholder_watch.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AHPlaceholderWatchArtifactSet(output_dir, json_path, markdown_path)


def summary_value(summary: dict[str, Any], key: str) -> int:
    return int(summary.get(key) or 0)


def _load_json_report(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    summary = payload.get("summary")
    return summary if isinstance(summary, dict) else {}


def _placeholder_rows(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("rows")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _watch_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "game_key": row.get("game_key"),
        "league": row.get("league"),
        "source_status": row.get("source_status"),
        "home_placeholder_team_key": row.get("home_placeholder_team_key"),
        "away_placeholder_team_key": row.get("away_placeholder_team_key"),
        "source_home_team_name": row.get("source_home_team_name"),
        "source_away_team_name": row.get("source_away_team_name"),
        "resolved_home_team_key": row.get("resolved_home_team_key"),
        "resolved_away_team_key": row.get("resolved_away_team_key"),
        "safe_to_apply": bool(row.get("safe_to_apply")),
        "blocks_phase3ae_upgrade": row.get("blocks_phase3ae_upgrade", True) is not False,
        "example_tickers": row.get("example_tickers") or [],
        "next_action": row.get("next_action"),
    }


def _phase3ae_gate(summary: dict[str, Any]) -> dict[str, Any]:
    rows_reviewed = int(summary.get("rows_reviewed") or 0)
    safe_rows = int(summary.get("safe_to_apply_rows") or 0)
    still_rows = int(summary.get("still_placeholder_rows") or 0)
    fetch_errors = int(summary.get("fetch_error_rows") or 0)
    unsupported = int(summary.get("unsupported_rows") or 0)
    blocked = still_rows + fetch_errors + unsupported
    if safe_rows > 0:
        status = "READY_FOR_PHASE3AE_SAFE_ROWS"
        next_action = (
            "Refresh schedule ingestion for resolved games, then rerun Phase 3AE. "
            "Unresolved placeholder rows remain blocked."
        )
    elif still_rows > 0:
        status = "HOLD_PLACEHOLDER_UPGRADES"
        next_action = (
            "Keep placeholder games blocked. Rerun the placeholder resolver after "
            "source schedules name real teams."
        )
    elif fetch_errors > 0 or unsupported > 0:
        status = "RETRY_OR_MANUAL_SOURCE_REVIEW"
        next_action = "Fix source fetch/manual resolution issues before rerunning Phase 3AE."
    elif rows_reviewed == 0:
        status = "NO_PLACEHOLDER_ROWS"
        next_action = "No round-placeholder watch rows are currently present."
    else:
        status = "PLACEHOLDER_GATE_CLEAR"
        next_action = "Rerun Phase 3AE after refreshing schedule evidence."
    return {
        "status": status,
        "phase3ae_can_create_links_from_placeholders": False,
        "phase3ae_can_evaluate_safe_rows": safe_rows > 0,
        "phase3ae_ready_placeholder_rows": safe_rows,
        "phase3ae_blocked_placeholder_rows": blocked,
        "clean_team_time_market_type_gate_required": True,
        "next_action": next_action,
    }


def _settlement_loop(
    summary: dict[str, Any],
    freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    exact_written = int(summary.get("exact_settlements_written") or 0)
    eligible_after = int(summary.get("eligible_exact_settlements_after") or 0)
    fetch_errors = int(summary.get("fetch_errors") or 0)
    cleared = bool(
        freshness and freshness.get("realization_cleared_by_fresher_reports")
    )
    stale_suppressed = cleared and (exact_written > 0 or eligible_after > 0)
    if stale_suppressed:
        status = (
            "NO_EXACT_SETTLEMENTS_AVAILABLE"
            if fetch_errors == 0
            else "KEEP_EXACT_TICKER_HARVESTING"
        )
        next_action = SETTLEMENT_HARVEST_COMMAND
    elif exact_written > 0 or eligible_after > 0:
        status = "EXACT_SETTLEMENTS_READY_TO_REALIZE"
        next_action = SETTLEMENT_REALIZE_COMMAND
    elif fetch_errors > 0:
        status = "KEEP_EXACT_TICKER_HARVESTING"
        next_action = SETTLEMENT_HARVEST_COMMAND
    else:
        status = "NO_EXACT_SETTLEMENTS_AVAILABLE"
        next_action = SETTLEMENT_HARVEST_COMMAND
    return {
        "status": status,
        "exact_ticker_only": True,
        "sibling_settlements_allowed": False,
        "harvest_command": SETTLEMENT_HARVEST_COMMAND,
        "realize_command": SETTLEMENT_REALIZE_COMMAND,
        "exact_settlements_written": exact_written,
        "eligible_after_harvest": eligible_after,
        "fetch_errors": fetch_errors,
        "stale_realize_prompt_suppressed": stale_suppressed,
        "next_action": next_action,
    }


def _settlement_freshness(
    *,
    phase3aa: dict[str, Any] | None,
    phase3aa_r3: dict[str, Any] | None,
    paper: dict[str, Any] | None,
) -> dict[str, Any]:
    phase3aa_payload = phase3aa if isinstance(phase3aa, dict) else {}
    r3_summary = _summary(phase3aa_r3)
    paper_summary = _summary(paper)
    sources_present = (
        isinstance(phase3aa, dict)
        and isinstance(phase3aa_r3, dict)
        and isinstance(paper, dict)
    )
    eligible_after = int(phase3aa_payload.get("eligible_after_realize") or 0)
    r3_cleared = bool(r3_summary.get("residue_cleared"))
    paper_eligible = int(paper_summary.get("eligible_to_settle_now") or 0)
    cleared = sources_present and eligible_after == 0 and r3_cleared and paper_eligible == 0
    return {
        "source_reports_present": sources_present,
        "phase3aa_generated_at": phase3aa_payload.get("generated_at"),
        "phase3aa_eligible_after_realize": eligible_after,
        "phase3aa_r3_generated_at": (phase3aa_r3 or {}).get("generated_at")
        if isinstance(phase3aa_r3, dict)
        else None,
        "phase3aa_r3_residue_cleared": r3_cleared,
        "paper_generated_at": (paper or {}).get("generated_at")
        if isinstance(paper, dict)
        else None,
        "paper_eligible_to_settle_now": paper_eligible,
        "realization_cleared_by_fresher_reports": cleared,
    }


def _next_commands(
    gate: dict[str, Any],
    settlement_loop: dict[str, Any],
    watch_rows: list[dict[str, Any]],
) -> list[str]:
    commands = [
        "kalshi-bot phase3ah-round-placeholder-resolution --output-dir reports/phase3ah_sports",
    ]
    if gate["phase3ae_can_evaluate_safe_rows"]:
        phase3ae_command = (
            "kalshi-bot phase3ae-verified-sports-connector --output-dir reports/phase3ae"
        )
        candidate_flags = _phase3ae_candidate_game_key_flags(watch_rows)
        if candidate_flags:
            phase3ae_command = f"{phase3ae_command} {candidate_flags}"
        commands.extend(
            [
                (
                    "kalshi-bot phase3ah-sports-evidence-backfill "
                    "--output-dir reports/phase3ah_sports --fetch-schedules --ingest-schedules"
                ),
                phase3ae_command,
            ]
        )
    commands.append(str(settlement_loop["next_action"]))
    commands.append(
        "kalshi-bot phase-orchestrator --analyze --output reports/phase_orchestrator.md "
        "--json-output reports/phase_orchestrator.json --next-prompt prompts/next_phase.md"
    )
    return _dedupe(commands)


def _phase3ae_candidate_game_key_flags(watch_rows: list[dict[str, Any]]) -> str:
    game_keys = sorted(
        {
            str(row.get("game_key") or "").strip()
            for row in watch_rows
            if row.get("safe_to_apply")
            and row.get("blocks_phase3ae_upgrade") is False
            and str(row.get("game_key") or "").strip()
        }
    )
    return " ".join(f"--candidate-game-key {key}" for key in game_keys)


def _recommended_next_action(gate: dict[str, Any], settlement_loop: dict[str, Any]) -> str:
    if settlement_loop["status"] == "EXACT_SETTLEMENTS_READY_TO_REALIZE":
        return (
            "Realize the newly harvested exact paper settlements first, then rerun this "
            "watch report."
        )
    if gate["status"] == "READY_FOR_PHASE3AE_SAFE_ROWS":
        return gate["next_action"]
    if gate["status"] == "HOLD_PLACEHOLDER_UPGRADES":
        return (
            f"{gate['next_action']} Settlement work remains separate: "
            f"{SETTLEMENT_HARVEST_COMMAND}."
        )
    return gate["next_action"]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    gate = payload["phase3ae_gate"]
    settlement = payload["settlement_watch"]
    lines = [
        "# Phase 3AH Sports Placeholder Watch",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "- Verified link auto-upgrades: blocked",
        "- Paper P&L realization: blocked in this command",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Phase 3AE Gate",
            "",
            f"- Status: {gate['status']}",
            f"- Phase 3AE can evaluate safe rows: {gate['phase3ae_can_evaluate_safe_rows']}",
            "- Phase 3AE can create links directly from placeholders: "
            f"{gate['phase3ae_can_create_links_from_placeholders']}",
            f"- Blocked placeholder rows: {gate['phase3ae_blocked_placeholder_rows']}",
            f"- Next action: {gate['next_action']}",
            "",
            "## Settlement Watch",
            "",
            f"- Status: {settlement['status']}",
            f"- Exact ticker only: {settlement['exact_ticker_only']}",
            f"- Sibling settlements allowed: {settlement['sibling_settlements_allowed']}",
            f"- Harvest command: `{settlement['harvest_command']}`",
            f"- Realize command: `{settlement['realize_command']}`",
            "",
            "## Placeholder Rows",
            "",
            "| Game key | Source status | Safe | Blocks Phase 3AE | Next action |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["placeholder_watch_rows"][:50]:
        lines.append(
            f"| {_md(row.get('game_key'))} | {_md(row.get('source_status'))} | "
            f"{row.get('safe_to_apply')} | {row.get('blocks_phase3ae_upgrade')} | "
            f"{_md(row.get('next_action'))} |"
        )
    if not payload["placeholder_watch_rows"]:
        lines.append("| none |  | False | True |  |")
    lines.extend(["", "## Next Commands", ""])
    for command in payload["next_commands"]:
        lines.append(f"- `{command}`")
    lines.extend(["", "## Recommended Next Action", "", payload["recommended_next_action"], ""])
    return "\n".join(lines)


def _md(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")
