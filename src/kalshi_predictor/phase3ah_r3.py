from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.phase3z import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3z_r2 import build_phase3z_r2_sports_provenance_repair
from kalshi_predictor.utils.time import utc_now

PHASE3AH_R3_VERSION = "phase3ah_r3_sports_provenance_repair_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3ah_r3")
DEFAULT_REPORTS_DIR = Path("reports")


@dataclass(frozen=True)
class Phase3AHR3ArtifactSet:
    output_dir: Path
    executive_summary_path: Path
    json_path: Path
    rows_path: Path
    next_actions_path: Path
    next_codex_task_path: Path
    manifest_path: Path


@dataclass(frozen=True)
class Phase3AHR3ExpansionArtifactSet:
    output_dir: Path
    executive_summary_path: Path
    json_path: Path
    expansion_markdown_path: Path
    expansion_json_path: Path
    rows_path: Path
    next_actions_path: Path
    next_codex_task_path: Path
    manifest_path: Path


def build_phase3ah_r3_sports_provenance_repair(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    sample_limit: int = 25,
    max_rows: int | None = 1000,
    ticker_prefix: str | None = None,
    registered_commands: set[str] | None = None,
) -> dict[str, Any]:
    source = build_phase3z_r2_sports_provenance_repair(
        session,
        reports_dir=reports_dir,
        settings=settings or get_settings(),
        sample_limit=sample_limit,
        max_rows=max_rows,
        ticker_prefix=ticker_prefix,
    )
    summary = _summary(source)
    command_audit = _command_audit(registered_commands or set(), summary=summary)
    next_codex_task = _next_codex_task(summary)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AH-R3",
        "phase_version": PHASE3AH_R3_VERSION,
        "mode": "PAPER_ONLY_READ_ONLY_SPORTS_PROVENANCE_REPAIR",
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "verified_link_auto_upgrades": False,
        "fabricated_evidence": False,
        "summary": summary,
        "phase3z_r2_source_summary": source.get("summary", {}),
        "phase3z_r2_gate": source.get("phase3ae_gate", {}),
        "row_scan": source.get("row_scan", {}),
        "grouped_degraded_links": source.get("grouped_degraded_links", []),
        "sports_provenance_repair_rows": source.get("degraded_rows", []),
        "safe_repair_rows": [
            row for row in source.get("degraded_rows", []) if row.get("safe_to_repair")
        ],
        "blocked_rows_sample": [
            row for row in source.get("degraded_rows", []) if not row.get("safe_to_repair")
        ][:sample_limit],
        "command_registry_audit": command_audit,
        "next_actions": _registered_next_actions(command_audit, summary),
        "next_codex_task": next_codex_task,
        "operator_do_not_run": [
            "Do not auto-upgrade sports links from placeholder or partial evidence.",
            "Do not run Phase 3AE unless this report has safe repair rows.",
            "Do not submit, cancel, replace, or amend live/demo exchange orders.",
            "Do not create paper trades from provenance diagnostics.",
        ],
    }


def write_phase3ah_r3_sports_provenance_repair_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    sample_limit: int = 25,
    max_rows: int | None = 1000,
    ticker_prefix: str | None = None,
    registered_commands: set[str] | None = None,
) -> Phase3AHR3ArtifactSet:
    payload = build_phase3ah_r3_sports_provenance_repair(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        sample_limit=sample_limit,
        max_rows=max_rows,
        ticker_prefix=ticker_prefix,
        registered_commands=registered_commands,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    json_path = output_dir / "sports_provenance_repair.json"
    rows_path = output_dir / "sports_provenance_repair_rows.csv"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    next_codex_task_path = output_dir / "NEXT_CODEX_TASK.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    _write_json(json_path, payload)
    _write_rows_csv(rows_path, payload["sports_provenance_repair_rows"])
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    next_codex_task_path.write_text(_render_next_codex_task(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            json_path,
            rows_path,
            next_actions_path,
            next_codex_task_path,
        ],
    )
    return Phase3AHR3ArtifactSet(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        json_path=json_path,
        rows_path=rows_path,
        next_actions_path=next_actions_path,
        next_codex_task_path=next_codex_task_path,
        manifest_path=manifest_path,
    )


def write_phase3ah_r3_bounded_scan_expansion_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    sample_limit: int = 25,
    max_rows: int | None = 7500,
    ticker_prefix: str | None = None,
    registered_commands: set[str] | None = None,
) -> Phase3AHR3ExpansionArtifactSet:
    payload = build_phase3ah_r3_sports_provenance_repair(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        sample_limit=sample_limit,
        max_rows=max_rows,
        ticker_prefix=ticker_prefix,
        registered_commands=registered_commands,
    )
    payload["phase"] = "3AH-R3-BOUNDED-SCAN-EXPANSION"
    payload["mode"] = "PAPER_ONLY_READ_ONLY_SPORTS_PROVENANCE_BOUNDED_SCAN_EXPANSION"
    payload["summary"]["bounded_scan_expansion"] = True
    payload["summary"]["bounded_scan_expansion_default_max_rows"] = max_rows

    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    json_path = output_dir / "sports_provenance_repair.json"
    expansion_markdown_path = output_dir / "bounded_scan_expansion.md"
    expansion_json_path = output_dir / "bounded_scan_expansion.json"
    rows_path = output_dir / "sports_provenance_repair_rows.csv"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    next_codex_task_path = output_dir / "NEXT_CODEX_TASK.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    _write_json(json_path, payload)
    _write_json(expansion_json_path, payload)
    _write_rows_csv(rows_path, payload["sports_provenance_repair_rows"])
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    expansion_markdown_path.write_text(
        _render_bounded_scan_expansion(payload),
        encoding="utf-8",
    )
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    next_codex_task_path.write_text(_render_next_codex_task(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            json_path,
            expansion_markdown_path,
            expansion_json_path,
            rows_path,
            next_actions_path,
            next_codex_task_path,
        ],
    )
    return Phase3AHR3ExpansionArtifactSet(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        json_path=json_path,
        expansion_markdown_path=expansion_markdown_path,
        expansion_json_path=expansion_json_path,
        rows_path=rows_path,
        next_actions_path=next_actions_path,
        next_codex_task_path=next_codex_task_path,
        manifest_path=manifest_path,
    )


def _summary(source: dict[str, Any]) -> dict[str, Any]:
    source_summary = source.get("summary") if isinstance(source.get("summary"), dict) else {}
    gate = source.get("phase3ae_gate") if isinstance(source.get("phase3ae_gate"), dict) else {}
    scan = source.get("row_scan") if isinstance(source.get("row_scan"), dict) else {}
    safe_rows = _int_value(source_summary.get("rows_safe_to_repair"))
    scan_complete = bool(source_summary.get("row_scan_complete"))
    first_blocker = _first_blocker(source_summary, gate)
    return {
        "status": _status(safe_rows=safe_rows, scan_complete=scan_complete),
        "rows_reviewed": _int_value(source_summary.get("rows_reviewed")),
        "candidate_degraded_rows": _int_value(source_summary.get("candidate_degraded_rows")),
        "rows_safe_to_repair": safe_rows,
        "rows_blocked": _int_value(source_summary.get("rows_blocked")),
        "placeholder_blocked_rows": _int_value(source_summary.get("placeholder_blocked_rows")),
        "partial_legacy_markets": _int_value(source_summary.get("partial_legacy_markets")),
        "partial_legacy_link_rows": _int_value(source_summary.get("partial_legacy_link_rows")),
        "unlinked_parsed_markets": _int_value(source_summary.get("unlinked_parsed_markets")),
        "verified_schedule_markets": _int_value(source_summary.get("verified_schedule_markets")),
        "verified_schedule_link_rows": _int_value(
            source_summary.get("verified_schedule_link_rows")
        ),
        "row_scan_complete": scan_complete,
        "row_scan_truncated": bool(source_summary.get("row_scan_truncated")),
        "row_scan_max_rows": source_summary.get("row_scan_max_rows"),
        "source_row_scan": scan,
        "phase3ae_gate_status": gate.get("status") or "UNKNOWN",
        "phase3ae_can_run_from_this_report": bool(gate.get("phase3ae_can_run_from_this_report")),
        "auto_upgrades_created": 0,
        "first_hard_blocker": first_blocker,
        "implementation_needed": not scan_complete or safe_rows > 0,
        "sports_r3_completed_without_safe_rows": scan_complete and safe_rows == 0,
        "paper_trade_creation": False,
        "live_or_demo_execution": False,
        "verified_link_auto_upgrades": False,
    }


def _status(*, safe_rows: int, scan_complete: bool) -> str:
    if safe_rows > 0:
        return "SAFE_ROWS_REQUIRE_PHASE3AE_REVIEW"
    if not scan_complete:
        return "BOUNDED_SCAN_INCOMPLETE"
    return "NO_SAFE_SPORTS_REPAIR_ROWS"


def _first_blocker(summary: dict[str, Any], gate: dict[str, Any]) -> str:
    if _int_value(summary.get("rows_safe_to_repair")) > 0:
        return "SAFE_SPORTS_REPAIR_ROWS_REQUIRE_REVIEW"
    if bool(summary.get("row_scan_truncated")):
        return "HOLD_BOUNDED_SCAN_INCOMPLETE"
    if _int_value(summary.get("placeholder_blocked_rows")) > 0:
        return "HOLD_PLACEHOLDER_UPGRADES"
    if _int_value(summary.get("partial_legacy_markets")) > 0:
        return "HOLD_PARTIAL_PROVENANCE"
    if _int_value(summary.get("unlinked_parsed_markets")) > 0:
        return "HOLD_UNLINKED_SPORTS_MARKETS"
    return str(gate.get("status") or "NO_SAFE_SPORTS_REPAIR_ROWS")


def _next_codex_task(summary: dict[str, Any]) -> dict[str, Any]:
    if int(summary.get("rows_safe_to_repair") or 0) > 0:
        phase = "Phase 3AE Verified Sports Connector"
        reason = "Phase 3AH-R3 found sports rows with clean repair evidence."
        problem = "Apply only exact schedule/roster-backed sports upgrades."
    elif not summary.get("row_scan_complete"):
        phase = "Phase 3AH-R3 Sports Provenance Bounded Scan Expansion"
        reason = "The bounded scan ended before all degraded sports rows were reviewed."
        problem = "Expand or focus the read-only scan before concluding no safe rows exist."
    else:
        phase = "Phase 3AN Economic/News Compatibility Watch"
        reason = (
            "Sports R3 completed without safe repair rows, so the next code slice "
            "is econ/news watch alignment."
        )
        problem = (
            "Keep economic/news compatibility report-only until matching active markets exist."
        )
    return {
        "task_phase_name": phase,
        "reason": reason,
        "problem_statement": problem,
        "acceptance_criteria": [
            "Keep everything PAPER / READ-ONLY.",
            "Do not submit/cancel/replace/amend live or demo exchange orders.",
            "Do not create paper trades from diagnostics.",
            "Do not fabricate sports provenance or force unsafe link upgrades.",
        ],
        "estimated_risk_level": "MEDIUM",
    }


def _command_audit(
    registered_commands: set[str],
    *,
    summary: dict[str, Any],
) -> dict[str, Any]:
    commands = [
        (
            "kalshi-bot phase3ah-r3-sports-provenance-repair "
            "--output-dir reports/phase3ah_r3 --reports-dir reports --max-rows 1000"
        ),
        (
            "kalshi-bot phase3ah-r3-bounded-scan-expansion "
            "--output-dir reports/phase3ah_r3 --reports-dir reports --max-rows 7500"
        ),
        (
            "kalshi-bot phase3z-r2-sports-provenance-repair "
            "--output-dir reports/phase3z_r2 --max-rows 1000"
        ),
        "kalshi-bot phase3ax-gap-analysis --output-dir reports/phase3ax --reports-dir reports",
        (
            "kalshi-bot phase-orchestrator --analyze --output reports/phase_orchestrator.md "
            "--json-output reports/phase_orchestrator.json --next-prompt prompts/next_phase.md"
        ),
    ]
    if int(summary.get("rows_safe_to_repair") or 0) > 0:
        commands.append(
            "kalshi-bot phase3ae-verified-sports-connector --output-dir reports/phase3ae"
        )
    rows = []
    for command in commands:
        name = _command_name(command)
        rows.append(
            {
                "command": name,
                "full_command": command,
                "registered": name in registered_commands,
                "included_in_next_actions": name in registered_commands,
            }
        )
    return {
        "candidate_commands": rows,
        "missing_command_names": [row["command"] for row in rows if not row["registered"]],
        "next_actions_reference_only_registered_commands": True,
    }


def _registered_next_actions(
    command_audit: dict[str, Any],
    summary: dict[str, Any],
) -> list[str]:
    commands = [
        str(row["full_command"])
        for row in command_audit["candidate_commands"]
        if row.get("registered")
    ]
    if int(summary.get("rows_safe_to_repair") or 0) > 0:
        return commands
    if not summary.get("row_scan_complete"):
        return [
            command
            for command in commands
            if "phase3ah-r3-sports-provenance-repair" in command
            or "phase3ah-r3-bounded-scan-expansion" in command
            or "phase3z-r2-sports-provenance-repair" in command
        ]
    return [
        command
        for command in commands
        if "phase3ax-gap-analysis" in command or "phase-orchestrator" in command
    ]


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    return "\n".join(
        [
            "# Phase 3AH-R3 Sports Provenance Repair",
            "",
            f"- Generated at: `{payload['generated_at']}`",
            f"- Mode: `{payload['mode']}`",
            f"- Status: `{summary['status']}`",
            f"- Rows reviewed: `{summary['rows_reviewed']}`",
            f"- Safe repair rows: `{summary['rows_safe_to_repair']}`",
            f"- Blocked rows: `{summary['rows_blocked']}`",
            f"- First hard blocker: `{summary['first_hard_blocker']}`",
            "",
            "## Evidence",
            "",
            f"- Placeholder-blocked rows: `{summary['placeholder_blocked_rows']}`",
            f"- Partial legacy markets: `{summary['partial_legacy_markets']}`",
            f"- Unlinked parsed markets: `{summary['unlinked_parsed_markets']}`",
            f"- Row scan complete: `{summary['row_scan_complete']}`",
            f"- Phase 3AE gate: `{summary['phase3ae_gate_status']}`",
            "",
            "No verified link upgrades, paper trades, or live/demo writes were created.",
            "",
        ]
    )


def _render_bounded_scan_expansion(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    return "\n".join(
        [
            "# Phase 3AH-R3 Sports Provenance Bounded Scan Expansion",
            "",
            f"- Generated at: `{payload['generated_at']}`",
            f"- Mode: `{payload['mode']}`",
            f"- Status: `{summary['status']}`",
            f"- Rows reviewed: `{summary['rows_reviewed']}`",
            f"- Row scan max rows: `{summary['row_scan_max_rows']}`",
            f"- Row scan complete: `{summary['row_scan_complete']}`",
            f"- Safe repair rows: `{summary['rows_safe_to_repair']}`",
            f"- Blocked rows: `{summary['rows_blocked']}`",
            f"- First hard blocker: `{summary['first_hard_blocker']}`",
            f"- Next Codex task: `{payload['next_codex_task']['task_phase_name']}`",
            "",
            "## Safety",
            "",
            "- No verified link upgrades were applied.",
            "- No paper trades were created.",
            "- No live/demo exchange writes were attempted.",
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AH-R3 Next Actions",
        "",
        f"- First hard blocker: `{payload['summary']['first_hard_blocker']}`",
        f"- Next Codex task: `{payload['next_codex_task']['task_phase_name']}`",
        "",
        "## Registered Commands",
        "",
    ]
    if payload["next_actions"]:
        lines.extend(f"- `{command}`" for command in payload["next_actions"])
    else:
        lines.append("- No registered command recommendations are available.")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Do not auto-upgrade placeholder or partial sports provenance.",
            "- Do not create paper trades from diagnostics.",
            "- Do not run live/demo exchange writes.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_next_codex_task(payload: dict[str, Any]) -> str:
    task = payload["next_codex_task"]
    lines = [
        "# Phase 3AH-R3 Next Codex Task",
        "",
        f"Task phase name: `{task['task_phase_name']}`",
        "",
        f"Reason: {task['reason']}",
        "",
        f"Problem statement: {task['problem_statement']}",
        "",
        "Acceptance criteria:",
    ]
    lines.extend(f"- {item}" for item in task["acceptance_criteria"])
    lines.append("")
    return "\n".join(lines)


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "ticker",
        "source_kind",
        "reason_code",
        "league",
        "market_type",
        "placeholder_involved",
        "safe_to_repair",
        "blocked_reasons",
        "game_key",
        "scheduled_at",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(row.get(key), sort_keys=True)
                    if key == "blocked_reasons"
                    else row.get(key)
                    for key in fieldnames
                }
            )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for artifact in files:
        if artifact.exists():
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            lines.append(f"{digest}  {artifact.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _command_name(command: str) -> str:
    parts = command.split()
    return parts[1] if len(parts) > 1 and parts[0] == "kalshi-bot" else parts[0]


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
