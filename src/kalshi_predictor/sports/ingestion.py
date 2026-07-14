import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.sports.repository import (
    insert_sports_injury,
    insert_sports_odds,
    insert_sports_team_stat,
    normalize_league,
    upsert_sports_game,
    upsert_sports_team,
)


@dataclass(frozen=True)
class SportsIngestionSummary:
    league: str
    source: str
    teams_seen: int = 0
    teams_inserted: int = 0
    games_seen: int = 0
    games_inserted: int = 0
    team_stats_inserted: int = 0
    injuries_inserted: int = 0
    odds_inserted: int = 0
    errors: list[str] = field(default_factory=list)
    message: str = "Sports ingestion completed."


def ingest_sports_file(
    session: Session,
    *,
    league: str,
    input_file: str | Path,
) -> SportsIngestionSummary:
    path = Path(input_file)
    if not path.exists():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Sports JSON must be an object with teams/games lists.")
        return ingest_sports_payload(session, payload, league=league, source=f"file:{path.name}")
    if suffix == ".csv":
        return ingest_sports_csv(session, league=league, input_file=path)
    raise ValueError("Sports input file must be .json or .csv.")


def ingest_sports_payload(
    session: Session,
    payload: dict[str, Any],
    *,
    league: str,
    source: str = "manual",
) -> SportsIngestionSummary:
    resolved_league = normalize_league(payload.get("league") or league)
    teams = _list(payload.get("teams"))
    games = _list(payload.get("games"))
    stats = _list(payload.get("team_stats") or payload.get("stats"))
    injuries = _list(payload.get("injuries"))
    odds = _list(payload.get("odds"))
    return _ingest_batches(
        session,
        league=resolved_league,
        source=source,
        teams=teams,
        games=games,
        stats=stats,
        injuries=injuries,
        odds=odds,
    )


def ingest_sports_csv(
    session: Session,
    *,
    league: str,
    input_file: str | Path,
) -> SportsIngestionSummary:
    path = Path(input_file)
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    teams: list[dict[str, Any]] = []
    games: list[dict[str, Any]] = []
    stats: list[dict[str, Any]] = []
    injuries: list[dict[str, Any]] = []
    odds: list[dict[str, Any]] = []
    for row in rows:
        record_type = str(row.get("record_type") or "").strip().lower()
        if record_type == "team" or _looks_like_team(row):
            teams.append(row)
        elif record_type == "game" or _looks_like_game(row):
            games.append(row)
        elif record_type in {"team_stat", "stat"} or _looks_like_stat(row):
            stats.append(row)
        elif record_type == "injury" or _looks_like_injury(row):
            injuries.append(row)
        elif record_type == "odds" or _looks_like_odds(row):
            odds.append(row)
    return _ingest_batches(
        session,
        league=normalize_league(league),
        source=f"file:{path.name}",
        teams=teams,
        games=games,
        stats=stats,
        injuries=injuries,
        odds=odds,
    )


def _ingest_batches(
    session: Session,
    *,
    league: str,
    source: str,
    teams: list[dict[str, Any]],
    games: list[dict[str, Any]],
    stats: list[dict[str, Any]],
    injuries: list[dict[str, Any]],
    odds: list[dict[str, Any]],
) -> SportsIngestionSummary:
    errors: list[str] = []
    teams_inserted = 0
    games_inserted = 0
    stats_inserted = 0
    injuries_inserted = 0
    odds_inserted = 0

    for row in teams:
        try:
            _, created = upsert_sports_team(session, row, league=league)
            teams_inserted += 1 if created else 0
        except Exception as exc:  # noqa: BLE001 - one bad row should not stop ingestion.
            errors.append(f"team {row.get('team_name') or row.get('name')}: {exc}")
    for row in games:
        try:
            _, created = upsert_sports_game(session, row, league=league)
            games_inserted += 1 if created else 0
        except Exception as exc:  # noqa: BLE001
            errors.append(f"game {row.get('game_key') or row.get('id')}: {exc}")
    for row in stats:
        try:
            insert_sports_team_stat(session, row, league=league)
            stats_inserted += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"stat {row.get('team_key') or row.get('team_name')}: {exc}")
    for row in injuries:
        try:
            insert_sports_injury(session, row, league=league)
            injuries_inserted += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"injury {row.get('player_name') or row.get('player')}: {exc}")
    for row in odds:
        try:
            insert_sports_odds(session, row, league=league)
            odds_inserted += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"odds {row.get('game_key') or row.get('id')}: {exc}")

    return SportsIngestionSummary(
        league=league,
        source=source,
        teams_seen=len(teams),
        teams_inserted=teams_inserted,
        games_seen=len(games),
        games_inserted=games_inserted,
        team_stats_inserted=stats_inserted,
        injuries_inserted=injuries_inserted,
        odds_inserted=odds_inserted,
        errors=errors,
    )


def _list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _looks_like_team(row: dict[str, Any]) -> bool:
    return bool((row.get("team_name") or row.get("name")) and not row.get("home_team"))


def _looks_like_game(row: dict[str, Any]) -> bool:
    return bool(row.get("home_team") or row.get("home_team_key")) and bool(
        row.get("away_team") or row.get("away_team_key")
    )


def _looks_like_stat(row: dict[str, Any]) -> bool:
    return any(row.get(key) not in (None, "") for key in ("wins", "losses", "rating"))


def _looks_like_injury(row: dict[str, Any]) -> bool:
    return bool(row.get("player_name") or row.get("player"))


def _looks_like_odds(row: dict[str, Any]) -> bool:
    return any(
        row.get(key) not in (None, "")
        for key in ("home_moneyline", "away_moneyline", "spread", "total")
    )

