import re
from collections.abc import Mapping, Sequence
from typing import Any

from kalshi_predictor.data.repositories import decode_json

SUPPORTED_LEAGUES = ("MLB", "NBA", "NFL", "NHL", "WNBA", "SOCCER", "SPORTS")

MONEYLINE = "MONEYLINE"
SPREAD = "SPREAD"
TOTAL = "TOTAL"
PLAYER_PROP = "PLAYER_PROP"
TEAM_PROP = "TEAM_PROP"
SERIES = "SERIES"
CHAMPIONSHIP = "CHAMPIONSHIP"
UNKNOWN = "UNKNOWN"

MARKET_TYPES = (
    MONEYLINE,
    SPREAD,
    TOTAL,
    PLAYER_PROP,
    TEAM_PROP,
    SERIES,
    CHAMPIONSHIP,
    UNKNOWN,
)

LEAGUE_KEYWORDS = {
    "MLB": (
        "mlb",
        "baseball",
        "world series",
        "yankees",
        "dodgers",
        "mets",
        "cubs",
        "red sox",
        "cardinals",
    ),
    "NBA": (
        "nba",
        "basketball",
        "finals",
        "lakers",
        "celtics",
        "knicks",
        "warriors",
        "mavericks",
        "nuggets",
    ),
    "NFL": (
        "nfl",
        "football",
        "super bowl",
        "chiefs",
        "cowboys",
        "eagles",
        "packers",
        "49ers",
        "ravens",
    ),
    "NHL": (
        "nhl",
        "hockey",
        "stanley cup",
        "rangers",
        "bruins",
        "maple leafs",
        "oilers",
        "avalanche",
        "panthers",
    ),
    "WNBA": (
        "wnba",
        "liberty",
        "aces",
        "fever",
        "sky",
        "lynx",
        "mercury",
        "angel reese",
        "caitlin clark",
        "a'ja wilson",
        "sabrina ionescu",
    ),
    "SOCCER": (
        "soccer",
        "fifa",
        "uefa",
        "epl",
        "world cup",
        "premier league",
        "champions league",
        "both teams to score",
        "goals scored",
        "wins by more than",
        "vinicius junior",
        "jonathan david",
        "son heung-min",
        "brahim diaz",
        "raul jimenez",
        "breel embolo",
        "achraf hakimi",
        "ermedin demirovic",
        "lionel messi",
        "cristiano ronaldo",
        "morocco",
        "brazil",
        "switzerland",
        "bosnia and herzegovina",
        "korea republic",
        "ivory coast",
        "germany",
        "japan",
        "netherlands",
        "usa",
        "senegal",
        "spain",
        "argentina",
        "england",
        "canada",
        "mexico",
        "colombia",
    ),
    "SPORTS": (
        "sports",
        "player prop",
        "multi game",
        "multigame",
    ),
}


def classify_sports_market(
    market: Any,
    *,
    teams: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    text = sports_text_from_market(market)
    league = detect_league(text, teams=teams)
    market_type = detect_market_type(text)
    return {
        "league": league,
        "market_type": market_type,
        "is_sports": league != UNKNOWN or market_type in MARKET_TYPES[:-1],
        "matched_terms": matched_sports_terms(text, league=league),
        "text": text,
    }


def detect_league(
    text: str,
    *,
    teams: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    normalized = text.lower()
    scores: dict[str, int] = {}
    for league, terms in LEAGUE_KEYWORDS.items():
        scores[league] = sum(1 for term in terms if _contains_term(normalized, term))
    for team in teams or ():
        league = str(team.get("league") or "").upper()
        if league not in SUPPORTED_LEAGUES:
            continue
        aliases = (
            team.get("team_key"),
            team.get("team_name"),
            team.get("abbreviation"),
            team.get("city"),
            *(team.get("aliases") if isinstance(team.get("aliases"), list) else ()),
        )
        if any(alias and _contains_term(normalized, str(alias).lower()) for alias in aliases):
            scores[league] = scores.get(league, 0) + 2
    best_league, best_score = max(scores.items(), key=lambda item: item[1])
    return best_league if best_score > 0 else UNKNOWN


def detect_market_type(text: str) -> str:
    normalized = text.lower()
    if re.search(r"\b(mvp|player|points|rebounds?|assists?|yards?|goals?|hits?)\b", normalized):
        return PLAYER_PROP
    if re.search(r"\b(team total|runs by|points by|goals by|team to score)\b", normalized):
        return TEAM_PROP
    if re.search(r"\b(championship|champion|win the title|super bowl|world series)\b", normalized):
        return CHAMPIONSHIP
    if re.search(r"\b(series|best of|advance|round)\b", normalized):
        return SERIES
    if re.search(r"\b(total|over|under|combined|more than|fewer than)\b", normalized):
        return TOTAL
    if re.search(r"\b(spread|cover|handicap|win by|margin)\b", normalized):
        return SPREAD
    if re.search(r"\b(win|winner|moneyline|beat|defeat)\b", normalized):
        return MONEYLINE
    return UNKNOWN


def sports_text_from_market(market: Any) -> str:
    if isinstance(market, Mapping):
        raw = market
        raw_json = raw.get("raw_json")
    else:
        raw_json = getattr(market, "raw_json", None)
        raw = {
            "ticker": getattr(market, "ticker", None),
            "title": getattr(market, "title", None),
            "subtitle": getattr(market, "subtitle", None),
            "series_ticker": getattr(market, "series_ticker", None),
            "event_ticker": getattr(market, "event_ticker", None),
            "market_type": getattr(market, "market_type", None),
            "rules_primary": getattr(market, "rules_primary", None),
            "rules_secondary": getattr(market, "rules_secondary", None),
        }
    decoded = decode_json(raw_json) if isinstance(raw_json, str) else {}
    parts = [
        raw.get("ticker"),
        raw.get("title"),
        raw.get("subtitle"),
        raw.get("series_ticker"),
        raw.get("event_ticker"),
        raw.get("market_type"),
        raw.get("rules_primary"),
        raw.get("rules_secondary"),
        decoded.get("title"),
        decoded.get("subtitle"),
        decoded.get("rules"),
        decoded.get("rules_primary"),
        decoded.get("rules_secondary"),
    ]
    return " ".join(str(part or "") for part in parts)


def matched_sports_terms(text: str, *, league: str) -> list[str]:
    normalized = text.lower()
    terms: list[str] = []
    for item in LEAGUE_KEYWORDS.get(league, ()):
        if _contains_term(normalized, item):
            terms.append(item)
    return sorted(set(terms))


def _contains_term(text: str, term: str) -> bool:
    if " " in term:
        return term in text
    return re.search(rf"\b{re.escape(term)}\b", text) is not None
