from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE_3AH_ROUND_PLACEHOLDER_VERSION = "phase3ah_round_placeholder_resolution_v1"
DEFAULT_TEMPLATE_PATH = Path(
    "reports/phase3ah_sports/phase3ah_round_placeholder_resolution_template.json"
)
DEFAULT_OUTPUT_DIR = Path("reports/phase3ah_sports")
ESPN_SOCCER_SUMMARY_URL_TEMPLATE = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/{competition}/summary?event={event_id}"
)
ESPN_SOCCER_MATCH_URL_TEMPLATE = "https://www.espn.com/soccer/match/_/gameId/{event_id}"
ESPN_MLB_SUMMARY_URL_TEMPLATE = (
    "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/summary?event={event_id}"
)
ESPN_MLB_GAME_URL_TEMPLATE = "https://www.espn.com/mlb/game/_/gameId/{event_id}"
PLACEHOLDER_STATUS = "SOURCE_STILL_PLACEHOLDER"
RESOLVED_STATUS = "RESOLVED_FROM_SOURCE"

SummaryFetcher = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class Phase3AHRoundPlaceholderArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    filled_template_path: Path


def build_phase3ah_round_placeholder_resolution(
    *,
    template_path: Path = DEFAULT_TEMPLATE_PATH,
    fetcher: SummaryFetcher | None = None,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Resolve bracket placeholders when the public source has real teams.

    This produces evidence only. It does not mutate sports games or create verified
    links; Phase 3AE remains responsible for applying the clean team/time/market gate.
    """

    rows = _load_template_rows(template_path)
    fetch = fetcher or (lambda url: _fetch_espn_summary(url, timeout_seconds=timeout_seconds))
    resolved_rows = [_resolve_row(row, fetcher=fetch) for row in rows]
    summary = {
        "rows_reviewed": len(resolved_rows),
        "safe_to_apply_rows": sum(1 for row in resolved_rows if row["safe_to_apply"] is True),
        "still_placeholder_rows": sum(
            1 for row in resolved_rows if row.get("source_status") == PLACEHOLDER_STATUS
        ),
        "fetch_error_rows": sum(
            1 for row in resolved_rows if str(row.get("source_status") or "").endswith("_ERROR")
        ),
        "unsupported_rows": sum(
            1 for row in resolved_rows if row.get("source_status") == "UNSUPPORTED_GAME_KEY"
        ),
        "phase3ah_auto_upgrades_created": 0,
    }
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AH_ROUND_PLACEHOLDER_RESOLUTION",
        "phase_version": PHASE_3AH_ROUND_PLACEHOLDER_VERSION,
        "mode": "PAPER_ONLY_ROUND_PLACEHOLDER_EVIDENCE",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "input_template_path": str(template_path),
        "auto_upgrade_policy": {
            "phase3ah_placeholder_resolution_creates_verified_links": False,
            "auto_upgrades_created": 0,
            "policy": (
                "Rows marked safe_to_apply only mean both source competitors are real "
                "teams. Phase 3AE must still re-check team, time, and market type "
                "before creating or upgrading a sports link."
            ),
        },
        "summary": summary,
        "rows": resolved_rows,
        "next_action": _next_action(summary),
    }


def write_phase3ah_round_placeholder_resolution_report(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    template_path: Path = DEFAULT_TEMPLATE_PATH,
    fetcher: SummaryFetcher | None = None,
    timeout_seconds: float = 20.0,
) -> Phase3AHRoundPlaceholderArtifactSet:
    payload = build_phase3ah_round_placeholder_resolution(
        template_path=template_path,
        fetcher=fetcher,
        timeout_seconds=timeout_seconds,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ah_round_placeholder_resolution_report.json"
    markdown_path = output_dir / "phase3ah_round_placeholder_resolution_report.md"
    filled_template_path = output_dir / "phase3ah_round_placeholder_resolution_filled.json"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    filled_template_path.write_text(
        json.dumps(payload["rows"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return Phase3AHRoundPlaceholderArtifactSet(
        output_dir,
        json_path,
        markdown_path,
        filled_template_path,
    )


def _load_template_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing round placeholder template: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("rows") or payload.get("round_placeholder_resolution_template") or []
    else:
        rows = payload
    return [row for row in rows if isinstance(row, dict)]


def _resolve_row(row: dict[str, Any], *, fetcher: SummaryFetcher) -> dict[str, Any]:
    resolved = dict(row)
    source = _source_from_game_key(str(row.get("game_key") or ""))
    resolved.setdefault("safe_to_apply", False)
    resolved.setdefault("blocks_phase3ae_upgrade", True)
    resolved["source_checked_at"] = utc_now().isoformat()
    if source is None:
        resolved.update(
            {
                "source_status": "UNSUPPORTED_GAME_KEY",
                "review_status": "UNVERIFIED",
                "safe_to_apply": False,
                "blocks_phase3ae_upgrade": True,
                "resolution_note": (
                    "The game_key is not a supported ESPN event key, so this resolver "
                    "cannot verify both teams."
                ),
                "next_action": "Resolve manually from an official/source schedule row.",
            }
        )
        return resolved

    resolved["source_summary_url"] = source["summary_url"]
    resolved["source_event_url"] = source["match_url"]
    try:
        payload = fetcher(source["summary_url"])
    except Exception as exc:  # noqa: BLE001 - report should capture fetch diagnostics.
        resolved.update(
            {
                "source_status": "FETCH_ERROR",
                "review_status": "UNVERIFIED",
                "safe_to_apply": False,
                "blocks_phase3ae_upgrade": True,
                "resolution_note": f"Could not fetch source event summary: {exc}",
                "next_action": "Rerun the resolver or verify the source manually.",
            }
        )
        return resolved

    competitors = _competitors_from_summary(payload)
    home = _competitor_by_side(competitors, "home")
    away = _competitor_by_side(competitors, "away")
    if home is None or away is None:
        resolved.update(
            {
                "source_status": "SOURCE_MISSING_COMPETITORS",
                "review_status": "UNVERIFIED",
                "safe_to_apply": False,
                "blocks_phase3ae_upgrade": True,
                "resolution_note": (
                    "The source response did not contain exactly one home and away "
                    "competitor."
                ),
                "next_action": "Rerun after the source publishes both competitors.",
            }
        )
        return resolved

    home_team = _team_from_competitor(home, league=str(row.get("league") or "SOCCER"))
    away_team = _team_from_competitor(away, league=str(row.get("league") or "SOCCER"))
    resolved.update(
        {
            "source_home_team_key": home_team["team_key"],
            "source_home_team_name": home_team["team_name"],
            "source_home_abbreviation": home_team["abbreviation"],
            "source_away_team_key": away_team["team_key"],
            "source_away_team_name": away_team["team_name"],
            "source_away_abbreviation": away_team["abbreviation"],
            "official_schedule_source_url": source["match_url"],
        }
    )
    if home_team["is_placeholder"] or away_team["is_placeholder"]:
        resolved.update(
            {
                "source_status": PLACEHOLDER_STATUS,
                "review_status": "UNVERIFIED",
                "safe_to_apply": False,
                "blocks_phase3ae_upgrade": True,
                "resolution_note": (
                    "The source still lists bracket placeholders instead of real teams."
                ),
                "next_action": (
                    "Wait until the bracket advances and rerun "
                    "phase3ah-round-placeholder-resolution."
                ),
            }
        )
        return resolved

    resolved.update(
        {
            "source_status": RESOLVED_STATUS,
            "review_status": "VERIFIED",
            "resolved_home_team_key": home_team["team_key"],
            "resolved_home_team_name": home_team["team_name"],
            "resolved_away_team_key": away_team["team_key"],
            "resolved_away_team_name": away_team["team_name"],
            "safe_to_apply": True,
            "blocks_phase3ae_upgrade": False,
            "resolution_note": (
                "Both source competitors are real teams. Phase 3AE must still verify "
                "team, time, and market type before any link upgrade."
            ),
            "next_action": "Rerun Phase 3AE after schedule ingestion refreshes the game row.",
        }
    )
    return resolved


def _source_from_game_key(game_key: str) -> dict[str, str] | None:
    soccer_match = re.match(
        r"^SOCCER:espn:(?P<competition>[^:]+):(?P<event_id>\d+)$",
        game_key,
    )
    if soccer_match is not None:
        competition = soccer_match.group("competition")
        event_id = soccer_match.group("event_id")
        return {
            "event_id": event_id,
            "competition": competition,
            "summary_url": ESPN_SOCCER_SUMMARY_URL_TEMPLATE.format(
                competition=competition,
                event_id=event_id,
            ),
            "match_url": ESPN_SOCCER_MATCH_URL_TEMPLATE.format(event_id=event_id),
        }

    mlb_match = re.match(r"^MLB:espn:mlb:(?P<event_id>\d+)$", game_key)
    if mlb_match is not None:
        event_id = mlb_match.group("event_id")
        return {
            "event_id": event_id,
            "competition": "mlb",
            "summary_url": ESPN_MLB_SUMMARY_URL_TEMPLATE.format(event_id=event_id),
            "match_url": ESPN_MLB_GAME_URL_TEMPLATE.format(event_id=event_id),
        }

    return None


def _fetch_espn_summary(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    response = httpx.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _competitors_from_summary(payload: dict[str, Any]) -> list[dict[str, Any]]:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    competitions = (
        header.get("competitions") if isinstance(header.get("competitions"), list) else []
    )
    competition = next((item for item in competitions if isinstance(item, dict)), {})
    competitors = competition.get("competitors") if isinstance(competition, dict) else []
    return [item for item in competitors if isinstance(item, dict)]


def _competitor_by_side(
    competitors: list[dict[str, Any]],
    side: str,
) -> dict[str, Any] | None:
    for competitor in competitors:
        if str(competitor.get("homeAway") or "").lower() == side:
            return competitor
    return None


def _team_from_competitor(competitor: dict[str, Any], *, league: str) -> dict[str, Any]:
    normalized_league = str(league or "SOCCER").upper()
    team = competitor.get("team") if isinstance(competitor.get("team"), dict) else {}
    key_source = (
        team.get("abbreviation")
        or team.get("shortDisplayName")
        or team.get("displayName")
        or team.get("name")
        or team.get("id")
        or competitor.get("id")
    )
    name = str(team.get("displayName") or team.get("name") or key_source or "Unknown").strip()
    abbreviation = str(team.get("abbreviation") or "").strip()
    return {
        "team_key": f"{normalized_league}:{_slug(str(key_source or name))}",
        "team_name": name,
        "abbreviation": abbreviation,
        "is_placeholder": _is_placeholder_team(team),
    }


def _is_placeholder_team(team: dict[str, Any]) -> bool:
    text = " ".join(
        str(team.get(key) or "")
        for key in ("abbreviation", "displayName", "shortDisplayName", "name", "location")
    ).lower()
    normalized = text.replace("-", " ")
    return any(
        token in normalized
        for token in (
            "round of",
            "winner",
            "tbd",
            "to be determined",
            "rd16",
            "rd32",
            "rd 16",
            "rd 32",
        )
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown"


def _next_action(summary: dict[str, int]) -> str:
    if summary["safe_to_apply_rows"]:
        return (
            "Refresh schedule ingestion for the resolved games, then rerun Phase 3AE; "
            "links still require the clean team + time + market-type gate."
        )
    if summary["still_placeholder_rows"]:
        return (
            "The source still has bracket placeholders. Rerun this resolver after the "
            "rounds feeding those games are complete."
        )
    if summary["fetch_error_rows"]:
        return "Fix fetch errors or rerun the resolver before Phase 3AE."
    return "No round placeholder rows required source resolution."


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AH Round Placeholder Resolution",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Input template: `{payload['input_template_path']}`",
        "- Auto-upgrades created: 0",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Rows",
            "",
            "| Game key | Source status | Home | Away | Safe | Next action |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["rows"]:
        home = row.get("source_home_team_name") or row.get("resolved_home_team_name") or ""
        away = row.get("source_away_team_name") or row.get("resolved_away_team_name") or ""
        lines.append(
            f"| {row.get('game_key')} | {row.get('source_status')} | {home} | {away} | "
            f"{row.get('safe_to_apply')} | {row.get('next_action')} |"
        )
    if not payload["rows"]:
        lines.append("| none |  |  |  | False |  |")
    lines.extend(["", "## Next Action", "", payload["next_action"], ""])
    return "\n".join(lines)
