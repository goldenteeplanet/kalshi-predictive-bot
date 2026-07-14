from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE3AX_R6_VERSION = "phase3ax_r6_sports_provenance_repair_v1"


@dataclass(frozen=True)
class Phase3AXR6ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    safe_rows_path: Path | None = None


def write_phase3an_sports_blocker_report(
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
) -> Phase3AXR6ArtifactSet:
    payload = build_phase3an_sports_blocker_report(reports_dir=reports_dir)
    return _write_report(
        payload,
        output_dir=output_dir,
        json_name="phase3an_sports_blocker_report.json",
        markdown_name="phase3an_sports_blocker_report.md",
    )


def write_phase3aw_dashboard_truth_report(
    *,
    output_dir: Path = Path("reports/phase3aw"),
    reports_dir: Path = Path("reports"),
    stale_after_minutes: int = 120,
) -> Phase3AXR6ArtifactSet:
    payload = build_phase3aw_dashboard_truth_report(
        reports_dir=reports_dir,
        stale_after_minutes=stale_after_minutes,
    )
    return _write_report(
        payload,
        output_dir=output_dir,
        json_name="phase3aw_dashboard_truth.json",
        markdown_name="phase3aw_dashboard_truth.md",
    )


def write_phase3ax_gap_analysis_report(
    *,
    output_dir: Path = Path("reports/phase3ax"),
    reports_dir: Path = Path("reports"),
    stale_after_minutes: int = 120,
) -> Phase3AXR6ArtifactSet:
    payload = build_phase3ax_gap_analysis(
        reports_dir=reports_dir,
        stale_after_minutes=stale_after_minutes,
    )
    return _write_report(
        payload,
        output_dir=output_dir,
        json_name="phase3ax_gap_analysis.json",
        markdown_name="phase3ax_gap_analysis.md",
        safe_rows_name="safe_sports_provenance_repair_rows.json",
        safe_rows=payload["safe_repair_rows"],
    )


def build_phase3an_sports_blocker_report(
    *, reports_dir: Path = Path("reports")
) -> dict[str, Any]:
    inputs = _load_inputs(reports_dir)
    rows = _load_rows(reports_dir)
    classification = _classify_rows(rows)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AN/3AX-R6",
        "phase_version": PHASE3AX_R6_VERSION,
        "mode": "PAPER_ONLY_READ_ONLY_SPORTS_BLOCKER_DIAGNOSTIC",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "input_reports": _input_paths(reports_dir),
        "summary": {
            **_input_summary(inputs),
            **classification["summary"],
        },
        "first_blocker": classification["first_blocker"],
        "diagnostic_groups": classification["diagnostic_groups"],
        "next_actions": _registered_next_actions(),
    }


def build_phase3aw_dashboard_truth_report(
    *,
    reports_dir: Path = Path("reports"),
    stale_after_minutes: int = 120,
) -> dict[str, Any]:
    inputs = _load_inputs(reports_dir)
    generated_at = utc_now()
    freshness = {
        name: _freshness_row(
            name=name,
            path=path,
            payload=inputs.get(name),
            now=generated_at,
            stale_after_minutes=stale_after_minutes,
        )
        for name, path in _input_paths(reports_dir).items()
    }
    stale = [row for row in freshness.values() if row["status"] != "FRESH"]
    return {
        "generated_at": generated_at.isoformat(),
        "phase": "3AW/3AX-R6",
        "phase_version": PHASE3AX_R6_VERSION,
        "mode": "REPORT_ONLY_DASHBOARD_TRUTH",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "stale_after_minutes": stale_after_minutes,
        "summary": {
            "reports_checked": len(freshness),
            "fresh_reports": len(freshness) - len(stale),
            "stale_or_missing_reports": len(stale),
            "dashboard_truth_status": "STALE_INPUTS_PRESENT" if stale else "REPORT_FRESH",
        },
        "report_freshness": freshness,
        "next_actions": _registered_next_actions(),
    }


def build_phase3ax_gap_analysis(
    *,
    reports_dir: Path = Path("reports"),
    stale_after_minutes: int = 120,
) -> dict[str, Any]:
    inputs = _load_inputs(reports_dir)
    rows = _load_rows(reports_dir)
    classification = _classify_rows(rows)
    safe_rows = classification["safe_repair_rows"]
    gate = "READY_SAFE_EXACT_REPAIR_ROWS" if safe_rows else "HOLD_DIAGNOSTIC_ONLY"
    first_blocker = classification["first_blocker"]
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AX-R6",
        "phase_version": PHASE3AX_R6_VERSION,
        "mode": "PAPER_ONLY_READ_ONLY_EXACT_SPORTS_PROVENANCE_GAP_ANALYSIS",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "safety_policy": {
            "live_or_demo_orders": "blocked",
            "paper_trade_creation_from_sports_diagnostics": "blocked",
            "threshold_lowering": "blocked",
            "fabricated_evidence": "blocked",
            "sibling_fuzzy_component_matching": "blocked",
        },
        "input_reports": _input_paths(reports_dir),
        "stale_after_minutes": stale_after_minutes,
        "summary": {
            **_input_summary(inputs),
            **classification["summary"],
            "phase3ax_r6_gate": gate,
            "safe_rows_file_created": bool(safe_rows),
        },
        "first_blocker": first_blocker,
        "safe_repair_rows": safe_rows,
        "diagnostic_only_rows": classification["diagnostic_only_rows"],
        "diagnostic_groups": classification["diagnostic_groups"],
        "next_actions": _registered_next_actions(),
        "recommended_next_action": (
            "Review safe_sports_provenance_repair_rows.json before any apply step."
            if safe_rows
            else f"Keep rows diagnostic-only; first blocker is {first_blocker['reason']}."
        ),
    }


def _write_report(
    payload: dict[str, Any],
    *,
    output_dir: Path,
    json_name: str,
    markdown_name: str,
    safe_rows_name: str | None = None,
    safe_rows: list[dict[str, Any]] | None = None,
) -> Phase3AXR6ArtifactSet:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / json_name
    markdown_path = output_dir / markdown_name
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    safe_rows_path = None
    if safe_rows_name and safe_rows:
        safe_rows_path = output_dir / safe_rows_name
        safe_rows_path.write_text(
            json.dumps(safe_rows, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
    return Phase3AXR6ArtifactSet(output_dir, json_path, markdown_path, safe_rows_path)


def _load_inputs(reports_dir: Path) -> dict[str, dict[str, Any] | None]:
    return {name: _load_json(path) for name, path in _input_paths(reports_dir).items()}


def _input_paths(reports_dir: Path) -> dict[str, Path]:
    return {
        "phase3ah_evidence": reports_dir
        / "phase3ah_sports"
        / "phase3ah_sports_evidence_backfill.json",
        "phase3ah_placeholder_watch": reports_dir
        / "phase3ah_sports"
        / "phase3ah_sports_placeholder_watch.json",
        "phase3z_r2_repair": reports_dir
        / "phase3z_r2"
        / "phase3z_r2_sports_provenance_repair.json",
        "phase3z_r2_rows": reports_dir / "phase3z_r2" / "sports_provenance_repair_rows.json",
        "phase3az_gap_analysis": reports_dir / "phase3az" / "phase3az_gap_analysis.json",
    }


def _load_rows(reports_dir: Path) -> list[dict[str, Any]]:
    payload = _load_json(_input_paths(reports_dir)["phase3z_r2_rows"])
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def _load_json(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, (dict, list)) else None


def _input_summary(inputs: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    evidence = _summary(inputs.get("phase3ah_evidence"))
    placeholder = _summary(inputs.get("phase3ah_placeholder_watch"))
    repair = _summary(inputs.get("phase3z_r2_repair"))
    return {
        "phase3ah_phase3ae_ready_rows": int(evidence.get("phase3ae_ready_rows") or 0),
        "phase3ah_auto_upgrades_created": int(evidence.get("auto_upgrades_created") or 0),
        "phase3ah_placeholder_safe_to_apply_rows": int(
            placeholder.get("safe_to_apply_rows") or 0
        ),
        "phase3ah_still_placeholder_rows": int(placeholder.get("still_placeholder_rows") or 0),
        "phase3z_rows_safe_to_repair": int(repair.get("rows_safe_to_repair") or 0),
        "phase3z_rows_blocked": int(repair.get("rows_blocked") or 0),
        "phase3z_row_scan_truncated": bool(repair.get("row_scan_truncated")),
    }


def _summary(payload: dict[str, Any] | list[Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    summary = payload.get("summary")
    return summary if isinstance(summary, dict) else {}


def _classify_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    safe_rows = [_safe_projection(row) for row in rows if _is_exact_safe_row(row)]
    diagnostic_rows = [_diagnostic_projection(row) for row in rows if not _is_exact_safe_row(row)]
    blocker_counts: Counter[str] = Counter()
    for row in diagnostic_rows:
        blocker_counts.update(row["blocked_reasons"] or ["UNKNOWN_BLOCKER"])
    groups = [
        {"reason": reason, "count": count}
        for reason, count in sorted(blocker_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    first_blocker = _first_blocker(diagnostic_rows, rows)
    return {
        "summary": {
            "rows_reviewed": len(rows),
            "safe_exact_repair_rows": len(safe_rows),
            "diagnostic_only_rows": len(diagnostic_rows),
            "placeholder_rows": sum(1 for row in rows if row.get("placeholder_involved")),
            "ambiguous_team_rows": sum(1 for row in rows if not row.get("clean_team_identity")),
            "partial_provenance_rows": sum(
                1 for row in rows if row.get("source_kind") == "PARTIAL_LEGACY_IDENTIFIER"
            ),
            "stale_or_missing_schedule_rows": sum(
                1
                for row in rows
                if not str(row.get("available_schedule_evidence") or "").startswith(
                    "verified_schedule"
                )
            ),
            "player_prop_roster_required_rows": sum(
                1 for row in rows if row.get("player_prop_requires_roster")
            ),
        },
        "safe_repair_rows": safe_rows,
        "diagnostic_only_rows": diagnostic_rows[:100],
        "diagnostic_groups": groups,
        "first_blocker": first_blocker,
    }


def _is_exact_safe_row(row: dict[str, Any]) -> bool:
    if not row.get("safe_to_repair"):
        return False
    if row.get("blocked_reasons"):
        return False
    return (
        str(row.get("available_schedule_evidence") or "").startswith("verified_schedule")
        and bool(row.get("clean_team_identity"))
        and bool(row.get("clean_time"))
        and bool(row.get("clean_market_type"))
        and not bool(row.get("placeholder_involved"))
        and not bool(row.get("player_prop_requires_roster"))
        and not bool(row.get("unsupported_multi_leg"))
        and not bool(row.get("cross_category"))
    )


def _safe_projection(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in [
            "ticker",
            "league",
            "market_type",
            "game_key",
            "scheduled_at",
            "home_team_key",
            "away_team_key",
            "available_schedule_evidence",
            "link_row_ids",
        ]
    }


def _diagnostic_projection(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": row.get("ticker"),
        "source_kind": row.get("source_kind"),
        "reason_code": row.get("reason_code"),
        "available_schedule_evidence": row.get("available_schedule_evidence"),
        "placeholder_involved": bool(row.get("placeholder_involved")),
        "clean_team_identity": bool(row.get("clean_team_identity")),
        "clean_time": bool(row.get("clean_time")),
        "clean_market_type": bool(row.get("clean_market_type")),
        "player_prop_requires_roster": bool(row.get("player_prop_requires_roster")),
        "unsupported_multi_leg": bool(row.get("unsupported_multi_leg")),
        "cross_category": bool(row.get("cross_category")),
        "blocked_reasons": list(row.get("blocked_reasons") or []),
    }


def _first_blocker(
    diagnostic_rows: list[dict[str, Any]], raw_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    if not raw_rows:
        return {"reason": "NO_ROW_DIAGNOSTICS", "ticker": None}
    if not diagnostic_rows:
        return {"reason": "NONE", "ticker": None}
    first = diagnostic_rows[0]
    reasons = first.get("blocked_reasons") or ["UNKNOWN_BLOCKER"]
    return {
        "reason": reasons[0],
        "ticker": first.get("ticker"),
        "all_reasons": reasons,
    }


def _freshness_row(
    *,
    name: str,
    path: Path,
    payload: dict[str, Any] | list[Any] | None,
    now: datetime,
    stale_after_minutes: int,
) -> dict[str, Any]:
    generated = payload.get("generated_at") if isinstance(payload, dict) else None
    parsed = _parse_time(generated)
    if payload is None:
        status = "MISSING"
        age_minutes = None
    elif parsed is None:
        status = "UNKNOWN_GENERATED_AT"
        age_minutes = None
    else:
        age_minutes = max((now - parsed).total_seconds() / 60, 0)
        status = "STALE" if age_minutes > stale_after_minutes else "FRESH"
    return {
        "name": name,
        "path": str(path),
        "exists": path.exists(),
        "generated_at": generated,
        "age_minutes": round(age_minutes, 2) if age_minutes is not None else None,
        "status": status,
    }


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _registered_next_actions() -> list[str]:
    return [
        "kalshi-bot phase3ah-schedule-roster-evidence --output-dir reports/phase3ah_sports --limit 0",
        "kalshi-bot phase3ah-sports-placeholder-watch --output-dir reports/phase3ah_sports",
        "kalshi-bot phase3z-r2-sports-provenance-repair --output-dir reports/phase3z_r2 --reports-dir reports --max-rows 1000",
        "kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az --reports-dir reports",
        "kalshi-bot phase3an-sports-blocker-report --output-dir reports/phase3an --reports-dir reports",
        "kalshi-bot phase3aw-dashboard-truth --output-dir reports/phase3aw --reports-dir reports --stale-after-minutes 120",
        "kalshi-bot phase3ax-gap-analysis --output-dir reports/phase3ax --reports-dir reports --stale-after-minutes 120",
    ]


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        f"# {payload.get('phase')} Sports Provenance Report",
        "",
        f"- Generated: {payload.get('generated_at')}",
        f"- Mode: {payload.get('mode')}",
        f"- Safety: {payload.get('paper_only_safety')}",
        "",
        "## Summary",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    if payload.get("first_blocker"):
        lines.extend(["", "## First Blocker", ""])
        first = payload["first_blocker"]
        lines.append(f"- reason: {first.get('reason')}")
        lines.append(f"- ticker: {first.get('ticker')}")
        if first.get("all_reasons"):
            lines.append(f"- all_reasons: {', '.join(first['all_reasons'])}")
    if payload.get("diagnostic_groups"):
        lines.extend(["", "## Diagnostic Groups", ""])
        for group in payload["diagnostic_groups"][:20]:
            lines.append(f"- {group.get('reason')}: {group.get('count')}")
    if payload.get("next_actions"):
        lines.extend(["", "## NEXT_ACTIONS", ""])
        for action in payload["next_actions"]:
            lines.append(f"- `{action}`")
    if payload.get("recommended_next_action"):
        lines.extend(["", "## Recommended Next Action", "", str(payload["recommended_next_action"])])
    lines.append("")
    return "\n".join(lines)
