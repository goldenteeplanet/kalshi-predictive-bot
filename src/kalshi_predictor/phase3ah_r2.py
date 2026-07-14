from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3af import DEFAULT_SOCCER_COMPETITIONS, run_sports_schedule_bootstrap
from kalshi_predictor.phase3ah_roster import (
    DEFAULT_OUTPUT_DIR as DEFAULT_PHASE3AH_OUTPUT_DIR,
)
from kalshi_predictor.phase3ah_roster import (
    DEFAULT_ROSTER_TEMPLATE_PATH,
    write_phase3ah_roster_verification_report,
)
from kalshi_predictor.utils.time import utc_now

PHASE_3AH_R2_VERSION = "phase3ah_r2_v1"
DEFAULT_DIAGNOSTICS_PATH = Path(
    "reports/phase3ae_roster_candidates/phase3ae_roster_candidate_diagnostics.json"
)
DEFAULT_R2_OUTPUT_DIR = Path("reports/phase3ah_r2")
DEFAULT_R2_SCHEDULE_OUTPUT_DIR = Path("data/sports_schedules/phase3ah_r2")
DEFAULT_R2_SCHEDULE_START_DATE = "2026-07-07"
DEFAULT_R2_SCHEDULE_DAYS_AHEAD = 4


ScheduleBootstrapRunner = Any


@dataclass(frozen=True)
class Phase3AHR2ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    updated_roster_template_path: Path
    roster_verification_json_path: Path
    verified_roster_evidence_path: Path


CURATED_ROSTER_EVIDENCE: tuple[dict[str, Any], ...] = (
    {
        "player_name": "Breel Embolo",
        "canonical_player_id": "fifa:worldcup2026:switzerland:breel-embolo",
        "current_team_key": "SOCCER:sui",
        "current_team_name": "Switzerland",
        "roster_source_url": (
            "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/"
            "teams/switzerland/squad"
        ),
    },
    {
        "player_name": "Ermedin Demirovic",
        "canonical_player_id": "fifa:worldcup2026:bosnia-herzegovina:ermedin-demirovic",
        "current_team_key": "SOCCER:bih",
        "current_team_name": "Bosnia-Herzegovina",
        "roster_source_url": (
            "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/"
            "articles/bosnia-and-herzegovina-sergej-barbarez-names-squad"
        ),
    },
    {
        "player_name": "Achraf Hakimi",
        "canonical_player_id": "fifa:worldcup2026:morocco:achraf-hakimi",
        "current_team_key": "SOCCER:mar",
        "current_team_name": "Morocco",
        "roster_source_url": (
            "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/"
            "teams/morocco/squad"
        ),
    },
    {
        "player_name": "Patrik Schick",
        "canonical_player_id": "fifa:worldcup2026:czechia:patrik-schick",
        "current_team_key": "SOCCER:cze",
        "current_team_name": "Czechia",
        "roster_source_url": (
            "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/"
            "teams/czechia/squad"
        ),
    },
    {
        "player_name": "Luis Diaz",
        "canonical_player_id": "fifa:worldcup2026:colombia:luis-diaz",
        "current_team_key": "SOCCER:col",
        "current_team_name": "Colombia",
        "roster_source_url": (
            "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/"
            "teams/colombia/squad"
        ),
    },
    {
        "player_name": "Bruno Guimaraes",
        "canonical_player_id": "fifa:worldcup2026:brazil:bruno-guimaraes",
        "current_team_key": "SOCCER:bra",
        "current_team_name": "Brazil",
        "roster_source_url": (
            "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/"
            "teams/brazil/squad"
        ),
    },
    {
        "player_name": "Granit Xhaka",
        "canonical_player_id": "fifa:worldcup2026:switzerland:granit-xhaka",
        "current_team_key": "SOCCER:sui",
        "current_team_name": "Switzerland",
        "roster_source_url": (
            "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/"
            "teams/switzerland/squad"
        ),
    },
    {
        "player_name": "Lee Kang-in",
        "canonical_player_id": "fifa:worldcup2026:korea-republic:lee-kang-in",
        "current_team_key": "SOCCER:kor",
        "current_team_name": "South Korea",
        "roster_source_url": (
            "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/"
            "teams/korea-republic/squad"
        ),
    },
)


def run_phase3ah_r2_backfill(
    session: Session | None,
    *,
    output_dir: Path = DEFAULT_R2_OUTPUT_DIR,
    roster_template_path: Path = DEFAULT_ROSTER_TEMPLATE_PATH,
    roster_output_dir: Path = DEFAULT_PHASE3AH_OUTPUT_DIR,
    diagnostics_path: Path = DEFAULT_DIAGNOSTICS_PATH,
    schedule_output_dir: Path = DEFAULT_R2_SCHEDULE_OUTPUT_DIR,
    schedule_start_date: str = DEFAULT_R2_SCHEDULE_START_DATE,
    schedule_days_ahead: int = DEFAULT_R2_SCHEDULE_DAYS_AHEAD,
    fetch_schedules: bool = True,
    ingest_schedules: bool = True,
    soccer_competitions: str | list[str] | tuple[str, ...] = DEFAULT_SOCCER_COMPETITIONS,
    schedule_runner: ScheduleBootstrapRunner = run_sports_schedule_bootstrap,
) -> dict[str, Any]:
    """Apply the Phase 3AH-R2 roster overlay and backfill the target soccer window.

    This phase only updates evidence templates, writes reports, and optionally ingests schedule
    rows. It never creates verified links or execution/order rows.
    """
    if ingest_schedules and session is None:
        raise ValueError("A database session is required when ingest_schedules=True.")

    output_dir.mkdir(parents=True, exist_ok=True)
    rows = _load_roster_rows(roster_template_path)
    diagnostics = _load_json(diagnostics_path)
    top_missing_by_player = _top_missing_by_player(diagnostics)
    before_verified = sum(1 for row in rows if _is_verified_row(row))
    applied_rows = _apply_curated_rows(rows, top_missing_by_player=top_missing_by_player)
    roster_template_path.parent.mkdir(parents=True, exist_ok=True)
    roster_template_path.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")

    roster_artifacts = write_phase3ah_roster_verification_report(
        output_dir=roster_output_dir,
        roster_template_path=roster_template_path,
    )
    verification_payload = _load_json(roster_artifacts.json_path)

    schedule_result: dict[str, Any] | None = None
    if fetch_schedules:
        schedule_result = schedule_runner(
            session,
            leagues=("SOCCER",),
            start_date=schedule_start_date,
            days_ahead=schedule_days_ahead,
            schedule_output_dir=schedule_output_dir,
            ingest=ingest_schedules,
            write_legacy_sample=False,
            soccer_competitions=soccer_competitions,
        )

    summary = {
        "curated_roster_rows_available": len(CURATED_ROSTER_EVIDENCE),
        "curated_roster_rows_applied": len(applied_rows),
        "verified_roster_rows_before": before_verified,
        "verified_roster_rows_after": verification_payload["summary"][
            "verified_roster_rows"
        ],
        "rework_rows_after": verification_payload["summary"]["rework_rows"],
        "schedule_fetches_run": len(schedule_result["league_results"])
        if schedule_result
        else 0,
        "schedules_ingested": int(
            schedule_result["summary"].get("games_inserted") or 0
        )
        if schedule_result
        else 0,
        "auto_upgrades_created": 0,
    }
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AH_R2",
        "phase_version": PHASE_3AH_R2_VERSION,
        "mode": "PAPER_ONLY_PLAYER_PROP_COMPLETENESS_AND_SCHEDULE_BACKFILL",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "roster_template_path": str(roster_template_path),
        "diagnostics_path": str(diagnostics_path),
        "schedule_output_dir": str(schedule_output_dir),
        "schedule_window": {
            "league": "SOCCER",
            "start_date": schedule_start_date,
            "days_ahead": schedule_days_ahead,
            "target_close_dates": ["2026-07-08", "2026-07-09"],
            "target_team_keys": ["SOCCER:bra", "SOCCER:can", "SOCCER:mar", "SOCCER:kor"],
        },
        "auto_upgrade_policy": {
            "phase3ah_r2_creates_verified_links": False,
            "auto_upgrades_created": 0,
            "policy": (
                "Phase 3AH-R2 can update roster evidence and schedules only. "
                "Phase 3AE remains the only verified link-upgrade path."
            ),
        },
        "summary": summary,
        "applied_roster_rows": applied_rows,
        "roster_verification_summary": verification_payload["summary"],
        "schedule_result": schedule_result,
        "recommended_next_action": _recommended_next_action(summary),
    }


def write_phase3ah_r2_backfill_report(
    session: Session | None,
    *,
    output_dir: Path = DEFAULT_R2_OUTPUT_DIR,
    roster_template_path: Path = DEFAULT_ROSTER_TEMPLATE_PATH,
    roster_output_dir: Path = DEFAULT_PHASE3AH_OUTPUT_DIR,
    diagnostics_path: Path = DEFAULT_DIAGNOSTICS_PATH,
    schedule_output_dir: Path = DEFAULT_R2_SCHEDULE_OUTPUT_DIR,
    schedule_start_date: str = DEFAULT_R2_SCHEDULE_START_DATE,
    schedule_days_ahead: int = DEFAULT_R2_SCHEDULE_DAYS_AHEAD,
    fetch_schedules: bool = True,
    ingest_schedules: bool = True,
    soccer_competitions: str | list[str] | tuple[str, ...] = DEFAULT_SOCCER_COMPETITIONS,
    schedule_runner: ScheduleBootstrapRunner = run_sports_schedule_bootstrap,
) -> Phase3AHR2ArtifactSet:
    payload = run_phase3ah_r2_backfill(
        session,
        output_dir=output_dir,
        roster_template_path=roster_template_path,
        roster_output_dir=roster_output_dir,
        diagnostics_path=diagnostics_path,
        schedule_output_dir=schedule_output_dir,
        schedule_start_date=schedule_start_date,
        schedule_days_ahead=schedule_days_ahead,
        fetch_schedules=fetch_schedules,
        ingest_schedules=ingest_schedules,
        soccer_competitions=soccer_competitions,
        schedule_runner=schedule_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ah_r2_player_prop_backfill.json"
    markdown_path = output_dir / "phase3ah_r2_player_prop_backfill.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AHR2ArtifactSet(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        updated_roster_template_path=roster_template_path,
        roster_verification_json_path=roster_output_dir
        / "phase3ah_roster_participant_verification.json",
        verified_roster_evidence_path=roster_output_dir
        / "phase3ah_verified_roster_evidence.json",
    )


def _load_roster_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing roster template: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = (
        payload.get("roster_review_template", payload.get("rows", []))
        if isinstance(payload, dict)
        else payload
    )
    return [dict(row) for row in rows if isinstance(row, dict)]


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _top_missing_by_player(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("top_missing_roster_players", [])
    return {
        _norm(row.get("player_name")): row
        for row in rows
        if isinstance(row, dict) and row.get("player_name")
    }


def _apply_curated_rows(
    rows: list[dict[str, Any]],
    *,
    top_missing_by_player: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_player = {_norm(row.get("player_name")): row for row in rows if row.get("player_name")}
    applied: list[dict[str, Any]] = []
    for curated in CURATED_ROSTER_EVIDENCE:
        key = _norm(curated["player_name"])
        row = by_player.get(key)
        if row is None:
            row = {
                "league": "SOCCER",
                "player_name": curated["player_name"],
                "count": 0,
                "example_tickers": [],
                "example_player_prop_tickers": [],
                "verified_entity_type": "PLAYER",
                "safety_note": "",
            }
            rows.append(row)
            by_player[key] = row
        diagnostic = top_missing_by_player.get(key, {})
        examples = _merge_examples(
            row.get("example_player_prop_tickers", []),
            diagnostic.get("example_tickers", []),
        )
        row.update(
            {
                "league": "SOCCER",
                "player_name": curated["player_name"],
                "canonical_player_id": curated["canonical_player_id"],
                "current_team_key": curated["current_team_key"],
                "current_team_name": curated["current_team_name"],
                "roster_source_url": curated["roster_source_url"],
                "valid_from": "2026-06-27",
                "valid_to": "",
                "review_status": "VERIFIED",
                "verified_entity_type": "PLAYER",
                "safe_to_apply": True,
                "blocks_team_link_upgrade": False,
                "example_player_prop_tickers": examples,
                "example_tickers": _merge_examples(row.get("example_tickers", []), examples),
                "count": max(int(row.get("count") or 0), int(diagnostic.get("count") or 0)),
                "safety_note": (
                    "Verified for Phase 3AH-R2 roster evidence only; Phase 3AE remains "
                    "the only link-upgrade path."
                ),
            }
        )
        applied.append(_applied_row(row))
    return applied


def _merge_examples(*groups: object) -> list[str]:
    merged: list[str] = []
    for group in groups:
        if not isinstance(group, list):
            continue
        for value in group:
            text = str(value or "").strip()
            if text and text not in merged:
                merged.append(text)
    return merged[:20]


def _applied_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "league": row.get("league"),
        "player_name": row.get("player_name"),
        "canonical_player_id": row.get("canonical_player_id"),
        "current_team_key": row.get("current_team_key"),
        "current_team_name": row.get("current_team_name"),
        "roster_source_url": row.get("roster_source_url"),
        "valid_from": row.get("valid_from"),
        "example_player_prop_tickers": row.get("example_player_prop_tickers", [])[:5],
        "safe_to_apply": row.get("safe_to_apply"),
    }


def _is_verified_row(row: dict[str, Any]) -> bool:
    return str(row.get("review_status") or "").upper() in {
        "APPROVED",
        "READY",
        "REVIEWED_VERIFIED",
        "VERIFIED",
    } and row.get("safe_to_apply") is True


def _norm(value: object) -> str:
    return " ".join(str(value or "").lower().replace("-", " ").split())


def _recommended_next_action(summary: dict[str, Any]) -> str:
    if summary["curated_roster_rows_applied"] and summary["schedule_fetches_run"]:
        return (
            "Rerun Phase 3AE roster candidate diagnostics, then run Phase 3AE only if "
            "clean candidates appear."
        )
    if summary["curated_roster_rows_applied"]:
        return "Run SOCCER schedule backfill for the July 8-9 windows before Phase 3AE."
    return "Review the diagnostics report and add verified roster evidence before Phase 3AE."


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AH-R2 Player-Prop Completeness + Schedule Backfill",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Applied Roster Evidence", ""])
    for row in payload["applied_roster_rows"]:
        lines.append(
            f"- {row['player_name']} -> {row['current_team_key']} "
            f"({row['roster_source_url']})"
        )
    if not payload["applied_roster_rows"]:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Schedule Window",
            "",
            f"- Start date: {payload['schedule_window']['start_date']}",
            f"- Days ahead: {payload['schedule_window']['days_ahead']}",
            f"- Target close dates: {payload['schedule_window']['target_close_dates']}",
            f"- Target team keys: {payload['schedule_window']['target_team_keys']}",
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
            "## Safety",
            "",
            "- No verified sports links are inserted.",
            "- No feature rows are inserted.",
            "- No demo or live orders.",
            "",
        ]
    )
    return "\n".join(lines)
