from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import Market, MarketLeg, SportsGame, SportsMarketLink, SportsTeam
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3af import DEFAULT_SOCCER_COMPETITIONS
from kalshi_predictor.sports.aliases import canonical_alias_suggestions
from kalshi_predictor.sports.repository import sports_team_aliases
from kalshi_predictor.utils.time import utc_now

PHASE_3AJ_VERSION = "phase3aj_v1"
PARTIAL_SOURCE = "partial_market_derived"
DERIVED_SOURCE = "kalshi_event_derived"
VERIFIED_SOURCE = "verified_schedule"
TEMPLATE_SOURCE = "phase3aj_competition_provenance_template"

GENERIC_ENTITY_FRAGMENTS = (
    "both teams",
    "goals scored",
    "goal scored",
    "runs scored",
    "points scored",
    "over ",
    "under ",
    "at least",
    "more than",
    "fewer than",
    "wins by",
    "total",
)

SOCCER_COMPETITION_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("world cup qualifying", ("fifa.worldq",)),
    ("world cup qualifier", ("fifa.worldq",)),
    ("world cup", ("fifa.world", "fifa.worldq")),
    ("gold cup", ("concacaf.gold",)),
    ("nations league", ("concacaf.nations.league",)),
    ("champions league", ("uefa.champions",)),
    ("europa league", ("uefa.europa",)),
    ("conference league", ("uefa.europa.conf",)),
    ("premier league", ("eng.1",)),
    ("epl", ("eng.1",)),
    ("la liga", ("esp.1",)),
    ("serie a", ("ita.1",)),
    ("bundesliga", ("ger.1",)),
    ("ligue 1", ("fra.1",)),
    ("mls", ("usa.1",)),
    ("liga mx", ("mex.1",)),
)


@dataclass(frozen=True)
class Phase3AJArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    alias_suggestions_path: Path
    competition_template_path: Path


def build_sports_alias_provenance_repair(
    session: Session,
    *,
    limit: int | None = None,
    apply_aliases: bool = False,
) -> dict[str, Any]:
    """Classify unresolved sports partials and produce alias/competition repair inputs.

    This phase only writes local alias metadata when explicitly requested. It never creates
    orders, live credentials, or exchange-side actions.
    """
    session.flush()
    partial_links = _unresolved_partial_links(session, limit=limit)
    tickers = sorted({link.ticker for link in partial_links})
    markets = {
        market.ticker: market
        for market in session.scalars(select(Market).where(Market.ticker.in_(tickers)))
    } if tickers else {}
    legs_by_ticker = _sports_legs_by_ticker(session, tickers)
    teams = list(
        session.scalars(select(SportsTeam).order_by(SportsTeam.league, SportsTeam.team_name))
    )
    games = list(
        session.scalars(select(SportsGame).order_by(SportsGame.league, SportsGame.game_key))
    )
    alias_index = _alias_index(teams)
    verified_games_by_league = Counter(game.league for game in games if _game_is_verified(game))
    rows: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    all_text_by_ticker: dict[str, str] = {}

    links_by_ticker: dict[str, list[SportsMarketLink]] = defaultdict(list)
    for link in partial_links:
        links_by_ticker[link.ticker].append(link)

    for ticker in tickers:
        market = markets.get(ticker)
        links = links_by_ticker[ticker]
        legs = legs_by_ticker.get(ticker, [])
        text = _market_text(market, links=links, legs=legs)
        all_text_by_ticker[ticker] = text
        league = _preferred_league(links, text)
        entities = _entities_for_market(legs=legs, text=text)
        team_matches = _team_matches(
            entities,
            league=league,
            alias_index=alias_index,
        )
        competition_hints = _soccer_competition_hints(text) if league == "SOCCER" else []
        reason = _classify_gap(
            market=market,
            league=league,
            entities=entities,
            team_matches=team_matches,
            verified_games_by_league=verified_games_by_league,
            text=text,
        )
        reason_counts[reason] += 1
        rows.append(
            {
                "ticker": ticker,
                "league": league,
                "reason": reason,
                "market_title": market.title if market else None,
                "market_status": market.status if market else None,
                "close_time": (
                    market.close_time.isoformat() if market and market.close_time else None
                ),
                "partial_link_rows": len(links),
                "sports_legs": len(legs),
                "entities": entities,
                "matched_teams": team_matches["matched"],
                "unmatched_entities": team_matches["unmatched"],
                "ambiguous_entities": team_matches["ambiguous"],
                "competition_hints": competition_hints,
                "next_action": _next_action_for_reason(reason, league=league),
            }
        )

    alias_suggestions = _alias_suggestions(
        teams,
        all_text_by_ticker=all_text_by_ticker,
        apply_aliases=apply_aliases,
    )
    competition_suggestions = _competition_suggestions(rows)
    template = _competition_template(competition_suggestions, alias_suggestions)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AJ",
        "phase_version": PHASE_3AJ_VERSION,
        "mode": "PAPER_ONLY_SPORTS_ALIAS_COMPETITION_REPAIR",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "apply_aliases": apply_aliases,
        "summary": {
            "partial_link_rows_reviewed": len(partial_links),
            "partial_markets_reviewed": len(tickers),
            "multi_leg_markets": reason_counts[
                "MULTI_LEG_MARKET_REQUIRES_COMPETITION_PROVENANCE"
            ],
            "soccer_markets": sum(1 for row in rows if row["league"] == "SOCCER"),
            "alias_suggestions": len(alias_suggestions),
            "aliases_applied": sum(1 for row in alias_suggestions if row["applied"]),
            "competition_suggestions": len(competition_suggestions),
            "verified_games_by_league": dict(sorted(verified_games_by_league.items())),
        },
        "reason_breakdown": [
            {"reason": reason, "count": count}
            for reason, count in reason_counts.most_common()
        ],
        "alias_suggestions": alias_suggestions,
        "competition_suggestions": competition_suggestions,
        "rows": rows,
        "competition_template": template,
        "recommended_next_action": _recommended_next_action(reason_counts, alias_suggestions),
    }


def write_phase3aj_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3aj"),
    limit: int | None = None,
    apply_aliases: bool = False,
) -> Phase3AJArtifactSet:
    payload = build_sports_alias_provenance_repair(
        session,
        limit=limit,
        apply_aliases=apply_aliases,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3aj_sports_alias_provenance.json"
    markdown_path = output_dir / "phase3aj_sports_alias_provenance.md"
    alias_path = output_dir / "sports_alias_suggestions.json"
    template_path = output_dir / "sports_competition_provenance_template.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    alias_path.write_text(
        json.dumps(payload["alias_suggestions"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    template_path.write_text(
        json.dumps(payload["competition_template"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return Phase3AJArtifactSet(output_dir, json_path, markdown_path, alias_path, template_path)


def _unresolved_partial_links(session: Session, *, limit: int | None) -> list[SportsMarketLink]:
    links = list(session.scalars(select(SportsMarketLink).order_by(SportsMarketLink.id)))
    upgraded = {
        link.ticker
        for link in links
        if _link_provenance(link) in {DERIVED_SOURCE, VERIFIED_SOURCE}
    }
    partial = [
        link
        for link in links
        if _link_provenance(link) == PARTIAL_SOURCE and link.ticker not in upgraded
    ]
    return partial[:limit] if limit is not None else partial


def _sports_legs_by_ticker(session: Session, tickers: list[str]) -> dict[str, list[MarketLeg]]:
    if not tickers:
        return {}
    rows = list(
        session.scalars(
            select(MarketLeg)
            .where(MarketLeg.ticker.in_(tickers), MarketLeg.category == "sports")
            .order_by(MarketLeg.ticker, MarketLeg.leg_index)
        )
    )
    grouped: dict[str, list[MarketLeg]] = defaultdict(list)
    for row in rows:
        grouped[row.ticker].append(row)
    return dict(grouped)


def _market_text(
    market: Market | None,
    *,
    links: list[SportsMarketLink],
    legs: list[MarketLeg],
) -> str:
    parts: list[object] = []
    if market is not None:
        parts.extend(
            [
                market.ticker,
                market.title,
                market.subtitle,
                market.event_ticker,
                market.series_ticker,
                market.rules_primary,
                market.rules_secondary,
            ]
        )
    for link in links:
        parts.extend([link.ticker, link.league, link.game_key, link.market_type])
    for leg in legs:
        parts.extend([leg.raw_text, leg.entity_name, leg.market_type])
    return " ".join(str(part or "") for part in parts).lower()


def _preferred_league(links: list[SportsMarketLink], text: str) -> str:
    counter = Counter(
        link.league for link in links if link.league not in {"", "ALL", "SPORTS", "UNKNOWN"}
    )
    if counter:
        return counter.most_common(1)[0][0]
    if any(term in text for term in ("soccer", "fifa", "uefa", "premier league", "world cup")):
        return "SOCCER"
    return "SPORTS"


def _entities_for_market(*, legs: list[MarketLeg], text: str) -> list[str]:
    entities: list[str] = []
    for leg in legs:
        entity = _clean_entity(leg.entity_name or leg.raw_text)
        if entity:
            entities.append(entity)
    if not entities and "," in text:
        for part in text.split(","):
            entity = _clean_entity(part)
            if entity:
                entities.append(entity)
    return _dedupe_keep_order(entities)


def _clean_entity(value: str | None) -> str | None:
    text = str(value or "").strip()
    text = re.sub(r"^(yes|no)\s+", "", text, flags=re.IGNORECASE)
    text = text.split(":", 1)[0]
    text = re.sub(r"\b\d+(\.\d+)?\+?\b", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ,-")
    lowered = text.lower()
    if not text or len(text) < 2:
        return None
    if any(fragment in lowered for fragment in GENERIC_ENTITY_FRAGMENTS):
        return None
    return text


def _team_matches(
    entities: list[str],
    *,
    league: str,
    alias_index: dict[str, dict[str, list[SportsTeam]]],
) -> dict[str, list[dict[str, Any]]]:
    matched: list[dict[str, Any]] = []
    unmatched: list[str] = []
    ambiguous: list[dict[str, Any]] = []
    index = alias_index.get(league, {})
    for entity in entities:
        key = _normalize(entity)
        teams = index.get(key, [])
        if len(teams) == 1:
            team = teams[0]
            matched.append(
                {
                    "entity": entity,
                    "team_key": team.team_key,
                    "team_name": team.team_name,
                    "match_type": "alias_exact",
                }
            )
        elif len(teams) > 1:
            ambiguous.append(
                {
                    "entity": entity,
                    "candidate_team_keys": [team.team_key for team in teams],
                }
            )
        else:
            unmatched.append(entity)
    return {"matched": matched, "unmatched": unmatched, "ambiguous": ambiguous}


def _alias_index(teams: list[SportsTeam]) -> dict[str, dict[str, list[SportsTeam]]]:
    index: dict[str, dict[str, list[SportsTeam]]] = defaultdict(lambda: defaultdict(list))
    for team in teams:
        for alias in sports_team_aliases(team):
            normalized = _normalize(alias)
            if normalized:
                index[team.league][normalized].append(team)
    return {league: dict(values) for league, values in index.items()}


def _classify_gap(
    *,
    market: Market | None,
    league: str,
    entities: list[str],
    team_matches: dict[str, list[Any]],
    verified_games_by_league: Counter[str],
    text: str,
) -> str:
    if market is None:
        return "MISSING_MARKET_ROW"
    if _is_multileg_market(text=text, entities=entities, team_matches=team_matches):
        return "MULTI_LEG_MARKET_REQUIRES_COMPETITION_PROVENANCE"
    if league == "SOCCER" and verified_games_by_league.get("SOCCER", 0) == 0:
        return "SOCCER_COMPETITION_PROVENANCE_MISSING"
    if team_matches["ambiguous"]:
        return "AMBIGUOUS_TEAM_ALIAS"
    if team_matches["unmatched"]:
        return "TEAM_ALIAS_OR_SCHEDULE_GAP"
    if verified_games_by_league.get(league, 0) == 0:
        return "VERIFIED_SCHEDULE_MISSING"
    return "READY_FOR_PHASE3AE_RETRY"


def _is_multileg_market(
    *,
    text: str,
    entities: list[str],
    team_matches: dict[str, list[Any]],
) -> bool:
    team_keys = {row["team_key"] for row in team_matches["matched"]}
    yes_no_count = len(re.findall(r"\b(yes|no)\b", text))
    if "multigame" in text or "multi game" in text or "crosscategory" in text:
        return True
    if len(entities) >= 3 or len(team_keys) >= 3:
        return True
    return text.count(",") >= 2 and yes_no_count >= 3


def _soccer_competition_hints(text: str) -> list[str]:
    codes: list[str] = []
    for phrase, phrase_codes in SOCCER_COMPETITION_HINTS:
        if phrase in text:
            codes.extend(phrase_codes)
    return _dedupe_keep_order(codes)


def _alias_suggestions(
    teams: list[SportsTeam],
    *,
    all_text_by_ticker: dict[str, str],
    apply_aliases: bool,
) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    for team in teams:
        raw = decode_json(team.raw_json)
        raw_aliases = _raw_aliases(raw)
        missing = canonical_alias_suggestions(
            league=team.league,
            team_name=team.team_name,
            existing_aliases=raw_aliases,
        )
        for alias in missing:
            examples = [
                ticker for ticker, text in all_text_by_ticker.items() if _alias_in_text(text, alias)
            ][:5]
            if not examples:
                continue
            suggestions.append(
                {
                    "league": team.league,
                    "team_key": team.team_key,
                    "team_name": team.team_name,
                    "alias": alias,
                    "confidence": "0.95",
                    "examples": examples,
                    "applied": False,
                }
            )
        if apply_aliases:
            applied = _apply_aliases(
                team,
                [row["alias"] for row in suggestions if row["team_key"] == team.team_key],
            )
            for row in suggestions:
                if row["team_key"] == team.team_key and row["alias"] in applied:
                    row["applied"] = True
    return suggestions


def _raw_aliases(raw: dict[str, Any]) -> list[str]:
    aliases = raw.get("aliases")
    if isinstance(aliases, list):
        return [str(alias) for alias in aliases if alias]
    if isinstance(aliases, str):
        return [part.strip() for part in aliases.split(",") if part.strip()]
    return []


def _apply_aliases(team: SportsTeam, aliases: list[str]) -> set[str]:
    if not aliases:
        return set()
    raw = decode_json(team.raw_json)
    existing = {_normalize(alias) for alias in _raw_aliases(raw)}
    current = _raw_aliases(raw)
    applied: set[str] = set()
    for alias in aliases:
        normalized = _normalize(alias)
        if not normalized or normalized in existing:
            continue
        current.append(alias)
        existing.add(normalized)
        applied.add(alias)
    if applied:
        raw["aliases"] = current
        raw["alias_repair_phase"] = "3AJ"
        raw["alias_repair_updated_at"] = utc_now().isoformat()
        team.raw_json = encode_json(raw)
        team.updated_at = utc_now()
    return applied


def _competition_suggestions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    examples_by_code: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if row["league"] != "SOCCER":
            continue
        hints = row["competition_hints"] or list(DEFAULT_SOCCER_COMPETITIONS[:8])
        for code in hints:
            examples_by_code[code].append(row["ticker"])
    suggestions = [
        {
            "league": "SOCCER",
            "competition_code": code,
            "reason": "detected_from_market_text"
            if any(code in row["competition_hints"] for row in rows)
            else "recommended_default_soccer_coverage",
            "example_count": len(tickers),
            "examples": tickers[:8],
        }
        for code, tickers in sorted(
            examples_by_code.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )
    ]
    return suggestions


def _competition_template(
    competition_suggestions: list[dict[str, Any]],
    alias_suggestions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "league": "SOCCER",
        "source": TEMPLATE_SOURCE,
        "source_note": (
            "Fill teams and games from verified soccer schedule sources, then ingest with "
            "kalshi-bot ingest-sports --league SOCCER --input-file <this-file>."
        ),
        "generated_at": utc_now().isoformat(),
        "recommended_competitions": [
            row["competition_code"] for row in competition_suggestions[:20]
        ],
        "alias_suggestions": alias_suggestions[:100],
        "teams": [],
        "games": [],
        "team_stats": [],
        "injuries": [],
        "odds": [],
        "expected_game_schema": {
            "game_key": "SOCCER:verified:<competition>:<event-id-or-home-away-date>",
            "scheduled_at": "2026-07-08T19:00:00+00:00",
            "home_team_key": "SOCCER:home-team",
            "away_team_key": "SOCCER:away-team",
            "status": "scheduled",
            "source": "verified_schedule",
            "competition": "<competition-code>",
            "source_url": "https://...",
        },
    }


def _next_action_for_reason(reason: str, *, league: str) -> str:
    if reason == "MULTI_LEG_MARKET_REQUIRES_COMPETITION_PROVENANCE":
        return (
            "Use competition/event provenance or component-leg evidence; do not force a "
            "single-game verified link."
        )
    if reason == "SOCCER_COMPETITION_PROVENANCE_MISSING":
        return (
            "Fetch or manually ingest verified soccer competition schedules, then rerun Phase 3AE."
        )
    if reason == "TEAM_ALIAS_OR_SCHEDULE_GAP":
        return "Review alias suggestions and ingest more verified schedule/team data."
    if reason == "READY_FOR_PHASE3AE_RETRY":
        return "Rerun phase3ae-verified-sports-connector."
    if reason == "VERIFIED_SCHEDULE_MISSING":
        return f"Ingest verified {league} schedules, then rerun Phase 3AE."
    return "Review this market before trusting sports model linkage."


def _recommended_next_action(
    reason_counts: Counter[str],
    alias_suggestions: list[dict[str, Any]],
) -> str:
    if reason_counts["MULTI_LEG_MARKET_REQUIRES_COMPETITION_PROVENANCE"]:
        return (
            "Add component-leg or competition-level provenance for multi-leg sports markets; "
            "keep them out of single-game verified upgrades."
        )
    if reason_counts["SOCCER_COMPETITION_PROVENANCE_MISSING"]:
        return (
            "Ingest broader soccer competitions using the Phase 3AJ template, then rerun "
            "Phase 3AE and market-coverage-doctor."
        )
    if alias_suggestions:
        return "Apply/review alias suggestions, then rerun Phase 3AE."
    return "Rerun Phase 3AE and market coverage."


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


def _link_provenance(link: SportsMarketLink) -> str:
    raw = decode_json(link.raw_json)
    source = str(raw.get("source") or "").lower()
    reason = str(link.link_reason or "").lower()
    game_key = str(link.game_key or "").lower()
    if source == VERIFIED_SOURCE or "verified schedule" in reason:
        return VERIFIED_SOURCE
    if "kalshi-event-derived" in game_key or source == DERIVED_SOURCE:
        return DERIVED_SOURCE
    if (
        "market-derived" in game_key
        or source == "market-derived-fallback"
        or "market-derived" in reason
    ):
        return PARTIAL_SOURCE
    return source or "other"


def _alias_in_text(text: str, alias: str) -> bool:
    normalized = _normalize(alias)
    if not normalized:
        return False
    if " " in normalized:
        return normalized in text
    return re.search(rf"(^|[\s,.-]){re.escape(normalized)}($|[\s,.-])", text) is not None


def _normalize(value: object) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _dedupe_keep_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _normalize(value)
        if key and key not in seen:
            deduped.append(value)
            seen.add(key)
    return deduped


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AJ Sports Alias + Competition Provenance Repair",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Apply aliases: {payload['apply_aliases']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Reason Breakdown", "", "| Reason | Count |", "| --- | ---: |"])
    for row in payload["reason_breakdown"]:
        lines.append(f"| {row['reason']} | {row['count']} |")
    lines.extend(
        [
            "",
            "## Alias Suggestions",
            "",
            "| League | Team | Alias | Applied | Examples |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["alias_suggestions"][:30]:
        lines.append(
            f"| {row['league']} | {_md(row['team_name'])} | `{_md(row['alias'])}` | "
            f"{row['applied']} | {_md(', '.join(row['examples']))} |"
        )
    if not payload["alias_suggestions"]:
        lines.append("| none |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Competition Suggestions",
            "",
            "| League | Competition | Reason | Examples |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in payload["competition_suggestions"][:30]:
        lines.append(
            f"| {row['league']} | `{row['competition_code']}` | {row['reason']} | "
            f"{_md(', '.join(row['examples'][:3]))} |"
        )
    if not payload["competition_suggestions"]:
        lines.append("| none |  |  |  |")
    lines.extend(
        [
            "",
            "## Top Unresolved Sports Partials",
            "",
            "| Ticker | League | Reason | Entities | Next action |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["rows"][:40]:
        lines.append(
            f"| `{row['ticker']}` | {row['league']} | {row['reason']} | "
            f"{_md(', '.join(row['entities'][:6]))} | {_md(row['next_action'])} |"
        )
    if not payload["rows"]:
        lines.append("| none |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
            "## Safety",
            "",
            "- Paper-only diagnostics and local sports metadata repair.",
            "- No demo orders.",
            "- No live orders.",
            "- Multi-leg markets remain unverified unless component provenance is supplied.",
            "",
        ]
    )
    return "\n".join(lines)


def _md(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
