import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Market, MarketLeg
from kalshi_predictor.market_legs import CATEGORY_SPORTS
from kalshi_predictor.sports.classifier import UNKNOWN, classify_sports_market
from kalshi_predictor.sports.features import calculate_sports_feature
from kalshi_predictor.sports.repository import (
    insert_sports_feature,
    insert_sports_market_link,
    latest_sports_feature,
    latest_sports_link,
    upsert_sports_game,
    upsert_sports_team,
)
from kalshi_predictor.utils.time import parse_datetime, utc_now

DERIVED_SOURCE = "KALSHI_EVENT_DERIVED"


@dataclass(frozen=True)
class SportsDerivedScheduleSummary:
    markets_scanned: int
    sports_markets_seen: int
    teams_created: int
    games_created: int
    links_created: int
    links_existing: int
    features_created: int
    features_existing: int
    skipped_no_market: int
    links_by_league: dict[str, int] = field(default_factory=dict)
    links_by_type: dict[str, int] = field(default_factory=dict)
    stopped_early: bool = False


def derive_sports_schedule_from_market_legs(
    session: Session,
    *,
    limit: int | None = None,
    build_features: bool = True,
    refresh_features: bool = False,
    settings: Settings | None = None,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    progress_every: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> SportsDerivedScheduleSummary:
    """Build local sports team/game/link rows from parsed Kalshi sports legs.

    This is not an external schedule feed. Rows are intentionally marked
    KALSHI_EVENT_DERIVED so downstream dashboards can distinguish usable local
    model inputs from verified team schedules.
    """
    resolved = settings or get_settings()
    ticker_to_legs = _sports_legs_by_ticker(session, limit=limit)
    teams_created = 0
    games_created = 0
    links_created = 0
    links_existing = 0
    features_created = 0
    features_existing = 0
    skipped_no_market = 0
    by_league: Counter[str] = Counter()
    by_type: Counter[str] = Counter()
    stopped_early = False

    total = len(ticker_to_legs)
    for index, (ticker, legs) in enumerate(ticker_to_legs.items(), start=1):
        if should_stop is not None and should_stop():
            stopped_early = True
            _emit_progress(
                progress_callback,
                progress_every=progress_every,
                processed=index - 1,
                total=total,
                ticker=ticker,
                status="STOPPED_EARLY",
                links_created=links_created,
                features_created=features_created,
            )
            break
        market = session.get(Market, ticker)
        if market is None:
            skipped_no_market += 1
            _emit_progress(
                progress_callback,
                progress_every=progress_every,
                processed=index,
                total=total,
                ticker=ticker,
                status="SKIPPED_NO_MARKET",
                links_created=links_created,
                features_created=features_created,
            )
            continue
        classification = classify_sports_market(market)
        league = _derived_league(market, legs, classification=classification)
        market_type = _derived_market_type(legs, classification=classification)
        slug = _slug(ticker)
        yes_team_key = f"{slug}-yes"
        no_team_key = f"{slug}-no"
        raw_market = decode_json(market.raw_json)
        provenance = {
            "source": DERIVED_SOURCE,
            "source_note": (
                "Derived from Kalshi market title/rules and parsed market legs; "
                "not an externally verified sports schedule."
            ),
            "market_ticker": ticker,
            "market_title": market.title,
            "series_ticker": market.series_ticker,
            "event_ticker": market.event_ticker,
            "leg_count": len(legs),
            "legs": [_leg_payload(leg) for leg in legs[:12]],
            "classification": classification,
            "raw_market": raw_market,
        }
        _, yes_created = upsert_sports_team(
            session,
            {
                "team_key": yes_team_key,
                "team_name": f"{_short_market_name(market)} YES side",
                "abbreviation": "YES",
                "raw_json": provenance,
            },
            league=league,
        )
        _, no_created = upsert_sports_team(
            session,
            {
                "team_key": no_team_key,
                "team_name": f"{_short_market_name(market)} NO side",
                "abbreviation": "NO",
                "raw_json": provenance,
            },
            league=league,
        )
        teams_created += int(yes_created) + int(no_created)

        game_key = f"{league}:kalshi-event-derived:{slug}"
        game, game_created = upsert_sports_game(
            session,
            {
                "game_key": game_key,
                "scheduled_at": _scheduled_at(market),
                "status": "kalshi_event_derived",
                "home_team_key": yes_team_key,
                "away_team_key": no_team_key,
                "venue": "Kalshi market-derived event",
                "neutral_site": True,
                "raw_json": provenance,
            },
            league=league,
        )
        games_created += int(game_created)

        link = latest_sports_link(session, ticker, league=league)
        has_current_link = link is not None and link.game_key == game_key
        if has_current_link:
            links_existing += 1
        else:
            _, was_created = insert_sports_market_link(
                session,
                ticker=ticker,
                league=league,
                game_key=game_key,
                market_type=market_type,
                link_confidence=Decimal("0.55"),
                link_reason=(
                    "Kalshi-event-derived sports link built from parsed market legs. "
                    "Use external schedule/team ingestion later to upgrade provenance."
                ),
                matched_terms=_matched_terms(league, market_type),
                raw_json=provenance,
            )
            if was_created:
                links_created += 1
            else:
                links_existing += 1

        if build_features:
            existing_feature = latest_sports_feature(
                session,
                ticker=ticker,
                league=league,
                game_key=game_key,
            )
            if existing_feature is not None and not refresh_features:
                features_existing += 1
            else:
                payload = calculate_sports_feature(session, game, settings=resolved)
                insert_sports_feature(
                    session,
                    league=league,
                    game_key=game_key,
                    ticker=ticker,
                    home_team_key=game.home_team_key,
                    away_team_key=game.away_team_key,
                    team_strength_edge=payload["team_strength_edge"],
                    injury_edge=payload["injury_edge"],
                    rest_edge=payload["rest_edge"],
                    travel_edge=payload["travel_edge"],
                    odds_edge=payload["odds_edge"],
                    weather_edge=payload["weather_edge"],
                    total_edge=payload["total_edge"],
                    home_win_probability=payload["home_win_probability"],
                    away_win_probability=payload["away_win_probability"],
                    projected_total=payload["projected_total"],
                    confidence_score=payload["confidence_score"],
                    raw_json={
                        **payload,
                        "source": DERIVED_SOURCE,
                        "market_ticker": ticker,
                        "sports_market_link": game_key,
                        "provenance": provenance,
                    },
                )
                features_created += 1
        by_league[league] += 1
        by_type[market_type] += 1
        _emit_progress(
            progress_callback,
            progress_every=progress_every,
            processed=index,
            total=total,
            ticker=ticker,
            status="PROGRESS",
            links_created=links_created,
            features_created=features_created,
        )

    return SportsDerivedScheduleSummary(
        markets_scanned=len(ticker_to_legs),
        sports_markets_seen=len(ticker_to_legs),
        teams_created=teams_created,
        games_created=games_created,
        links_created=links_created,
        links_existing=links_existing,
        features_created=features_created,
        features_existing=features_existing,
        skipped_no_market=skipped_no_market,
        links_by_league=dict(sorted(by_league.items())),
        links_by_type=dict(sorted(by_type.items())),
        stopped_early=stopped_early,
    )


def _sports_legs_by_ticker(
    session: Session,
    *,
    limit: int | None,
) -> dict[str, list[MarketLeg]]:
    rows = list(
        session.scalars(
            select(MarketLeg)
            .where(MarketLeg.category == CATEGORY_SPORTS)
            .order_by(MarketLeg.ticker, MarketLeg.leg_index)
        )
    )
    grouped: dict[str, list[MarketLeg]] = {}
    for row in rows:
        if row.ticker not in grouped and limit is not None and len(grouped) >= limit:
            continue
        grouped.setdefault(row.ticker, []).append(row)
    return grouped


def _derived_league(
    market: Market,
    legs: list[MarketLeg],
    *,
    classification: dict[str, Any],
) -> str:
    league = str(classification.get("league") or UNKNOWN).upper()
    if league != UNKNOWN:
        return league
    text = _combined_text(market, legs)
    if re.search(r"\b(runs?|strikeouts?|hits?|home runs?|innings?|mlb|baseball)\b", text, re.I):
        return "MLB"
    if re.search(r"\b(touchdowns?|yards?|field goals?|nfl|football|super bowl)\b", text, re.I):
        return "NFL"
    if re.search(r"\b(rebounds?|assists?|points?|nba|basketball)\b", text, re.I):
        return "NBA"
    if re.search(r"\b(goals?|shots?|soccer|fifa|uefa|both teams to score)\b", text, re.I):
        return "SOCCER"
    return "SPORTS"


def _derived_market_type(
    legs: list[MarketLeg],
    *,
    classification: dict[str, Any],
) -> str:
    values = [
        leg.market_type
        for leg in legs
        if leg.market_type and leg.market_type.upper() not in {"UNKNOWN", "MARKET"}
    ]
    if values:
        return Counter(values).most_common(1)[0][0]
    market_type = str(classification.get("market_type") or UNKNOWN)
    return "TEAM_PROP" if market_type == UNKNOWN else market_type


def _scheduled_at(market: Market) -> Any:
    candidate = (
        market.close_time
        or market.expected_expiration_time
        or market.expiration_time
        or market.settlement_ts
        or utc_now()
    )
    return parse_datetime(candidate) or utc_now()


def _short_market_name(market: Market) -> str:
    title = str(market.title or market.ticker)
    cleaned = re.sub(r"\s+", " ", title).strip()
    return cleaned[:80] or market.ticker


def _combined_text(market: Market, legs: list[MarketLeg]) -> str:
    return " ".join(
        str(part or "")
        for part in (
            market.ticker,
            market.title,
            market.subtitle,
            market.series_ticker,
            market.event_ticker,
            market.rules_primary,
            market.rules_secondary,
            " ".join(leg.raw_text for leg in legs),
        )
    )


def _leg_payload(leg: MarketLeg) -> dict[str, Any]:
    return {
        "leg_index": leg.leg_index,
        "side": leg.side,
        "market_type": leg.market_type,
        "entity_name": leg.entity_name,
        "operator": leg.operator,
        "threshold_value": leg.threshold_value,
        "unit": leg.unit,
        "confidence": leg.confidence,
        "raw_text": leg.raw_text,
    }


def _matched_terms(league: str, market_type: str) -> list[str]:
    return [league.lower(), market_type.lower(), "kalshi_event_derived"]


def _slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-") or "unknown"


def _emit_progress(
    progress_callback: Callable[[dict[str, object]], None] | None,
    *,
    progress_every: int,
    processed: int,
    total: int,
    ticker: str,
    status: str,
    links_created: int,
    features_created: int,
) -> None:
    if progress_callback is None:
        return
    cadence = max(progress_every, 0)
    if status == "PROGRESS" and cadence and processed % cadence != 0 and processed != total:
        return
    progress_callback(
        {
            "stage": "sports_derived_schedule",
            "processed": processed,
            "total": total,
            "ticker": ticker,
            "status": status,
            "links_created": links_created,
            "features_created": features_created,
        }
    )
