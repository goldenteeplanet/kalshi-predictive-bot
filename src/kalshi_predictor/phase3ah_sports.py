from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import SportsGame
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3ae_roster_candidates import (
    CROSS_SPORT_PLAYER_LEAGUE_HINTS,
    NON_PLAYER_ROSTER_ENTITIES,
)
from kalshi_predictor.phase3af import (
    DEFAULT_SOCCER_COMPETITIONS,
    run_sports_schedule_bootstrap,
)
from kalshi_predictor.utils.time import utc_now

PHASE_3AH_SPORTS_VERSION = "phase3ah_sports_v1"
DEFAULT_REPAIR_PATH = Path("reports/phase3ag/phase3ag_sports_link_repair_pass.json")
DEFAULT_ALIAS_PATH = Path("reports/phase3ag/phase3ag_missing_alias_candidates.json")
DEFAULT_ROSTER_CANDIDATE_DIAGNOSTICS_PATH = Path(
    "reports/phase3ae_roster_candidates/phase3ae_roster_candidate_diagnostics.json"
)
DEFAULT_OUTPUT_DIR = Path("reports/phase3ah_sports")
DEFAULT_SCHEDULE_OUTPUT_DIR = Path("data/sports_schedules/phase3ah")
NO_VERIFIED_WINDOW = "NO_VERIFIED_GAMES_IN_SCHEDULE_WINDOW"
PLAYER_PROP_CAUSE = "PLAYER_PROP_NEEDS_PLAYER_TEAM_MAPPING"
MULTI_LEG_CAUSE = "MULTI_LEG_MARKET_REQUIRES_MANUAL_DISAMBIGUATION"
PLAYER_ROSTER_ENTITY_TYPES = {"", "PLAYER", "PLAYER_OR_PARTICIPANT", "PARTICIPANT"}
NON_PLAYER_ROSTER_CANDIDATE_NAMES = set(NON_PLAYER_ROSTER_ENTITIES) | {
    "a s",
    "atlanta",
    "bosnia and herzegovina",
    "chicago c",
    "congo dr",
    "congo dr wins by more than goals",
    "cleveland",
    "democratic republic of the congo",
    "detroit",
    "dr congo",
    "indiana",
    "los angeles a",
    "los angeles d",
    "milwaukee",
    "minnesota",
    "texas",
    "toronto",
}


ScheduleBootstrapRunner = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class Phase3AHSportsArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    schedule_plan_path: Path
    team_alias_template_path: Path
    roster_template_path: Path
    manual_disambiguation_template_path: Path
    round_placeholder_template_path: Path


def build_phase3ah_sports_evidence_backfill(
    session: Session | None,
    *,
    repair_path: Path = DEFAULT_REPAIR_PATH,
    alias_candidates_path: Path | None = DEFAULT_ALIAS_PATH,
    roster_candidate_diagnostics_path: Path | None = None,
    existing_roster_template_path: Path | None = None,
    leagues: str | list[str] | tuple[str, ...] = ("MLB", "WNBA", "SOCCER"),
    window_days_before: int = 1,
    window_days_after: int = 1,
    fetch_schedules: bool = False,
    ingest_schedules: bool = False,
    schedule_output_dir: Path = DEFAULT_SCHEDULE_OUTPUT_DIR,
    max_windows_per_league: int | None = None,
    limit: int | None = None,
    bootstrap_runner: ScheduleBootstrapRunner = run_sports_schedule_bootstrap,
    soccer_competitions: str | list[str] | tuple[str, ...] = DEFAULT_SOCCER_COMPETITIONS,
) -> dict[str, Any]:
    """Build verified evidence inputs from Phase 3AG failed sports links.

    This phase writes evidence, schedules, and human-review templates. It never creates
    verified sports links; Phase 3AE remains the only upgrade path.
    """

    if window_days_before < 0 or window_days_after < 0:
        raise ValueError("window day buffers must be non-negative.")
    if ingest_schedules and session is None:
        raise ValueError("A database session is required when ingest_schedules=True.")

    selected_leagues = _parse_leagues(leagues)
    repair_payload = _load_json_file(repair_path)
    rows = _repair_rows(repair_payload, selected_leagues=selected_leagues, limit=limit)
    alias_candidates = _alias_candidates(
        repair_payload,
        alias_candidates_path=alias_candidates_path,
        selected_leagues=selected_leagues,
    )
    roster_diagnostics = _load_optional_json_file(roster_candidate_diagnostics_path)
    roster_candidate_rows = _roster_candidates_from_diagnostics(
        roster_diagnostics,
        selected_leagues=selected_leagues,
    )
    existing_roster_rows = _load_existing_roster_rows(existing_roster_template_path)
    round_placeholder_template = _round_placeholder_template_from_diagnostics(
        roster_diagnostics,
        selected_leagues=selected_leagues,
    )
    cause_breakdown = Counter(
        str(row.get("primary_cause") or row.get("cause") or "UNKNOWN") for row in rows
    )
    schedule_windows = _schedule_windows(
        rows,
        selected_leagues=selected_leagues,
        window_days_before=window_days_before,
        window_days_after=window_days_after,
        max_windows_per_league=max_windows_per_league,
    )
    schedule_fetches = (
        _fetch_schedule_windows(
            session,
            schedule_windows=schedule_windows,
            schedule_output_dir=schedule_output_dir,
            ingest_schedules=ingest_schedules,
            bootstrap_runner=bootstrap_runner,
            soccer_competitions=soccer_competitions,
        )
        if fetch_schedules
        else []
    )
    annotated_schedule_windows = _annotate_schedule_windows(session, schedule_windows)
    team_alias_template = _team_alias_template(alias_candidates)
    has_roster_diagnostics = bool(roster_diagnostics)
    generated_roster_rows = (
        roster_candidate_rows
        if has_roster_diagnostics
        else _roster_template(alias_candidates, rows)
    )
    roster_template = _merge_roster_template_rows(
        existing_roster_rows=existing_roster_rows,
        generated_rows=generated_roster_rows,
    )
    manual_template = _manual_disambiguation_template(rows)
    player_prop_rows = _player_prop_rows(rows)
    team_game_rows = _team_game_rows(rows)
    ready_gate = _phase3ae_ready_gate(rows)

    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AH_SPORTS",
        "phase_version": PHASE_3AH_SPORTS_VERSION,
        "mode": "PAPER_ONLY_VERIFIED_SPORTS_EVIDENCE_BACKFILL",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "repair_source_path": str(repair_path),
        "alias_candidates_source_path": str(alias_candidates_path)
        if alias_candidates_path
        else None,
        "roster_candidate_diagnostics_path": str(roster_candidate_diagnostics_path)
        if roster_candidate_diagnostics_path
        else None,
        "selected_leagues": selected_leagues,
        "window_days_before": window_days_before,
        "window_days_after": window_days_after,
        "fetch_schedules": fetch_schedules,
        "ingest_schedules": ingest_schedules,
        "auto_upgrade_policy": {
            "phase3ah_creates_verified_links": False,
            "auto_upgrades_created": 0,
            "policy": (
                "Phase 3AH only writes evidence and manual templates. Phase 3AE may "
                "upgrade later only when team, time, and market type are clean."
            ),
        },
        "summary": {
            "repair_rows_reviewed": len(rows),
            "schedule_backfill_rows": sum(
                1
                for row in rows
                if str(row.get("primary_cause") or row.get("cause")) == NO_VERIFIED_WINDOW
            ),
            "schedule_windows": len(annotated_schedule_windows),
            "schedule_fetches_run": len(schedule_fetches),
            "schedules_ingested": sum(
                int(fetch.get("summary", {}).get("games_inserted") or 0)
                for fetch in schedule_fetches
            ),
            "alias_candidates": len(alias_candidates),
            "team_alias_review_rows": len(team_alias_template),
            "roster_review_rows": len(roster_template),
            "current_roster_candidate_rows": len(roster_candidate_rows),
            "manual_disambiguation_rows": len(manual_template),
            "round_placeholder_resolution_rows": len(round_placeholder_template),
            "player_prop_rows": len(player_prop_rows),
            "team_game_rows": len(team_game_rows),
            "phase3ae_ready_rows": ready_gate["phase3ae_ready_rows"],
            "auto_upgrades_created": 0,
        },
        "cause_breakdown": [
            {"cause": cause, "count": count} for cause, count in cause_breakdown.most_common()
        ],
        "schedule_backfill_plan": annotated_schedule_windows,
        "schedule_fetch_results": schedule_fetches,
        "team_alias_review_template": team_alias_template,
        "roster_review_template": roster_template,
        "manual_disambiguation_template": manual_template,
        "round_placeholder_resolution_template": round_placeholder_template,
        "player_prop_evidence_rows": player_prop_rows[:200],
        "team_game_evidence_rows": team_game_rows[:200],
        "phase3ae_ready_gate": ready_gate,
        "next_commands": _next_commands(
            fetch_schedules=fetch_schedules,
            schedule_windows=annotated_schedule_windows,
            round_placeholder_rows=len(round_placeholder_template),
        ),
        "recommended_next_action": _recommended_next_action(
            schedule_windows=annotated_schedule_windows,
            schedule_fetches=schedule_fetches,
            roster_rows=len(roster_template),
            team_alias_rows=len(team_alias_template),
            round_placeholder_rows=len(round_placeholder_template),
            ready_rows=ready_gate["phase3ae_ready_rows"],
            fetch_schedules=fetch_schedules,
        ),
    }


def write_phase3ah_sports_evidence_report(
    session: Session | None,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    repair_path: Path = DEFAULT_REPAIR_PATH,
    alias_candidates_path: Path | None = DEFAULT_ALIAS_PATH,
    roster_candidate_diagnostics_path: Path | None = DEFAULT_ROSTER_CANDIDATE_DIAGNOSTICS_PATH,
    existing_roster_template_path: Path | None = None,
    leagues: str | list[str] | tuple[str, ...] = ("MLB", "WNBA", "SOCCER"),
    window_days_before: int = 1,
    window_days_after: int = 1,
    fetch_schedules: bool = False,
    ingest_schedules: bool = False,
    schedule_output_dir: Path = DEFAULT_SCHEDULE_OUTPUT_DIR,
    max_windows_per_league: int | None = None,
    limit: int | None = None,
    bootstrap_runner: ScheduleBootstrapRunner = run_sports_schedule_bootstrap,
    soccer_competitions: str | list[str] | tuple[str, ...] = DEFAULT_SOCCER_COMPETITIONS,
) -> Phase3AHSportsArtifactSet:
    payload = build_phase3ah_sports_evidence_backfill(
        session,
        repair_path=repair_path,
        alias_candidates_path=alias_candidates_path,
        roster_candidate_diagnostics_path=roster_candidate_diagnostics_path,
        existing_roster_template_path=existing_roster_template_path
        or output_dir / "phase3ah_roster_review_template.json",
        leagues=leagues,
        window_days_before=window_days_before,
        window_days_after=window_days_after,
        fetch_schedules=fetch_schedules,
        ingest_schedules=ingest_schedules,
        schedule_output_dir=schedule_output_dir,
        max_windows_per_league=max_windows_per_league,
        limit=limit,
        bootstrap_runner=bootstrap_runner,
        soccer_competitions=soccer_competitions,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ah_sports_evidence_backfill.json"
    markdown_path = output_dir / "phase3ah_sports_evidence_backfill.md"
    schedule_plan_path = output_dir / "phase3ah_schedule_backfill_plan.json"
    team_alias_template_path = output_dir / "phase3ah_team_alias_review_template.json"
    roster_template_path = output_dir / "phase3ah_roster_review_template.json"
    manual_template_path = output_dir / "phase3ah_manual_disambiguation_template.json"
    round_placeholder_template_path = (
        output_dir / "phase3ah_round_placeholder_resolution_template.json"
    )
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    schedule_plan_path.write_text(
        json.dumps(payload["schedule_backfill_plan"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    team_alias_template_path.write_text(
        json.dumps(payload["team_alias_review_template"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    roster_template_path.write_text(
        json.dumps(payload["roster_review_template"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    manual_template_path.write_text(
        json.dumps(payload["manual_disambiguation_template"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    round_placeholder_template_path.write_text(
        json.dumps(
            payload["round_placeholder_resolution_template"],
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return Phase3AHSportsArtifactSet(
        output_dir,
        json_path,
        markdown_path,
        schedule_plan_path,
        team_alias_template_path,
        roster_template_path,
        manual_template_path,
        round_placeholder_template_path,
    )


def _load_json_file(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"Missing Phase 3AH input file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_optional_json_file(path: Path | None) -> Any:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _repair_rows(
    payload: dict[str, Any],
    *,
    selected_leagues: list[str],
    limit: int | None,
) -> list[dict[str, Any]]:
    rows = [row for row in payload.get("rows", []) if isinstance(row, dict)]
    selected = {league.upper() for league in selected_leagues}
    filtered = [
        row
        for row in rows
        if str(row.get("league") or "").upper() in selected
        and str(row.get("phase3ae_status") or "NO_VERIFIED_MATCH") == "NO_VERIFIED_MATCH"
    ]
    return filtered[:limit] if limit is not None else filtered


def _alias_candidates(
    repair_payload: dict[str, Any],
    *,
    alias_candidates_path: Path | None,
    selected_leagues: list[str],
) -> list[dict[str, Any]]:
    if alias_candidates_path is not None and alias_candidates_path.exists():
        source = _load_json_file(alias_candidates_path)
    else:
        source = repair_payload.get("missing_alias_candidates", [])
    candidates = source if isinstance(source, list) else source.get("alias_candidates", [])
    selected = {league.upper() for league in selected_leagues}
    return [
        row
        for row in candidates
        if isinstance(row, dict) and str(row.get("league") or "").upper() in selected
    ]


def _schedule_windows(
    rows: list[dict[str, Any]],
    *,
    selected_leagues: list[str],
    window_days_before: int,
    window_days_after: int,
    max_windows_per_league: int | None,
) -> list[dict[str, Any]]:
    dates_by_league: dict[str, set[date]] = defaultdict(set)
    row_counts_by_league_date: Counter[tuple[str, date]] = Counter()
    selected = {league.upper() for league in selected_leagues}
    for row in rows:
        if str(row.get("primary_cause") or row.get("cause")) != NO_VERIFIED_WINDOW:
            continue
        league = str(row.get("league") or "").upper()
        if league not in selected:
            continue
        close_date = _date_from_iso(row.get("market_close_time") or row.get("close_time"))
        if close_date is None:
            continue
        row_counts_by_league_date[(league, close_date)] += 1
        for offset in range(-window_days_before, window_days_after + 1):
            dates_by_league[league].add(close_date + timedelta(days=offset))

    windows: list[dict[str, Any]] = []
    for league in sorted(dates_by_league):
        league_windows = _collapse_dates(
            league,
            sorted(dates_by_league[league]),
            row_counts_by_league_date=row_counts_by_league_date,
        )
        if max_windows_per_league is not None and max_windows_per_league > 0:
            league_windows = league_windows[:max_windows_per_league]
        windows.extend(league_windows)
    return windows


def _collapse_dates(
    league: str,
    dates: list[date],
    *,
    row_counts_by_league_date: Counter[tuple[str, date]],
) -> list[dict[str, Any]]:
    if not dates:
        return []
    grouped: list[list[date]] = [[dates[0]]]
    for current in dates[1:]:
        if current == grouped[-1][-1] + timedelta(days=1):
            grouped[-1].append(current)
        else:
            grouped.append([current])
    windows: list[dict[str, Any]] = []
    for group in grouped:
        first = group[0]
        last = group[-1]
        source_rows = sum(row_counts_by_league_date[(league, item)] for item in group)
        windows.append(
            {
                "league": league,
                "start_date": first.isoformat(),
                "end_date": last.isoformat(),
                "days_ahead": (last - first).days + 1,
                "dates": [item.isoformat() for item in group],
                "source_failed_rows": source_rows,
                "command": (
                    "kalshi-bot phase3af-sports-schedule-bootstrap "
                    f"--leagues {league} --start-date {first.isoformat()} "
                    f"--days-ahead {(last - first).days + 1} --ingest"
                ),
            }
        )
    return windows


def _fetch_schedule_windows(
    session: Session | None,
    *,
    schedule_windows: list[dict[str, Any]],
    schedule_output_dir: Path,
    ingest_schedules: bool,
    bootstrap_runner: ScheduleBootstrapRunner,
    soccer_competitions: str | list[str] | tuple[str, ...],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for window in schedule_windows:
        payload = bootstrap_runner(
            session,
            leagues=window["league"],
            start_date=window["start_date"],
            days_ahead=window["days_ahead"],
            schedule_output_dir=schedule_output_dir,
            ingest=ingest_schedules,
            write_legacy_sample=False,
            timeout_seconds=20.0,
            soccer_competitions=soccer_competitions,
            include_coverage=False,
        )
        results.append(
            {
                "league": window["league"],
                "start_date": window["start_date"],
                "days_ahead": window["days_ahead"],
                "ingest_requested": ingest_schedules,
                "summary": payload.get("summary", {}),
                "schedule_paths": payload.get("schedule_paths", []),
                "errors": payload.get("errors", []),
            }
        )
    return results


def _annotate_schedule_windows(
    session: Session | None,
    windows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if session is None or not windows:
        return [
            {
                **window,
                "verified_games_in_db": None,
                "schedule_evidence_status": "UNKNOWN_SESSION_NOT_PROVIDED",
            }
            for window in windows
        ]
    leagues = sorted({window["league"] for window in windows})
    games = list(
        session.scalars(
            select(SportsGame)
            .where(SportsGame.league.in_(leagues), SportsGame.scheduled_at.is_not(None))
            .order_by(SportsGame.league, SportsGame.scheduled_at)
        )
    )
    verified_games = [game for game in games if _game_is_verified(game)]
    annotated: list[dict[str, Any]] = []
    for window in windows:
        window_dates = set(window.get("dates") or [])
        count = sum(
            1
            for game in verified_games
            if game.league == window["league"]
            and game.scheduled_at is not None
            and game.scheduled_at.date().isoformat() in window_dates
        )
        annotated.append(
            {
                **window,
                "verified_games_in_db": count,
                "schedule_evidence_status": (
                    "VERIFIED_GAMES_PRESENT_BUT_NO_CLEAN_MATCH"
                    if count > 0
                    else "BACKFILL_REQUIRED"
                ),
            }
        )
    return annotated


def _game_is_verified(game: SportsGame) -> bool:
    raw = decode_json(game.raw_json)
    source = str(raw.get("source") or "").lower()
    game_key = str(game.game_key or "").lower()
    status = str(game.status or "").lower()
    return not (
        source == "kalshi_event_derived"
        or "kalshi-event-derived" in game_key
        or "market-derived" in game_key
        or status == "kalshi_event_derived"
    )


def _team_alias_template(alias_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in alias_candidates:
        role = str(candidate.get("entity_role") or "")
        if "player" in role:
            continue
        entity = str(candidate.get("entity") or "").strip()
        if not entity:
            continue
        rows.append(
            {
                "league": candidate.get("league"),
                "entity": entity,
                "entity_role": candidate.get("entity_role"),
                "count": candidate.get("count", 0),
                "example_tickers": candidate.get("example_tickers", []),
                "review_status": "UNVERIFIED",
                "verified_entity_type": "TEAM_OR_COMPETITION_ENTITY",
                "canonical_team_key": "",
                "canonical_team_name": "",
                "alias_to_add": entity,
                "evidence_source_url": "",
                "reviewer": "",
                "reviewed_at": "",
                "safe_to_apply": False,
                "safety_note": (
                    "Add only after one league and one canonical team/entity are verified."
                ),
            }
        )
    return rows


def _roster_template(
    alias_candidates: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    player_prop_tickers = {
        str(row.get("ticker"))
        for row in rows
        if _is_player_prop(row) and row.get("ticker") is not None
    }
    team_alias_keys = {
        (
            str(candidate.get("league") or "").upper(),
            _normalized_roster_candidate_name(candidate.get("entity")),
        )
        for candidate in alias_candidates
        if "player" not in str(candidate.get("entity_role") or "")
        and str(candidate.get("entity") or "").strip()
    }
    roster_rows: list[dict[str, Any]] = []
    for candidate in alias_candidates:
        role = str(candidate.get("entity_role") or "")
        if "player" not in role:
            continue
        league = str(candidate.get("league") or "").upper()
        entity = str(candidate.get("entity") or "").strip()
        if not entity:
            continue
        if _blocks_generated_roster_candidate(
            league=league,
            player_name=entity,
            also_team_alias=(league, _normalized_roster_candidate_name(entity))
            in team_alias_keys,
        ):
            continue
        examples = [str(item) for item in candidate.get("example_tickers", [])]
        roster_rows.append(
            {
                "league": league or candidate.get("league"),
                "player_name": entity,
                "count": candidate.get("count", 0),
                "example_tickers": examples,
                "example_player_prop_tickers": [
                    ticker for ticker in examples if ticker in player_prop_tickers
                ],
                "review_status": "UNVERIFIED",
                "verified_entity_type": "PLAYER",
                "canonical_player_id": "",
                "current_team_key": "",
                "current_team_name": "",
                "roster_source_url": "",
                "valid_from": "",
                "valid_to": "",
                "safe_to_apply": False,
                "blocks_team_link_upgrade": True,
                "safety_note": (
                    "Player names must map to a verified roster/team before any player "
                    "prop link can be upgraded."
                ),
            }
        )
    return roster_rows


def _roster_candidates_from_diagnostics(
    payload: Any,
    *,
    selected_leagues: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    selected = {league.upper() for league in selected_leagues}
    rows: list[dict[str, Any]] = []
    for item in payload.get("top_missing_roster_players", []):
        if not isinstance(item, dict):
            continue
        league = str(item.get("league") or "").upper()
        player_name = str(item.get("player_name") or "").strip()
        if league not in selected or not player_name:
            continue
        if _blocks_roster_candidate_from_diagnostics(item, player_name):
            continue
        examples = [str(ticker) for ticker in item.get("example_tickers", [])]
        rows.append(
            {
                "league": league,
                "player_name": player_name,
                "count": int(item.get("count") or 0),
                "example_tickers": examples,
                "example_player_prop_tickers": examples,
                "review_status": "UNVERIFIED",
                "verified_entity_type": "PLAYER",
                "canonical_player_id": "",
                "current_team_key": "",
                "current_team_name": "",
                "roster_source_url": "",
                "valid_from": "",
                "valid_to": "",
                "safe_to_apply": False,
                "blocks_team_link_upgrade": True,
                "source": "phase3ae_roster_candidate_diagnostics",
                "diagnostic_reason": "NO_VERIFIED_ROSTER_PLAYER_MENTIONED",
                "safety_note": (
                    "Current R3 diagnostics say this is a true player/participant "
                    "roster gap. Verify source evidence before Phase 3AE."
                ),
            }
        )
    return rows


def _blocks_roster_candidate_from_diagnostics(
    item: dict[str, Any],
    player_name: str,
) -> bool:
    if item.get("blocks_roster_evidence") is True:
        return True
    entity_type = str(
        item.get("verified_entity_type") or item.get("entity_type") or ""
    ).upper()
    if entity_type not in PLAYER_ROSTER_ENTITY_TYPES:
        return True
    league = str(item.get("league") or "").upper()
    return _blocks_generated_roster_candidate(
        league=league,
        player_name=player_name,
        also_team_alias=False,
    )


def _blocks_generated_roster_candidate(
    *,
    league: str,
    player_name: str,
    also_team_alias: bool,
) -> bool:
    normalized = _normalized_roster_candidate_name(player_name)
    if not normalized:
        return True
    if also_team_alias or normalized in NON_PLAYER_ROSTER_CANDIDATE_NAMES:
        return True
    for hinted_name, hinted_league in CROSS_SPORT_PLAYER_LEAGUE_HINTS.items():
        if (
            _normalized_roster_candidate_name(hinted_name) == normalized
            and str(hinted_league or "").upper() != str(league or "").upper()
        ):
            return True
    return False


def _normalized_roster_candidate_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("-", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _load_existing_roster_rows(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("roster_review_template", payload.get("rows", []))
    else:
        rows = payload
    return [row for row in rows if isinstance(row, dict)]


def _merge_roster_template_rows(
    *,
    existing_roster_rows: list[dict[str, Any]],
    generated_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    generated_by_key = {
        _roster_key(row): row for row in generated_rows if _roster_key(row) is not None
    }
    merged_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in existing_roster_rows:
        key = _roster_key(row)
        if key is None:
            continue
        if _row_is_verified_roster_evidence(row) or key in generated_by_key:
            merged_by_key[key] = row
    for row in generated_rows:
        key = _roster_key(row)
        if key is None:
            continue
        existing = merged_by_key.get(key)
        if existing is None:
            merged_by_key[key] = row
        elif not _row_is_verified_roster_evidence(existing):
            merged_by_key[key] = {**row, **existing}
    return sorted(
        merged_by_key.values(),
        key=lambda row: (
            not _row_is_verified_roster_evidence(row),
            -int(row.get("count") or 0),
            str(row.get("league") or ""),
            str(row.get("player_name") or ""),
        ),
    )


def _row_is_verified_roster_evidence(row: dict[str, Any]) -> bool:
    return (
        str(row.get("review_status") or "").upper()
        in {"APPROVED", "VERIFIED", "READY", "REVIEWED_VERIFIED"}
        and row.get("safe_to_apply") is True
        and bool(str(row.get("canonical_player_id") or "").strip())
        and bool(str(row.get("current_team_key") or "").strip())
        and bool(str(row.get("current_team_name") or "").strip())
        and str(row.get("roster_source_url") or "").startswith(("http://", "https://"))
    )


def _roster_key(row: dict[str, Any]) -> tuple[str, str] | None:
    league = str(row.get("league") or "").upper()
    player_name = str(row.get("player_name") or "").strip().lower()
    if not league or not player_name:
        return None
    normalized = " ".join(player_name.replace("-", " ").split())
    return (league, normalized)


def _round_placeholder_template_from_diagnostics(
    payload: Any,
    *,
    selected_leagues: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    selected = {league.upper() for league in selected_leagues}
    rows: list[dict[str, Any]] = []
    for item in payload.get("top_round_placeholder_games", []):
        if not isinstance(item, dict):
            continue
        league = str(item.get("league") or "").upper()
        if league not in selected:
            continue
        rows.append(
            {
                "league": league,
                "game_key": item.get("game_key"),
                "home_placeholder_team_key": item.get("home_team_key"),
                "away_placeholder_team_key": item.get("away_team_key"),
                "count": int(item.get("count") or 0),
                "example_tickers": list(item.get("example_tickers") or []),
                "review_status": "UNVERIFIED",
                "resolved_home_team_key": "",
                "resolved_home_team_name": "",
                "resolved_away_team_key": "",
                "resolved_away_team_name": "",
                "official_schedule_source_url": "",
                "safe_to_apply": False,
                "blocks_phase3ae_upgrade": True,
                "safety_note": (
                    "Resolve both placeholder teams from an official schedule/source "
                    "before any Phase 3AE link upgrade."
                ),
            }
        )
    return rows


def _manual_disambiguation_template(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    review_rows: list[dict[str, Any]] = []
    for row in rows:
        cause = str(row.get("primary_cause") or row.get("cause") or "")
        if cause not in {MULTI_LEG_CAUSE, PLAYER_PROP_CAUSE} and int(
            row.get("clean_candidate_count") or 0
        ) <= 0:
            continue
        review_rows.append(
            {
                "ticker": row.get("ticker"),
                "league": row.get("league"),
                "market_type": row.get("market_type"),
                "market_close_time": row.get("market_close_time"),
                "market_title": row.get("market_title"),
                "primary_cause": cause,
                "entities": row.get("entities", []),
                "candidate_games": row.get("game_candidates")
                or row.get("candidate_games")
                or [],
                "review_status": "UNVERIFIED",
                "chosen_game_key": "",
                "chosen_market_type": "",
                "verification_source_url": "",
                "safe_to_upgrade": False,
                "safety_note": (
                    "Do not mark safe unless exactly one team/time/market-type candidate "
                    "is verified."
                ),
            }
        )
    return review_rows


def _phase3ae_ready_gate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ready_rows: list[dict[str, Any]] = []
    blocked_counts: Counter[str] = Counter()
    for row in rows:
        reason = _blocked_reason(row)
        if reason is None:
            ready_rows.append(
                {
                    "ticker": row.get("ticker"),
                    "league": row.get("league"),
                    "market_type": row.get("market_type"),
                    "market_close_time": row.get("market_close_time"),
                    "candidate_games": row.get("game_candidates")
                    or row.get("candidate_games")
                    or [],
                }
            )
        else:
            blocked_counts[reason] += 1
    return {
        "phase3ah_auto_upgrades_created": 0,
        "phase3ae_ready_rows": len(ready_rows),
        "ready_rows": ready_rows[:100],
        "blocked_breakdown": [
            {"reason": reason, "count": count}
            for reason, count in blocked_counts.most_common()
        ],
        "policy": (
            "Rows are only informational. Rerun Phase 3AE to create verified links after "
            "schedule, alias, and roster evidence is complete."
        ),
    }


def _blocked_reason(row: dict[str, Any]) -> str | None:
    candidates = row.get("game_candidates") or row.get("candidate_games") or []
    if _is_player_prop(row):
        return "PLAYER_PROP_REQUIRES_ROSTER_MAPPING"
    if str(row.get("primary_cause") or row.get("cause")) == MULTI_LEG_CAUSE:
        return "MULTI_LEG_REQUIRES_MANUAL_DISAMBIGUATION"
    if str(row.get("primary_cause") or row.get("cause")) == NO_VERIFIED_WINDOW:
        return "SCHEDULE_WINDOW_BACKFILL_REQUIRED"
    if int(row.get("clean_candidate_count") or 0) != 1 or len(candidates) != 1:
        return "NOT_EXACTLY_ONE_CLEAN_CANDIDATE"
    if row.get("unmatched_entities"):
        return "UNMATCHED_ENTITIES_REMAIN"
    return None


def _player_prop_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_evidence_row(row) for row in rows if _is_player_prop(row)]


def _team_game_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_evidence_row(row) for row in rows if not _is_player_prop(row)]


def _evidence_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": row.get("ticker"),
        "league": row.get("league"),
        "market_type": row.get("market_type"),
        "market_close_time": row.get("market_close_time"),
        "primary_cause": row.get("primary_cause") or row.get("cause"),
        "entities": row.get("entities", []),
        "market_title": row.get("market_title"),
        "safe_action": row.get("safe_action")
        or "Do not auto-upgrade; collect verified evidence first.",
    }


def _is_player_prop(row: dict[str, Any]) -> bool:
    return (
        str(row.get("market_type") or "").upper() == "PLAYER_PROP"
        or str(row.get("primary_cause") or row.get("cause") or "") == PLAYER_PROP_CAUSE
    )


def _date_from_iso(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _parse_leagues(value: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = []
        for item in value:
            parts.extend(str(item).split(","))
    leagues = []
    for part in parts:
        normalized = part.strip().upper()
        if normalized and normalized not in leagues:
            leagues.append(normalized)
    if not leagues:
        raise ValueError("At least one league is required.")
    return leagues


def _next_commands(
    *,
    fetch_schedules: bool,
    schedule_windows: list[dict[str, Any]],
    round_placeholder_rows: int,
) -> list[str]:
    commands = [
        "kalshi-bot phase3ag-sports-link-repair-pass --output-dir reports/phase3ag",
    ]
    needs_fetch = any(
        not int(window.get("verified_games_in_db") or 0) for window in schedule_windows
    )
    if needs_fetch and not fetch_schedules:
        commands.append(
            "kalshi-bot phase3ah-sports-evidence-backfill "
            "--fetch-schedules --ingest-schedules"
        )
    if round_placeholder_rows:
        commands.append(
            "kalshi-bot phase3ah-round-placeholder-resolution "
            "--output-dir reports/phase3ah_sports"
        )
    commands.extend(
        [
            "kalshi-bot phase3ae-verified-sports-connector --output-dir reports/phase3ae",
            "kalshi-bot phase3ag-sports-ambiguity-coverage --output-dir reports/phase3ag",
        ]
    )
    return commands


def _recommended_next_action(
    *,
    schedule_windows: list[dict[str, Any]],
    schedule_fetches: list[dict[str, Any]],
    roster_rows: int,
    team_alias_rows: int,
    round_placeholder_rows: int,
    ready_rows: int,
    fetch_schedules: bool,
) -> str:
    backfill_needed = [
        window
        for window in schedule_windows
        if not int(window.get("verified_games_in_db") or 0)
    ]
    if backfill_needed and not fetch_schedules:
        return (
            "Run Phase 3AH with --fetch-schedules --ingest-schedules to backfill the "
            "specific failed close-date windows, then rerun Phase 3AE."
        )
    if backfill_needed and fetch_schedules:
        return (
            "Some failed windows still have no verified games after fetch. Expand soccer "
            "competition coverage or add manual verified schedule rows, then rerun Phase 3AE."
        )
    if round_placeholder_rows:
        return (
            "Resolve round placeholder games with official home/away teams before rerunning "
            "Phase 3AE; placeholder games remain blocked by design."
        )
    if schedule_windows and (roster_rows or team_alias_rows):
        return (
            "Review the alias and roster templates before rerunning Phase 3AE; player "
            "props remain blocked until roster evidence is verified."
        )
    if schedule_fetches and (roster_rows or team_alias_rows):
        return (
            "Review the alias and roster templates before rerunning Phase 3AE; player "
            "props remain blocked until roster evidence is verified."
        )
    if ready_rows:
        return "Rerun Phase 3AE; evidence rows are ready for the verified connector gate."
    return "Continue collecting verified schedule, alias, and roster evidence before Phase 3AE."


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AH Verified Sports Evidence Backfill",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Repair source: `{payload['repair_source_path']}`",
        f"- Roster candidate diagnostics: `{payload['roster_candidate_diagnostics_path']}`",
        f"- Fetch schedules: {payload['fetch_schedules']}",
        f"- Ingest schedules: {payload['ingest_schedules']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Safety Gate",
            "",
            "- Phase 3AH auto-upgrades created: "
            f"{payload['auto_upgrade_policy']['auto_upgrades_created']}",
            f"- Phase 3AE ready rows: {payload['phase3ae_ready_gate']['phase3ae_ready_rows']}",
            f"- Policy: {payload['auto_upgrade_policy']['policy']}",
            "",
            "## Cause Breakdown",
            "",
            "| Cause | Count |",
            "| --- | ---: |",
        ]
    )
    for row in payload["cause_breakdown"]:
        lines.append(f"| {row['cause']} | {row['count']} |")
    lines.extend(
        [
            "",
            "## Schedule Backfill Windows",
            "",
            "| League | Start | End | Days | Rows | Verified games | Status | Command |",
            "| --- | --- | --- | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in payload["schedule_backfill_plan"][:50]:
        lines.append(
            f"| {row['league']} | {row['start_date']} | {row['end_date']} | "
            f"{row['days_ahead']} | {row['source_failed_rows']} | "
            f"{row.get('verified_games_in_db')} | {row.get('schedule_evidence_status')} | "
            f"`{row['command']}` |"
        )
    if not payload["schedule_backfill_plan"]:
        lines.append("| none |  |  | 0 | 0 | 0 |  |  |")
    lines.extend(
        [
            "",
            "## Manual Evidence Templates",
            "",
            f"- Team/entity alias review rows: {summary['team_alias_review_rows']}",
            f"- Roster review rows: {summary['roster_review_rows']}",
            f"- Manual disambiguation rows: {summary['manual_disambiguation_rows']}",
            "- Round placeholder resolution rows: "
            f"{summary['round_placeholder_resolution_rows']}",
            "",
            "## Round Placeholder Resolution",
            "",
            "| League | Game key | Home placeholder | Away placeholder | Count |",
            "| --- | --- | --- | --- | ---: |",
        ]
    )
    for row in payload["round_placeholder_resolution_template"][:30]:
        lines.append(
            f"| {row['league']} | {row['game_key']} | "
            f"{row['home_placeholder_team_key']} | {row['away_placeholder_team_key']} | "
            f"{row['count']} |"
        )
    if not payload["round_placeholder_resolution_template"]:
        lines.append("| none |  |  |  | 0 |")
    lines.extend(
        [
            "## Schedule Fetch Results",
            "",
            "| League | Start | Days | Games inserted | Errors |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    for row in payload["schedule_fetch_results"]:
        errors = len(row.get("errors") or [])
        inserted = row.get("summary", {}).get("games_inserted", 0)
        lines.append(
            f"| {row['league']} | {row['start_date']} | {row['days_ahead']} | "
            f"{inserted} | {errors} |"
        )
    if not payload["schedule_fetch_results"]:
        lines.append("| not run |  | 0 | 0 | 0 |")
    lines.extend(["", "## Next Commands", ""])
    for command in payload["next_commands"]:
        lines.append(f"- `{command}`")
    lines.extend(["", "## Recommended Next Action", "", payload["recommended_next_action"], ""])
    return "\n".join(lines)
