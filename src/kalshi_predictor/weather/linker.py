import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Market, MarketLeg, WeatherMarketLink
from kalshi_predictor.weather.repository import insert_weather_market_link

LOCATION_PATTERNS = {
    "kansas_city": r"\b(kansas city|kcmo|k\.?c\.?|mci)\b",
    "new_york": r"\b(new york|nyc|manhattan|jfk|laguardia|lga)\b",
    "los_angeles": r"\b(los angeles|l\.?a\.?|lax)\b",
    "chicago": r"\bchicago\b",
    "miami": r"\bmiami\b",
    "dallas": r"\b(dallas|dfw)\b",
    "seattle": r"\bseattle\b",
    "denver": r"\bdenver\b",
    "boston": r"\bboston\b",
    "philadelphia": r"\b(philadelphia|philly)\b",
    "atlanta": r"\batlanta\b",
    "houston": r"\bhouston\b",
    "phoenix": r"\bphoenix\b",
    "san_francisco": r"\b(san francisco|sfo)\b",
}

WEATHER_TICKER_PREFIXES = (
    "KXTEMP",
    "KXHIGH",
    "KXLOW",
    "KXRAIN",
    "KXWIND",
    "KXHURR",
    "KXSNOW",
    "KXFREEZE",
)


@dataclass(frozen=True)
class WeatherLinkDetection:
    location_key: str
    weather_metric: str
    target_operator: str
    target_value: Decimal | None
    target_time: object | None
    confidence: Decimal
    reason: str


@dataclass(frozen=True)
class WeatherLinkResult:
    markets_scanned: int
    links_created: int
    by_metric: dict[str, int]
    by_location_key: dict[str, int]
    unknown_location_count: int


def link_weather_markets(
    session: Session,
    *,
    limit: int | None = None,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    progress_every: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> WeatherLinkResult:
    session.flush()
    statement = _weather_candidate_statement()
    if limit is not None:
        statement = statement.limit(limit)
    markets = list(session.scalars(statement))
    if not markets:
        statement = select(Market).order_by(Market.ticker)
        if limit is not None:
            statement = statement.limit(limit)
        markets = list(session.scalars(statement))
    existing_link_tickers = set()
    if markets:
        existing_link_tickers = set(
            session.scalars(
                select(WeatherMarketLink.ticker).where(
                    WeatherMarketLink.ticker.in_([market.ticker for market in markets])
                )
            )
        )
    by_metric: Counter[str] = Counter()
    by_location: Counter[str] = Counter()
    unknown_location_count = 0
    links_created = 0

    for index, market in enumerate(markets, start=1):
        if should_stop is not None and should_stop():
            _emit_progress(
                progress_callback,
                progress_every=progress_every,
                processed=index - 1,
                total=len(markets),
                ticker=market.ticker,
                status="STOPPED_EARLY",
                created=links_created,
            )
            break
        if market.ticker in existing_link_tickers:
            _emit_progress(
                progress_callback,
                progress_every=progress_every,
                processed=index,
                total=len(markets),
                ticker=market.ticker,
                status="SKIPPED_EXISTING_LINK",
                created=links_created,
            )
            continue
        detection = detect_weather_market(market)
        if detection.confidence <= 0:
            _emit_progress(
                progress_callback,
                progress_every=progress_every,
                processed=index,
                total=len(markets),
                ticker=market.ticker,
                status="SKIPPED_NO_WEATHER_MATCH",
                created=links_created,
            )
            continue
        raw_market = decode_json(market.raw_json)
        insert_weather_market_link(
            session,
            ticker=market.ticker,
            location_key=detection.location_key,
            weather_metric=detection.weather_metric,
            target_operator=detection.target_operator,
            target_value=detection.target_value,
            target_time=detection.target_time,
            confidence=detection.confidence,
            reason=detection.reason,
            raw_json={
                "ticker": market.ticker,
                "title": market.title,
                "subtitle": market.subtitle,
                "series_ticker": market.series_ticker,
                "event_ticker": market.event_ticker,
                "raw_market": raw_market,
            },
        )
        links_created += 1
        existing_link_tickers.add(market.ticker)
        by_metric[detection.weather_metric] += 1
        by_location[detection.location_key] += 1
        if detection.location_key == "unknown":
            unknown_location_count += 1
        _emit_progress(
            progress_callback,
            progress_every=progress_every,
            processed=index,
            total=len(markets),
            ticker=market.ticker,
            status="PROGRESS",
            created=links_created,
        )

    return WeatherLinkResult(
        markets_scanned=len(markets),
        links_created=links_created,
        by_metric=dict(by_metric),
        by_location_key=dict(by_location),
        unknown_location_count=unknown_location_count,
    )


def detect_weather_market(market: Market) -> WeatherLinkDetection:
    text = _market_text(market)
    metric = _detect_metric(text)
    location = _detect_location(text)
    operator = _detect_operator(text)
    target_value = _detect_target_value(text)
    target_time = (
        market.close_time
        or market.expected_expiration_time
        or market.expiration_time
        or market.settlement_ts
    )
    confidence = _confidence(metric, location, target_value)
    reason = _reason(metric, location, operator, target_value)
    return WeatherLinkDetection(
        location_key=location,
        weather_metric=metric,
        target_operator=operator,
        target_value=target_value,
        target_time=target_time,
        confidence=confidence,
        reason=reason,
    )


def _detect_metric(text: str) -> str:
    normalized = text.lower()
    if re.search(r"\b(kxhurr|hurricane|tropical storm)\b", normalized):
        return "HURRICANE"
    if re.search(r"\b(kxsnow|snow(?:fall)?|blizzard)\b", normalized):
        return "SNOW"
    if re.search(r"\b(kxrain|rain(?:fall)?|precipitation|precip|showers?)\b", normalized):
        return "RAIN"
    if re.search(r"\b(kxwind|wind|gust)\b", normalized):
        return "WIND"
    if re.search(r"\b(kxfreeze|freeze|freezing|frost)\b", normalized):
        return "FREEZE"
    if re.search(
        r"\b(kxtemp|kxhigh|kxlow|temperature|temp|high temp|low temp|degrees?|hot|cold)\b",
        normalized,
    ):
        return "TEMPERATURE"
    if re.search(r"\b(high|low)\b", normalized) and re.search(
        r"\b(-?\d+(?:\.\d+)?)\s*(degrees?|deg|f)\b",
        normalized,
    ):
        return "TEMPERATURE"
    return "UNKNOWN"


def _detect_location(text: str) -> str:
    for location_key, pattern in LOCATION_PATTERNS.items():
        if re.search(pattern, text, flags=re.IGNORECASE):
            return location_key
    return "unknown"


def _detect_operator(text: str) -> str:
    normalized = text.lower()
    if re.search(r"\b(at or above|at least|no less than)\b", normalized):
        return "AT_OR_ABOVE"
    if re.search(r"\b(at or below|at most|no more than)\b", normalized):
        return "AT_OR_BELOW"
    if re.search(r"\b(above|greater than|over|exceed|exceeds)\b", normalized):
        return "ABOVE"
    if re.search(r"\b(below|less than|under)\b", normalized):
        return "BELOW"
    if re.search(r"\b(equal|equals|exactly)\b", normalized):
        return "EQUALS"
    return "UNKNOWN"


def _detect_target_value(text: str) -> Decimal | None:
    operator_matches = re.findall(
        (
            r"\b(?:at or above|at least|no less than|at or below|at most|no more than|"
            r"above|greater than|over|exceed|exceeds|below|less than|under|"
            r"equal|equals|exactly)\s+(-?\d+(?:\.\d+)?)\s*"
            r"(?:°|degrees?|deg|f|mph|inches?|inch|\"|%)?"
        ),
        text,
        flags=re.IGNORECASE,
    )
    if operator_matches:
        return Decimal(operator_matches[0])
    unit_matches = re.findall(
        r"(-?\d+(?:\.\d+)?)\s*(?:°|degrees?|deg|f|mph|inches?|inch|\"|%)",
        text,
        flags=re.IGNORECASE,
    )
    if unit_matches:
        return Decimal(unit_matches[0])
    matches = re.findall(
        r"(-?\d+(?:\.\d+)?)\s*(?:degrees?|deg|f|mph|inches?|inch|\"|%)?",
        text,
        flags=re.IGNORECASE,
    )
    if not matches:
        return None
    return Decimal(matches[-1])


def _weather_candidate_statement():
    ticker_family_filters = [
        Market.ticker.like(f"{prefix}%") for prefix in WEATHER_TICKER_PREFIXES
    ] + [
        Market.series_ticker.like(f"{prefix}%") for prefix in WEATHER_TICKER_PREFIXES
    ]
    return (
        select(Market)
        .outerjoin(MarketLeg, MarketLeg.ticker == Market.ticker)
        .where(
            or_(
                MarketLeg.category == "weather",
                (
                    Market.status.in_(("active", "open"))
                    & or_(*ticker_family_filters)
                ),
            )
        )
        .distinct()
        .order_by(Market.ticker)
    )


def _confidence(metric: str, location_key: str, target_value: Decimal | None) -> Decimal:
    if metric == "UNKNOWN":
        return Decimal("0.0")
    if location_key != "unknown" and target_value is not None:
        return Decimal("1.0")
    if location_key != "unknown":
        return Decimal("0.8")
    return Decimal("0.6")


def _reason(
    metric: str,
    location_key: str,
    operator: str,
    target_value: Decimal | None,
) -> str:
    if metric == "UNKNOWN":
        return "No weather keyword match."
    parts = [f"Metric {metric} detected"]
    if location_key != "unknown":
        parts.append(f"location {location_key} detected")
    else:
        parts.append("location unknown")
    if operator != "UNKNOWN":
        parts.append(f"operator {operator} detected")
    if target_value is not None:
        parts.append(f"threshold {target_value} detected")
    return "; ".join(parts) + "."


def _market_text(market: Market) -> str:
    raw = decode_json(market.raw_json)
    parts = [
        market.ticker,
        market.title,
        market.subtitle,
        market.series_ticker,
        market.event_ticker,
        market.rules_primary,
        market.rules_secondary,
        raw.get("rules_primary"),
        raw.get("rules_secondary"),
        raw.get("rules"),
        raw.get("series_title"),
        raw.get("event_title"),
        raw.get("category"),
        raw.get("tags"),
        _raw_text(raw),
    ]
    return " ".join(str(part or "") for part in parts)


def _raw_text(raw: object) -> str:
    try:
        return json.dumps(raw, sort_keys=True)
    except TypeError:
        return str(raw)


def _emit_progress(
    progress_callback: Callable[[dict[str, object]], None] | None,
    *,
    progress_every: int,
    processed: int,
    total: int,
    ticker: str,
    status: str,
    created: int,
) -> None:
    if progress_callback is None:
        return
    cadence = max(progress_every, 0)
    if status == "PROGRESS" and cadence and processed % cadence != 0 and processed != total:
        return
    progress_callback(
        {
            "stage": "weather_link",
            "processed": processed,
            "total": total,
            "ticker": ticker,
            "status": status,
            "created": created,
        }
    )
