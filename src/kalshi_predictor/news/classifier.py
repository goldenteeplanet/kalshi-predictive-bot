import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.utils.time import parse_datetime, utc_now

NEWS_CATEGORIES = (
    "crypto",
    "weather",
    "economic",
    "sports",
    "politics",
    "company",
    "geopolitical",
    "general",
)

CATEGORY_KEYWORDS = {
    "crypto": ("btc", "bitcoin", "eth", "ethereum", "crypto", "coinbase", "blockchain"),
    "weather": (
        "weather",
        "hurricane",
        "storm",
        "rain",
        "snow",
        "temperature",
        "tornado",
        "flood",
        "noaa",
        "nhc",
    ),
    "economic": (
        "fed",
        "fomc",
        "powell",
        "interest rate",
        "rates",
        "cpi",
        "inflation",
        "jobs",
        "payrolls",
        "unemployment",
        "gdp",
        "bls",
        "bea",
    ),
    "sports": ("mlb", "nba", "nfl", "nhl", "injury", "team", "game", "playoff"),
    "politics": ("election", "senate", "house", "president", "congress", "vote"),
    "company": ("earnings", "shares", "stock", "company", "sec", "merger", "guidance"),
    "geopolitical": ("war", "ceasefire", "tariff", "sanction", "oil", "gas", "energy"),
}

ENTITY_PATTERNS = {
    "BTC": (r"\bbtc\b", r"\bbitcoin\b"),
    "ETH": (r"\beth\b", r"\bethereum\b"),
    "Fed": (r"\bfed\b", r"\bfederal reserve\b", r"\bfomc\b", r"\bpowell\b"),
    "Interest Rates": (r"\binterest rates?\b", r"\brate decision\b", r"\brates?\b"),
    "CPI": (r"\bcpi\b", r"\binflation\b"),
    "Jobs": (r"\bjobs?\b", r"\bunemployment\b", r"\bpayrolls?\b"),
    "Hurricane": (r"\bhurricane\b", r"\bnhc\b"),
    "Storm": (r"\bstorm\b", r"\brain\b", r"\bsnow\b", r"\btemperature\b"),
    "Oil": (r"\boil\b", r"\bgas\b", r"\benergy\b"),
    "MLB": (r"\bmlb\b", r"\bbaseball\b"),
    "NBA": (r"\bnba\b", r"\bbasketball\b"),
    "NFL": (r"\bnfl\b", r"\bfootball\b"),
    "NHL": (r"\bnhl\b", r"\bhockey\b"),
}

POSITIVE_WORDS = {
    "advance",
    "approved",
    "beat",
    "beats",
    "bullish",
    "gain",
    "gains",
    "growth",
    "higher",
    "improve",
    "positive",
    "rally",
    "record",
    "rise",
    "rises",
    "strong",
    "surge",
    "surges",
    "up",
    "win",
}

NEGATIVE_WORDS = {
    "bearish",
    "decline",
    "declines",
    "delay",
    "down",
    "drop",
    "emergency",
    "fall",
    "falls",
    "lawsuit",
    "loss",
    "miss",
    "misses",
    "outage",
    "risk",
    "shutdown",
    "strike",
    "weak",
    "warning",
}

IMPORTANCE_KEYWORDS = {
    "breaking": Decimal("0.30"),
    "official": Decimal("0.20"),
    "fed": Decimal("0.20"),
    "fomc": Decimal("0.20"),
    "cpi": Decimal("0.18"),
    "jobs": Decimal("0.15"),
    "payrolls": Decimal("0.15"),
    "hurricane": Decimal("0.20"),
    "emergency": Decimal("0.20"),
    "rate decision": Decimal("0.20"),
    "court": Decimal("0.12"),
    "ruling": Decimal("0.12"),
    "shutdown": Decimal("0.15"),
    "strike": Decimal("0.15"),
    "outage": Decimal("0.15"),
}

LOW_IMPORTANCE_KEYWORDS = ("opinion", "preview", "rumor", "recap", "analysis")


def classify_news_item(payload: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Classify a raw news payload using deterministic local keyword rules."""

    observed_now = now or utc_now()
    text = _payload_text(payload)
    category = _provided_category(payload) or _detect_category(text)
    published_at = parse_datetime(payload.get("published_at"))
    sentiment = score_sentiment(text)
    importance = score_importance(text, source=str(payload.get("source") or ""))
    freshness = score_freshness(published_at, now=observed_now)
    entities = extract_entities(text)
    return {
        "category": category,
        "entities": entities,
        "sentiment_score": sentiment,
        "importance_score": importance,
        "freshness_score": freshness,
        "published_at": published_at,
    }


def extract_entities(text: str) -> list[str]:
    normalized = text.lower()
    entities: set[str] = set()
    for label, patterns in ENTITY_PATTERNS.items():
        if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in patterns):
            entities.add(label)

    for token in re.findall(r"\b[A-Z]{2,5}\b", text):
        if token in {"THE", "AND", "FOR", "FROM", "WITH", "THIS", "THAT"}:
            continue
        if token in {"BTC", "ETH", "CPI", "MLB", "NBA", "NFL", "NHL"}:
            entities.add(token)
        elif re.fullmatch(r"[A-Z]{2,5}", token):
            entities.add(token)
    return sorted(entities)


def score_sentiment(text: str) -> Decimal:
    tokens = [token.lower() for token in re.findall(r"[a-zA-Z]+", text)]
    if not tokens:
        return Decimal("0")
    positive = sum(1 for token in tokens if token in POSITIVE_WORDS)
    negative = sum(1 for token in tokens if token in NEGATIVE_WORDS)
    total = positive + negative
    if total == 0:
        return Decimal("0")
    score = Decimal(positive - negative) / Decimal(max(total, 3))
    return _clamp(score, Decimal("-1"), Decimal("1")).quantize(Decimal("0.0001"))


def score_importance(text: str, *, source: str = "") -> Decimal:
    normalized = text.lower()
    score = Decimal("0.30")
    if source.lower() in {"fed", "bls", "bea", "noaa", "nhc"}:
        score += Decimal("0.10")
    for keyword, boost in IMPORTANCE_KEYWORDS.items():
        if keyword in normalized:
            score += boost
    if any(keyword in normalized for keyword in LOW_IMPORTANCE_KEYWORDS):
        score -= Decimal("0.15")
    return _clamp(score, Decimal("0"), Decimal("1")).quantize(Decimal("0.0001"))


def score_freshness(published_at: datetime | None, *, now: datetime | None = None) -> Decimal:
    if published_at is None:
        return Decimal("0.50")
    observed_now = now or utc_now()
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=observed_now.tzinfo)
    age_hours = Decimal(str(max((observed_now - published_at).total_seconds(), 0))) / Decimal(
        "3600"
    )
    freshness = Decimal("1") / (Decimal("1") + (age_hours / Decimal("12")))
    return _clamp(freshness, Decimal("0"), Decimal("1")).quantize(Decimal("0.0001"))


def _payload_text(payload: dict[str, Any]) -> str:
    return " ".join(
        str(payload.get(key) or "")
        for key in ("title", "summary", "body", "category", "source")
    )


def _provided_category(payload: dict[str, Any]) -> str | None:
    category = str(payload.get("category") or "").strip().lower()
    return category if category in NEWS_CATEGORIES else None


def _detect_category(text: str) -> str:
    normalized = text.lower()
    scores: dict[str, int] = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        scores[category] = sum(1 for keyword in keywords if keyword in normalized)
    best_category = max(scores, key=lambda category: scores[category])
    return best_category if scores[best_category] > 0 else "general"


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    if value < low:
        return low
    if value > high:
        return high
    return value
