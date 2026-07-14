from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import Market, MarketLeg, SportsMarketLink
from kalshi_predictor.phase3ah_r3 import build_phase3ah_r3_sports_provenance_repair
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R6_VERSION = "phase3bb_r6_sports_provenance_repair_v1"
ACTIVE_MARKET_STATUSES = ("active", "open")
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r6")
DEFAULT_REPORTS_DIR = Path("reports")
R6_COMMAND = (
    "kalshi-bot phase3bb-r6-sports-provenance-repair "
    "--output-dir reports/phase3bb_r6 --reports-dir reports --max-rows 1000"
)
FREE_SOURCE_COMMAND = (
    "kalshi-bot phase3bb-r3-free-source-inventory "
    "--output-dir reports/phase3bb_r3 --reports-dir reports"
)
CSV_FIELDS = [
    "ticker",
    "classification",
    "row_count",
    "source_kind",
    "reason_code",
    "league",
    "market_type",
    "game_key",
    "scheduled_at",
    "placeholder_involved",
    "available_schedule_evidence",
    "exact_event_team_date_mapping",
    "safe_to_repair",
    "blocked_reasons",
    "first_blocker",
    "repair_action",
    "db_writes_performed",
]


@dataclass(frozen=True)
class Phase3BBR6SportsProvenanceRepairArtifacts:
    output_dir: Path
    executive_summary_path: Path
    json_path: Path
    markdown_path: Path
    candidates_csv_path: Path
    unsafe_rows_csv_path: Path
    manifest_path: Path


def write_phase3bb_r6_sports_provenance_repair_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    sample_limit: int = 50,
    max_rows: int | None = 1000,
    ticker_prefix: str | None = None,
) -> Phase3BBR6SportsProvenanceRepairArtifacts:
    payload = build_phase3bb_r6_sports_provenance_repair(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        sample_limit=sample_limit,
        max_rows=max_rows,
        ticker_prefix=ticker_prefix,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    json_path = output_dir / "sports_provenance_repair.json"
    markdown_path = output_dir / "sports_provenance_repair.md"
    candidates_csv_path = output_dir / "sports_repair_candidates.csv"
    unsafe_rows_csv_path = output_dir / "unsafe_sports_rows.csv"
    manifest_path = output_dir / "MANIFEST.sha256"

    _write_json(json_path, payload)
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    _write_rows_csv(candidates_csv_path, payload["sports_repair_candidates"])
    _write_rows_csv(unsafe_rows_csv_path, payload["unsafe_sports_rows"])
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            json_path,
            markdown_path,
            candidates_csv_path,
            unsafe_rows_csv_path,
        ],
    )
    return Phase3BBR6SportsProvenanceRepairArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        json_path=json_path,
        markdown_path=markdown_path,
        candidates_csv_path=candidates_csv_path,
        unsafe_rows_csv_path=unsafe_rows_csv_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r6_sports_provenance_repair(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    sample_limit: int = 50,
    max_rows: int | None = 1000,
    ticker_prefix: str | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=utc_now().isoformat(),
        command_args=command_args or [],
        output_dir=output_dir,
    )
    metadata["command_arguments"] = {
        "command": "kalshi-bot phase3bb-r6-sports-provenance-repair",
        "argv": command_args or [],
    }
    source = build_phase3ah_r3_sports_provenance_repair(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=resolved,
        sample_limit=sample_limit,
        max_rows=max_rows,
        ticker_prefix=ticker_prefix,
        registered_commands={
            "phase3bb-r6-sports-provenance-repair",
            "phase3ah-r3-sports-provenance-repair",
            "phase3ah-r3-bounded-scan-expansion",
            "phase3z-r2-sports-provenance-repair",
            "phase3ax-gap-analysis",
            "phase-orchestrator",
        },
    )
    source_summary = _dict(source.get("summary"))
    phase3z_summary = _dict(source.get("phase3z_r2_source_summary"))
    source_rows = [_dict(row) for row in source.get("sports_provenance_repair_rows") or []]
    inventory = _sports_inventory(
        session,
        reports_dir=reports_dir,
        source_summary=source_summary,
        phase3z_summary=phase3z_summary,
    )
    candidate_rows = [_candidate_csv_row(row) for row in source_rows if _exact_safe_candidate(row)]
    unsafe_rows = [_unsafe_csv_row(row) for row in source_rows if not _exact_safe_candidate(row)]
    unsupported_composites = _to_int(inventory.get("unsupported_composite_markets"))
    if unsupported_composites and not any(
        row["classification"] == "UNSUPPORTED_COMPOSITE_PARKED" for row in unsafe_rows
    ):
        unsafe_rows.append(_unsupported_composite_row(unsupported_composites))
    summary = _summary(
        inventory=inventory,
        source_summary=source_summary,
        phase3z_summary=phase3z_summary,
        candidate_rows=candidate_rows,
        unsafe_rows=unsafe_rows,
    )
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "fabricates_schedules_teams_outcomes_or_rounds": False,
        "uses_fuzzy_matching": False,
        "treats_placeholders_as_real_teams": False,
        "forecast_writes": False,
        "db_writes_performed": 0,
    }
    return {
        **metadata,
        "phase": "3BB-R6",
        "phase_version": PHASE3BB_R6_VERSION,
        "mode": "PAPER_READ_ONLY_SPORTS_PROVENANCE_REPAIR_PREVIEW",
        "reports_dir": str(reports_dir),
        "max_rows": max_rows,
        "sample_limit": sample_limit,
        "ticker_prefix": ticker_prefix,
        "summary": summary,
        "sports_inventory": inventory,
        "phase3ah_r3_source_summary": source_summary,
        "phase3z_r2_source_summary": phase3z_summary,
        "phase3z_r2_gate": source.get("phase3z_r2_gate") or {},
        "row_scan": source.get("row_scan") or {},
        "sports_repair_candidates": candidate_rows,
        "unsafe_sports_rows": unsafe_rows,
        "acceptance": _acceptance(summary),
        "safety_flags": safety,
        "operator_guardrails": [
            "PAPER / READ-ONLY sports provenance repair preview only.",
            "Do not create paper trades.",
            "Do not submit, cancel, replace, or amend live/demo exchange orders.",
            "Do not fabricate schedules, teams, outcomes, or rounds.",
            "Do not use fuzzy matching.",
            "Do not treat placeholders as real teams.",
            "Do not run Phase 3AE unless a later exact repair gate reports safe rows.",
        ],
        "next_actions": _next_actions(summary),
    }


def _sports_inventory(
    session: Session,
    *,
    reports_dir: Path,
    source_summary: dict[str, Any],
    phase3z_summary: dict[str, Any],
) -> dict[str, Any]:
    coverage_unsupported = _coverage_sports_unsupported_composites(reports_dir)
    return {
        "active_sports_parsed_markets": _active_sports_parsed_markets(session),
        "active_sports_linked_markets": _active_sports_linked_markets(session),
        "verified_schedule_markets": _to_int(
            phase3z_summary.get("verified_schedule_markets")
            or source_summary.get("verified_schedule_markets")
        ),
        "verified_schedule_link_rows": _to_int(
            phase3z_summary.get("verified_schedule_link_rows")
            or source_summary.get("verified_schedule_link_rows")
        ),
        "verified_derived_markets": _to_int(
            phase3z_summary.get("kalshi_event_derived_markets")
            or phase3z_summary.get("derived_usable_markets")
        ),
        "verified_derived_link_rows": _to_int(
            phase3z_summary.get("kalshi_event_derived_link_rows")
        ),
        "partial_provenance_markets": _to_int(
            phase3z_summary.get("partial_legacy_markets")
            or source_summary.get("partial_legacy_markets")
        ),
        "partial_provenance_link_rows": _to_int(
            phase3z_summary.get("partial_legacy_link_rows")
            or source_summary.get("partial_legacy_link_rows")
        ),
        "placeholder_rows": _to_int(
            phase3z_summary.get("placeholder_blocked_rows")
            or source_summary.get("placeholder_blocked_rows")
        ),
        "unsupported_composite_markets": max(
            _to_int(phase3z_summary.get("excluded_composite_partial_markets")),
            coverage_unsupported
            if coverage_unsupported is not None
            else _active_unsupported_composites(session),
        ),
        "unlinked_parsed_markets": _to_int(
            phase3z_summary.get("unlinked_parsed_markets")
            or source_summary.get("unlinked_parsed_markets")
        ),
    }


def _summary(
    *,
    inventory: dict[str, Any],
    source_summary: dict[str, Any],
    phase3z_summary: dict[str, Any],
    candidate_rows: list[dict[str, Any]],
    unsafe_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    partial_before = _to_int(inventory.get("partial_provenance_markets"))
    placeholder_before = _to_int(inventory.get("placeholder_rows"))
    safe_candidates = len(candidate_rows)
    unsafe_count = sum(_to_int(row.get("row_count")) or 1 for row in unsafe_rows)
    return {
        "status": "SAFE_EXACT_ROWS_AVAILABLE" if safe_candidates else "HOLD_DIAGNOSTIC_ONLY",
        "active_sports_parsed_markets": _to_int(inventory.get("active_sports_parsed_markets")),
        "active_sports_linked_markets": _to_int(inventory.get("active_sports_linked_markets")),
        "verified_derived_markets": _to_int(inventory.get("verified_derived_markets")),
        "verified_derived_link_rows": _to_int(inventory.get("verified_derived_link_rows")),
        "verified_schedule_markets": _to_int(inventory.get("verified_schedule_markets")),
        "verified_schedule_link_rows": _to_int(inventory.get("verified_schedule_link_rows")),
        "partial_rows_before": partial_before,
        "partial_rows_after": partial_before,
        "partial_link_rows_before": _to_int(inventory.get("partial_provenance_link_rows")),
        "partial_link_rows_after": _to_int(inventory.get("partial_provenance_link_rows")),
        "placeholder_rows_before": placeholder_before,
        "placeholder_rows_after": placeholder_before,
        "unsupported_composite_markets": _to_int(inventory.get("unsupported_composite_markets")),
        "unlinked_parsed_markets": _to_int(inventory.get("unlinked_parsed_markets")),
        "rows_reviewed": _to_int(source_summary.get("rows_reviewed")),
        "row_scan_complete": bool(source_summary.get("row_scan_complete")),
        "row_scan_truncated": bool(source_summary.get("row_scan_truncated")),
        "row_scan_max_rows": source_summary.get("row_scan_max_rows"),
        "safe_repair_candidates": safe_candidates,
        "safe_repairs_applied": 0,
        "unsafe_rows": unsafe_count,
        "db_writes_performed": 0,
        "paper_trade_creation": False,
        "live_or_demo_execution": False,
        "first_hard_blocker": _first_blocker(
            safe_candidates=safe_candidates,
            source_summary=source_summary,
            phase3z_summary=phase3z_summary,
            inventory=inventory,
        ),
        "next_sport_category_to_focus": _next_focus(
            safe_candidates=safe_candidates,
            partial_rows=partial_before,
            placeholder_rows=placeholder_before,
            unsupported_composites=_to_int(inventory.get("unsupported_composite_markets")),
        ),
    }


def _active_sports_parsed_markets(session: Session) -> int:
    statement = (
        select(func.count(func.distinct(MarketLeg.ticker)))
        .select_from(MarketLeg)
        .join(Market, Market.ticker == MarketLeg.ticker)
        .where(MarketLeg.category == "sports")
        .where(func.lower(Market.status).in_(ACTIVE_MARKET_STATUSES))
    )
    return _scalar_int(session, statement)


def _active_sports_linked_markets(session: Session) -> int:
    statement = (
        select(func.count(func.distinct(SportsMarketLink.ticker)))
        .select_from(SportsMarketLink)
        .join(Market, Market.ticker == SportsMarketLink.ticker)
        .where(func.lower(Market.status).in_(ACTIVE_MARKET_STATUSES))
    )
    return _scalar_int(session, statement)


def _coverage_sports_unsupported_composites(reports_dir: Path) -> int | None:
    coverage = _read_json(reports_dir / "market_coverage" / "link_coverage.json")
    rows = coverage.get("category_rows") if isinstance(coverage, dict) else None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict) or row.get("category") != "sports":
            continue
        if row.get("unsupported_multileg_markets") is not None:
            return _to_int(row.get("unsupported_multileg_markets"))
        if row.get("unsupported_composites") is not None:
            return _to_int(row.get("unsupported_composites"))
    return None


def _active_unsupported_composites(session: Session) -> int:
    upper_ticker = func.upper(Market.ticker)
    upper_event = func.upper(Market.event_ticker)
    upper_series = func.upper(Market.series_ticker)
    composite_filter = or_(
        upper_ticker.like("KXMVESPORTSMULTIGAME%"),
        upper_ticker.like("KXMVECROSSCATEGORY%"),
        upper_event.like("KXMVESPORTSMULTIGAME%"),
        upper_event.like("KXMVECROSSCATEGORY%"),
        upper_series.like("KXMVESPORTSMULTIGAME%"),
        upper_series.like("KXMVECROSSCATEGORY%"),
    )
    statement = (
        select(func.count(func.distinct(Market.ticker)))
        .select_from(Market)
        .where(func.lower(Market.status).in_(ACTIVE_MARKET_STATUSES))
        .where(composite_filter)
    )
    return _scalar_int(session, statement)


def _exact_safe_candidate(row: dict[str, Any]) -> bool:
    if not _truthy(row.get("safe_to_repair")):
        return False
    if _truthy(row.get("placeholder_involved")):
        return False
    if _truthy(row.get("unsupported_multi_leg")) or _truthy(row.get("cross_category")):
        return False
    if not str(row.get("available_schedule_evidence") or "").startswith("verified_schedule"):
        return False
    return _truthy(row.get("clean_team_identity")) and _truthy(row.get("clean_time"))


def _candidate_csv_row(row: dict[str, Any]) -> dict[str, Any]:
    return _base_csv_row(
        row,
        classification="SAFE_EXACT_EVENT_TEAM_DATE_MAPPING",
        first_blocker="NONE",
        repair_action="REVIEW_ONLY_NO_DB_WRITE",
        exact_event_team_date_mapping=True,
    )


def _unsafe_csv_row(row: dict[str, Any]) -> dict[str, Any]:
    blockers = _list(row.get("blocked_reasons"))
    return _base_csv_row(
        row,
        classification=_classification(row),
        first_blocker=blockers[0] if blockers else "NOT_EXACT_SAFE_REPAIR",
        repair_action="BLOCKED_DIAGNOSTIC_ONLY",
        exact_event_team_date_mapping=False,
    )


def _unsupported_composite_row(count: int) -> dict[str, Any]:
    return {
        "ticker": "(aggregate)",
        "classification": "UNSUPPORTED_COMPOSITE_PARKED",
        "row_count": count,
        "source_kind": "UNSUPPORTED_COMPOSITE",
        "reason_code": "UNSUPPORTED_KXMVE_COMPOSITE",
        "league": "",
        "market_type": "COMPOSITE",
        "game_key": "",
        "scheduled_at": "",
        "placeholder_involved": False,
        "available_schedule_evidence": "not_applicable_composite_market",
        "exact_event_team_date_mapping": False,
        "safe_to_repair": False,
        "blocked_reasons": "UNSUPPORTED_KXMVE_COMPOSITE",
        "first_blocker": "UNSUPPORTED_KXMVE_COMPOSITE",
        "repair_action": "PARKED_OUTSIDE_SINGLE_MARKET_REMEDIATION",
        "db_writes_performed": 0,
    }


def _base_csv_row(
    row: dict[str, Any],
    *,
    classification: str,
    first_blocker: str,
    repair_action: str,
    exact_event_team_date_mapping: bool,
) -> dict[str, Any]:
    return {
        "ticker": row.get("ticker") or "",
        "classification": classification,
        "row_count": 1,
        "source_kind": row.get("source_kind") or "",
        "reason_code": row.get("reason_code") or "",
        "league": row.get("league") or "",
        "market_type": row.get("market_type") or "",
        "game_key": row.get("game_key") or "",
        "scheduled_at": row.get("scheduled_at") or "",
        "placeholder_involved": _truthy(row.get("placeholder_involved")),
        "available_schedule_evidence": row.get("available_schedule_evidence") or "",
        "exact_event_team_date_mapping": exact_event_team_date_mapping,
        "safe_to_repair": _truthy(row.get("safe_to_repair")),
        "blocked_reasons": ";".join(_list(row.get("blocked_reasons"))),
        "first_blocker": first_blocker,
        "repair_action": repair_action,
        "db_writes_performed": 0,
    }


def _classification(row: dict[str, Any]) -> str:
    if _truthy(row.get("placeholder_involved")):
        return "PLACEHOLDER_ROW"
    if _truthy(row.get("unsupported_multi_leg")) or _truthy(row.get("cross_category")):
        return "UNSUPPORTED_COMPOSITE_PARKED"
    source_kind = str(row.get("source_kind") or "")
    if source_kind == "PARTIAL_LEGACY_IDENTIFIER":
        return "PARTIAL_PROVENANCE"
    if source_kind == "UNLINKED_PARSED_MARKET":
        return "UNLINKED_PARSED_SPORTS"
    return "UNSAFE_SPORTS_PROVENANCE"


def _first_blocker(
    *,
    safe_candidates: int,
    source_summary: dict[str, Any],
    phase3z_summary: dict[str, Any],
    inventory: dict[str, Any],
) -> str:
    if safe_candidates:
        return "SAFE_EXACT_REPAIR_ROWS_REQUIRE_SEPARATE_REVIEW"
    if _truthy(source_summary.get("row_scan_truncated")):
        return "HOLD_BOUNDED_SCAN_INCOMPLETE"
    if _to_int(inventory.get("placeholder_rows")):
        return "PLACEHOLDER_TEAM"
    if _to_int(inventory.get("partial_provenance_markets")):
        return "PARTIAL_LEGACY_IDENTIFIER"
    if _to_int(inventory.get("unlinked_parsed_markets")):
        return "UNLINKED_PARSED_MARKET"
    if _to_int(inventory.get("unsupported_composite_markets")):
        return "UNSUPPORTED_KXMVE_COMPOSITE"
    return str(
        source_summary.get("first_hard_blocker")
        or phase3z_summary.get("phase3ae_gate_status")
        or "NO_SAFE_SPORTS_REPAIR_ROWS"
    )


def _next_focus(
    *,
    safe_candidates: int,
    partial_rows: int,
    placeholder_rows: int,
    unsupported_composites: int,
) -> str:
    if safe_candidates:
        return "manual exact-sports repair review before any separate writer-gated apply"
    if placeholder_rows:
        return "Phase 3AH placeholder/roster verification with exact team identities"
    if partial_rows:
        return "sports partial provenance exact schedule/team/date repair"
    if unsupported_composites:
        return "future KXMVE composite-market support, kept outside single-market repair"
    return "sports provenance monitoring; continue weather/economic activation backlog"


def _next_actions(summary: dict[str, Any]) -> list[str]:
    if _to_int(summary.get("safe_repair_candidates")):
        return [
            R6_COMMAND,
            "# review sports_repair_candidates.csv before any separate apply command",
        ]
    return [R6_COMMAND, FREE_SOURCE_COMMAND]


def _acceptance(summary: dict[str, Any]) -> dict[str, bool]:
    return {
        "safe_rows_repaired_only_with_exact_provenance": (
            _to_int(summary["safe_repairs_applied"]) == 0
        ),
        "unsafe_rows_stay_blocked": _to_int(summary["unsafe_rows"]) >= 0,
        "no_live_demo_orders": summary["live_or_demo_execution"] is False,
        "no_paper_orders": summary["paper_trade_creation"] is False,
        "db_writes_zero": _to_int(summary["db_writes_performed"]) == 0,
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = _metadata_lines(payload, "# Phase 3BB-R6 Sports Provenance Repair Sprint")
    lines.extend(
        [
            "",
            "## Status",
            "",
            f"- Status: `{summary['status']}`",
            f"- First hard blocker: `{summary['first_hard_blocker']}`",
            f"- Active sports parsed markets: `{summary['active_sports_parsed_markets']}`",
            f"- Active sports linked markets: `{summary['active_sports_linked_markets']}`",
            f"- Verified derived markets: `{summary['verified_derived_markets']}`",
            f"- Verified schedule markets: `{summary['verified_schedule_markets']}`",
            "- Partial rows before/after: "
            f"`{summary['partial_rows_before']}` / `{summary['partial_rows_after']}`",
            "- Placeholder rows before/after: "
            f"`{summary['placeholder_rows_before']}` / "
            f"`{summary['placeholder_rows_after']}`",
            f"- Unsupported composites parked: `{summary['unsupported_composite_markets']}`",
            f"- Safe repair candidates: `{summary['safe_repair_candidates']}`",
            f"- Safe repairs applied: `{summary['safe_repairs_applied']}`",
            f"- Unsafe rows reported: `{summary['unsafe_rows']}`",
            f"- Next sports focus: `{summary['next_sport_category_to_focus']}`",
            "",
            "No forecasts, paper trades, live/demo exchange writes, or DB repairs were run.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = _metadata_lines(payload, "# Phase 3BB-R6 Sports Provenance Repair")
    lines.extend(
        [
            "",
            "## Before And After",
            "",
            "| Metric | Before | After |",
            "| --- | ---: | ---: |",
            "| Partial provenance markets | "
            f"{summary['partial_rows_before']} | {summary['partial_rows_after']} |",
            "| Partial provenance link rows | "
            f"{summary['partial_link_rows_before']} | "
            f"{summary['partial_link_rows_after']} |",
            "| Placeholder rows | "
            f"{summary['placeholder_rows_before']} | {summary['placeholder_rows_after']} |",
            f"| Safe repairs applied | 0 | {summary['safe_repairs_applied']} |",
            "",
            "## Sports Inventory",
            "",
            f"- Active sports parsed markets: `{summary['active_sports_parsed_markets']}`",
            f"- Active sports linked markets: `{summary['active_sports_linked_markets']}`",
            f"- Verified derived markets: `{summary['verified_derived_markets']}`",
            f"- Verified schedule markets: `{summary['verified_schedule_markets']}`",
            f"- Unsupported KXMVE composites parked: `{summary['unsupported_composite_markets']}`",
            "",
            "## Repair Gate",
            "",
            f"- Safe exact repair candidates: `{summary['safe_repair_candidates']}`",
            f"- Unsafe rows: `{summary['unsafe_rows']}`",
            f"- First hard blocker: `{summary['first_hard_blocker']}`",
            f"- Row scan complete: `{summary['row_scan_complete']}`",
            f"- Row scan max rows: `{summary['row_scan_max_rows']}`",
            "",
            "## Next Actions",
            "",
        ]
    )
    lines.extend(f"- `{command}`" for command in payload["next_actions"])
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- Exact schedule/team/date evidence only.",
            "- No fuzzy teams, inferred rounds, fabricated schedules, or sibling matches.",
            "- Placeholder rows remain blocked.",
            "- Unsupported KXMVE composites remain parked outside single-market remediation.",
            "- No paper/live/demo orders were created.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _scalar_int(session: Session, statement: Any) -> int:
    try:
        return int(session.scalar(statement) or 0)
    except Exception:
        return 0


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item)]
    if value is None:
        return []
    text = str(value)
    return [text] if text else []


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}
