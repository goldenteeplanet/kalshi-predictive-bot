import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import and_, case, delete, desc, func, literal, select, union_all
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import current_market_predicate
from kalshi_predictor.crypto.assets import (
    DEFAULT_CRYPTO_SYMBOLS,
    symbol_for_target_price,
    symbol_from_alias_text,
)
from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import (
    CryptoFeature,
    CryptoMarketLink,
    EconomicFeature,
    EconomicMarketLink,
    Market,
    MarketLeg,
    MarketSnapshot,
    NewsFeature,
    NewsMarketLink,
    SportsFeature,
    SportsGame,
    SportsMarketLink,
    WeatherFeature,
    WeatherMarketLink,
)
from kalshi_predictor.utils.time import utc_now

CATEGORY_CRYPTO = "crypto"
CATEGORY_WEATHER = "weather"
CATEGORY_ECONOMIC = "economic"
CATEGORY_SPORTS = "sports"
CATEGORY_CROSS_CATEGORY = "cross_category"
CATEGORY_NEWS = "news"
CATEGORY_GENERAL = "general"
CATEGORY_UNKNOWN = "unknown"

LINKED_CATEGORIES = (
    CATEGORY_CRYPTO,
    CATEGORY_WEATHER,
    CATEGORY_ECONOMIC,
    CATEGORY_SPORTS,
    CATEGORY_NEWS,
)
DISPLAY_CATEGORIES = LINKED_CATEGORIES + (
    CATEGORY_CROSS_CATEGORY,
    CATEGORY_GENERAL,
    CATEGORY_UNKNOWN,
)
LINK_TABLE_BY_CATEGORY: dict[str, Any] = {
    CATEGORY_CRYPTO: CryptoMarketLink,
    CATEGORY_WEATHER: WeatherMarketLink,
    CATEGORY_ECONOMIC: EconomicMarketLink,
    CATEGORY_SPORTS: SportsMarketLink,
    CATEGORY_NEWS: NewsMarketLink,
}
ESPORTS_MARKET_FAMILY_PREFIXES = (
    ("KXCODGAME", "KXCOD esports market family matched before general fallback"),
    ("KXCODMAP", "KXCOD esports market family matched before general fallback"),
    ("KXCS2GAME", "KXCS2 esports market family matched before general fallback"),
    ("KXCS2MAP", "KXCS2 esports market family matched before general fallback"),
    ("KXCS2TOTALMAPS", "KXCS2 esports market family matched before general fallback"),
    ("KXVALORANTGAME", "KXVALORANT esports market family matched before general fallback"),
    ("KXVALORANTMAP", "KXVALORANT esports market family matched before general fallback"),
)
CRICKET_MARKET_FAMILY_PREFIXES = (
    ("KXT20MATCH", "Kalshi cricket market family matched before general fallback"),
    ("KXWT20MATCH", "Kalshi cricket market family matched before general fallback"),
    ("KXWODIMATCH", "Kalshi cricket market family matched before general fallback"),
)


@dataclass(frozen=True)
class ParsedMarketLeg:
    leg_index: int
    side: str
    category: str
    market_type: str
    entity_name: str | None
    operator: str
    threshold_value: str | None
    unit: str | None
    confidence: str
    raw_text: str
    reason: str
    raw_json: dict[str, Any]


@dataclass(frozen=True)
class MarketLegParseResult:
    markets_scanned: int
    markets_with_legs: int
    legs_inserted: int
    markets_skipped_existing: int
    existing_markets_with_legs: int = 0


def parse_market_legs(market: Market) -> list[ParsedMarketLeg]:
    """Parse visible contract legs from one Kalshi market without making trade decisions."""
    context = _market_context(market)
    raw_legs = _candidate_leg_texts(market)
    parsed: list[ParsedMarketLeg] = []
    for index, raw_text in enumerate(raw_legs):
        side, body = _extract_side(raw_text)
        category, category_reason = _classify_category(body, context)
        market_type, type_reason = _classify_market_type(
            body,
            context,
            category,
            market.market_type,
        )
        operator = _extract_operator(body)
        threshold, unit = _extract_threshold(body)
        entity = _extract_entity(body, category, market_type, context)
        confidence = _confidence(category, market_type, entity, threshold)
        reason = "; ".join(
            part
            for part in (
                category_reason,
                type_reason,
                f"operator {operator}" if operator != "UNKNOWN" else "",
                "threshold detected" if threshold is not None else "",
            )
            if part
        )
        parsed.append(
            ParsedMarketLeg(
                leg_index=index,
                side=side,
                category=category,
                market_type=market_type,
                entity_name=entity,
                operator=operator,
                threshold_value=threshold,
                unit=unit,
                confidence=confidence,
                raw_text=raw_text,
                reason=reason or "No specialized parser evidence matched.",
                raw_json={
                    "ticker": market.ticker,
                    "title": market.title,
                    "subtitle": market.subtitle,
                    "series_ticker": market.series_ticker,
                    "event_ticker": market.event_ticker,
                    "source": "market_leg_parser_v1",
                },
            )
        )
    return parsed


def parse_and_store_market_legs(
    session: Session,
    *,
    limit: int | None = None,
    refresh: bool = False,
) -> MarketLegParseResult:
    session.flush()
    statement = select(Market).order_by(desc(Market.last_seen_at), Market.ticker)
    if limit is not None and limit > 0:
        statement = statement.limit(limit)
    markets = list(session.scalars(statement))
    market_tickers = [market.ticker for market in markets]
    existing_tickers: set[str] = set()
    if refresh:
        delete_statement = delete(MarketLeg)
        if limit is not None and limit > 0:
            delete_statement = delete_statement.where(MarketLeg.ticker.in_(market_tickers))
        session.execute(delete_statement)
    else:
        existing_tickers = set(session.scalars(select(MarketLeg.ticker).distinct()))
    parsed_at = utc_now()
    legs_inserted = 0
    markets_with_legs = 0
    skipped_existing = 0
    existing_markets_with_legs = 0

    for market in markets:
        if not refresh and market.ticker in existing_tickers:
            skipped_existing += 1
            existing_markets_with_legs += 1
            continue
        parsed_legs = parse_market_legs(market)
        if parsed_legs:
            markets_with_legs += 1
        for parsed in parsed_legs:
            session.add(
                MarketLeg(
                    ticker=market.ticker,
                    leg_index=parsed.leg_index,
                    parsed_at=parsed_at,
                    side=parsed.side,
                    category=parsed.category,
                    market_type=parsed.market_type,
                    entity_name=parsed.entity_name,
                    operator=parsed.operator,
                    threshold_value=parsed.threshold_value,
                    unit=parsed.unit,
                    confidence=parsed.confidence,
                    raw_text=parsed.raw_text,
                    reason=parsed.reason,
                    raw_json=encode_json(parsed.raw_json),
                )
            )
            legs_inserted += 1
    session.flush()
    return MarketLegParseResult(
        markets_scanned=len(markets),
        markets_with_legs=markets_with_legs,
        legs_inserted=legs_inserted,
        markets_skipped_existing=skipped_existing,
        existing_markets_with_legs=existing_markets_with_legs,
    )


def link_coverage_dashboard(session: Session) -> dict[str, Any]:
    link_counts = _link_ticker_counts(session)
    sports_reconciliation = _sports_link_reconciliation(session)
    unsupported_composite_counts = _unsupported_composite_counts_by_category(session)
    coverage_counts = _market_leg_coverage_counts(session)
    category_stats = {
        category: {
            "parsed_legs": row["parsed_legs"],
            "parsed_markets": row["parsed_markets"],
            "current_parsed_legs": row["current_parsed_legs"],
            "current_parsed_markets": row["current_parsed_markets"],
            "current_linked_markets": row["current_linked_markets"],
        }
        for category, row in coverage_counts.items()
    }
    linked_market_counts = {
        category: row["linked_markets"] for category, row in coverage_counts.items()
    }
    linked_leg_counts = {category: row["linked_legs"] for category, row in coverage_counts.items()}
    unlinked_leg_counts = {
        category: row["unlinked_legs"] for category, row in coverage_counts.items()
    }
    table_counts = _table_counts(session)
    category_rows = [
        _category_row(
            category,
            category_stats=category_stats,
            linked_market_counts=linked_market_counts,
            table_counts=table_counts,
            sports_reconciliation=sports_reconciliation,
            unsupported_composite_counts=unsupported_composite_counts,
        )
        for category in DISPLAY_CATEGORIES
    ]
    linked_leg_count = sum(linked_leg_counts.values())
    partial_leg_count = sports_reconciliation["unresolved_partial_legs"]
    linked_unsupported_leg_count = sum(
        int(unsupported_composite_counts.get(category, {}).get("legs") or 0)
        for category in LINKED_CATEGORIES
    )
    unlinked_leg_count = max(
        sum(unlinked_leg_counts.values()) - linked_unsupported_leg_count,
        0,
    )
    unsupported_composite_market_count = sum(
        row["markets"] for row in unsupported_composite_counts.values()
    )
    current_parsed_market_count = sum(
        int(row.get("current_parsed_markets") or 0) for row in category_rows
    )
    current_unlinked_market_count = sum(
        int(row.get("current_unlinked_markets") or 0) for row in category_rows
    )
    bottleneck = _top_bottleneck(category_rows)
    return {
        "generated_at": utc_now().isoformat(),
        "mode": "PAPER ONLY",
        "refresh_mode": "SUMMARY_ONLY_INDEXED",
        "refresh_note": (
            "Summary counts use indexed aggregate queries; row detail examples are bounded."
        ),
        "summary_cards": [
            {
                "label": "Markets",
                "value": _count(session, Market),
                "definition": "Stored Kalshi market rows in the local catalog.",
            },
            {
                "label": "Parsed Legs",
                "value": sum(row["parsed_legs"] for row in category_stats.values()),
                "definition": "Individual parsed YES/NO contract legs across stored markets.",
            },
            {
                "label": "Linked Legs",
                "value": linked_leg_count,
                "definition": "Parsed legs whose ticker has a specialized link row.",
            },
            {
                "label": "Partial Legs",
                "value": partial_leg_count,
                "definition": (
                    "Parsed legs on tickers that only have unresolved market-derived sports "
                    "provenance."
                ),
            },
            {
                "label": "Unlinked Legs",
                "value": unlinked_leg_count,
                "definition": (
                    "Parsed linkable legs with no specialized link row, excluding unsupported "
                    "sports multi-leg composites."
                ),
            },
            {
                "label": "Unsupported Composites",
                "value": unsupported_composite_market_count,
                "definition": (
                    "KXMVE multi-leg composite markets that require composite support, "
                    "not single-market link rows."
                ),
            },
            {
                "label": "Current Parsed Markets",
                "value": current_parsed_market_count,
                "definition": (
                    "Parsed markets not explicitly inactive and not past a known close or "
                    "expiration time."
                ),
            },
            {
                "label": "Current Unlinked Markets",
                "value": current_unlinked_market_count,
                "definition": (
                    "Current parsed linkable markets without a specialized link row, "
                    "excluding unsupported composites."
                ),
            },
        ],
        "table_counts": table_counts,
        "category_rows": category_rows,
        "bottleneck": bottleneck,
        "link_counts": _link_counts(link_counts, sports_reconciliation),
        "count_definitions": _count_definitions(),
        "reconciliation": {"sports": sports_reconciliation},
        "unlinked_examples": _example_rows(session, link_sets={}, mode="unlinked"),
        "partial_examples": _example_rows(
            session,
            link_sets={
                CATEGORY_SPORTS: set(sports_reconciliation["unresolved_partial_tickers"])
            },
            mode="partial",
        ),
        "next_commands": _next_commands(category_rows, bottleneck),
    }


def generate_link_coverage_report(
    session: Session,
    *,
    output_path: Path = Path("reports/link_coverage_report.md"),
    coverage: dict[str, Any] | None = None,
) -> Path:
    coverage = coverage or link_coverage_dashboard(session)
    lines = [
        "# Market Link Coverage Report",
        "",
        f"- Generated at: {coverage['generated_at']}",
        f"- Mode: {coverage['mode']}",
        "- Safety: read-only diagnostics; no demo or live orders are submitted.",
        "",
        "## Summary",
        "",
    ]
    for card in coverage["summary_cards"]:
        lines.append(f"- {card['label']}: {card['value']}")
    lines.extend(
        [
            "",
            (
                "## Coverage Status"
                if coverage["bottleneck"]["status"] == "CONNECTED"
                else "## Main Bottleneck"
            ),
            "",
            f"- Status: {coverage['bottleneck']['status']}",
            f"- Detail: {coverage['bottleneck']['message']}",
            f"- Next action: {coverage['bottleneck']['next_action']}",
            "",
            "## Category Coverage",
            "",
            "| Category | Current markets | Current linked | Current unlinked | "
            "Current coverage | All parsed markets | All linked markets | Historical unlinked | "
            "Derived markets | Verified markets | Partial markets | Unsupported composites | "
            "Current status | Next action |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | "
            "---: | ---: | --- | --- |",
        ]
    )
    for row in coverage["category_rows"]:
        lines.append(
            "| {category} | {current_parsed_markets} | {current_linked_markets} | "
            "{current_unlinked_markets} | {current_coverage_percent} | {parsed_markets} | "
            "{linked_markets} | {historical_unlinked_markets} | "
            "{derived_usable_markets} | {verified_schedule_markets} | {partial_markets} | "
            "{current_unsupported_multileg_markets} | {current_status_label} | "
            "{current_next_action} |".format(**row)
        )
    lines.extend(["", "## Count Definitions", ""])
    for item in coverage["count_definitions"]:
        lines.append(f"- {item['label']}: {item['definition']}")
    lines.extend(["", "## Link Tables", ""])
    for row in coverage["link_counts"]:
        lines.append(f"- {row['label']}: {row['value']}")
    lines.extend(["", "## Data Tables", ""])
    for key, value in coverage["table_counts"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Unlinked Examples", ""])
    if coverage["unlinked_examples"]:
        for example in coverage["unlinked_examples"]:
            lines.append(
                f"- [{example.get('scope', 'UNKNOWN')}] {example['category']} "
                f"{example['ticker']}: {example['title']} "
                f"({example['raw_text']}) -> {example['next_action']}"
            )
    else:
        lines.append("- None.")
    lines.extend(["", "## Partial Link Examples", ""])
    if coverage["partial_examples"]:
        for example in coverage["partial_examples"]:
            lines.append(
                f"- [{example.get('scope', 'UNKNOWN')}] {example['category']} "
                f"{example['ticker']}: {example['title']} "
                f"({example['raw_text']}) -> {example['next_action']}"
            )
    else:
        lines.append("- None.")
    lines.extend(["", "## Recommended Next Commands", ""])
    if coverage["next_commands"]:
        lines.append("```bash")
        lines.extend(coverage["next_commands"])
        lines.append("```")
    else:
        lines.append("- No coverage command is required right now.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def _candidate_leg_texts(market: Market) -> list[str]:
    title = _clean(market.title)
    subtitle = _clean(market.subtitle)
    primary = title or subtitle or _clean(market.rules_primary) or market.ticker
    parts = [part for part in re.split(r"\s*,\s*(?=(?:yes|no)\b)", primary, flags=re.I) if part]
    if not parts:
        parts = [primary]
    return [_clean(part) for part in parts if _clean(part)]


def _extract_side(text: str) -> tuple[str, str]:
    match = re.match(r"^\s*(yes|no)\b[:\s-]*(.*)$", text, flags=re.I)
    if match:
        return match.group(1).upper(), _clean(match.group(2)) or text
    return "UNKNOWN", text


def _classify_category(leg_text: str, context: str) -> tuple[str, str]:
    text = f"{leg_text} {context}"
    normalized = text.lower()
    family_category = _known_market_family_category(text)
    if family_category and family_category[0] == CATEGORY_CROSS_CATEGORY:
        return family_category
    target_price_symbol = _crypto_target_price_symbol(leg_text, context)
    if target_price_symbol is not None:
        return (
            CATEGORY_CRYPTO,
            f"supported {target_price_symbol} dollar target-price leg matched",
        )
    if _looks_like_sports_leg_body(leg_text):
        return (
            CATEGORY_SPORTS,
            "sports metric or player prop matched on leg text",
        )
    if family_category:
        return family_category
    if symbol_from_alias_text(text) or re.search(
        r"\b(crypto|cryptocurrency|cryptocurrencies|digital asset)\b",
        normalized,
    ):
        return CATEGORY_CRYPTO, "crypto symbol or keyword matched"
    if re.search(r"\btarget price\b", normalized) and "$" in normalized:
        return CATEGORY_CRYPTO, "dollar target price leg treated as crypto/asset price"
    if re.search(
        r"\b(kxtemp|kxhigh|kxlow|kxrain|kxsnow|kxwind|kxhurr|weather|temperature|rain|"
        r"snow|wind|gust|hurricane|freeze|precipitation)\b",
        normalized,
    ):
        return CATEGORY_WEATHER, "weather metric keyword matched"
    if re.search(
        r"\b(cpi|inflation|fomc|federal reserve|fed|interest rate|unemployment|jobs report|"
        r"payrolls|gdp|recession|treasury|mortgage)\b",
        normalized,
    ):
        return CATEGORY_ECONOMIC, "economic calendar keyword matched"
    if _looks_like_player_prop(leg_text) or re.search(
        r"\b(mlb|nfl|nba|nhl|wnba|epl|uefa|fifa|sports?|baseball|football|basketball|"
        r"hockey|soccer|game|player|team|home run|touchdown|strikeout|runs?)\b",
        normalized,
    ):
        return CATEGORY_SPORTS, "sports league, team, game, or player prop keyword matched"
    if re.search(r"\b(news|headline|announcement|breaking)\b", normalized):
        return CATEGORY_NEWS, "news keyword matched"
    if leg_text:
        return CATEGORY_GENERAL, "general market leg parsed"
    return CATEGORY_UNKNOWN, "no parseable category evidence"


def _known_market_family_category(text: str) -> tuple[str, str] | None:
    normalized = text.upper()
    if "KXMV" in normalized and "CROSSCATEGORY" in normalized:
        return (
            CATEGORY_CROSS_CATEGORY,
            "KXMV cross-category market family matched before general fallback",
        )
    if "KXMV" in normalized and ("SPORTSMULTIGAME" in normalized or "MULTIGAME" in normalized):
        return (
            CATEGORY_SPORTS,
            "KXMV sports multi-game market family matched before general fallback",
        )
    for prefix, reason in ESPORTS_MARKET_FAMILY_PREFIXES + CRICKET_MARKET_FAMILY_PREFIXES:
        if prefix in normalized:
            return CATEGORY_SPORTS, reason
    return None


def _classify_market_type(
    leg_text: str,
    context: str,
    category: str,
    existing_market_type: str | None,
) -> tuple[str, str]:
    normalized = f"{leg_text} {context}".lower()
    leg_normalized = leg_text.lower()
    if re.search(r"\btarget price\b", leg_text, flags=re.I) or _crypto_target_price_symbol(
        leg_text,
        context,
    ):
        return "TARGET_PRICE", "target price leg detected"
    if _looks_like_player_prop(leg_text):
        return "PLAYER_PROP", "player prop count pattern detected"
    if re.search(r"\bboth teams to score\b", normalized):
        return "BOTH_TEAMS_SCORE", "both teams to score pattern detected"
    if (
        re.search(r"\b(beat|win|winner|moneyline)\b", leg_normalized)
        and category == CATEGORY_SPORTS
    ):
        return "MONEYLINE", "sports winner pattern detected"
    if re.search(r"\b(total|over|under)\b", leg_normalized) and category == CATEGORY_SPORTS:
        return "TOTAL", "sports total pattern detected"
    if re.search(r"\b(above|below|over|under|at least|at most|exceed|less than)\b", leg_normalized):
        return "THRESHOLD", "threshold phrase detected"
    if existing_market_type:
        return existing_market_type.upper(), "existing Kalshi market_type used"
    return "UNKNOWN", "no specialized market type detected"


def _extract_operator(text: str) -> str:
    normalized = text.lower()
    if re.search(r"\b(at least|at or above|no less than)\b", normalized) or re.search(
        r":\s*[-+]?\d+(?:\.\d+)?\+",
        text,
    ):
        return "AT_LEAST"
    if re.search(r"\b(at most|at or below|no more than)\b", normalized):
        return "AT_MOST"
    if re.search(r"\b(above|greater than|over|exceed|exceeds)\b", normalized):
        return "ABOVE"
    if re.search(r"\b(below|less than|under)\b", normalized):
        return "BELOW"
    if re.search(r"\b(equal|equals|exactly|target price)\b", normalized):
        return "EQUALS"
    return "UNKNOWN"


def _extract_threshold(text: str) -> tuple[str | None, str | None]:
    money = re.search(r"\$\s*([-+]?\d[\d,]*(?:\.\d+)?)", text)
    if money:
        return money.group(1).replace(",", ""), "USD"
    count = re.search(r":\s*([-+]?\d+(?:\.\d+)?)(\+)?", text)
    if count:
        return count.group(1), "COUNT"
    percent = re.search(r"([-+]?\d+(?:\.\d+)?)\s*%", text)
    if percent:
        return percent.group(1), "PERCENT"
    temperature = re.search(r"([-+]?\d+(?:\.\d+)?)\s*(?:degrees?|deg|f)\b", text, flags=re.I)
    if temperature:
        return temperature.group(1), "F"
    generic = re.search(r"\b([-+]?\d+(?:\.\d+)?)\b", text)
    if generic:
        return generic.group(1), None
    return None, None


def _extract_entity(
    text: str,
    category: str,
    market_type: str,
    context: str,
) -> str | None:
    if market_type == "TARGET_PRICE":
        asset = _asset_symbol(context)
        return asset or "Target Price"
    player_prop = re.match(r"^\s*([^:]{3,120}):\s*[-+]?\d", text)
    if player_prop:
        return _clean(player_prop.group(1))
    if category == CATEGORY_CRYPTO:
        return _asset_symbol(f"{text} {context}")
    if category == CATEGORY_WEATHER:
        location = _weather_location(f"{text} {context}")
        return location or "weather market"
    if category == CATEGORY_ECONOMIC:
        event = _economic_event(f"{text} {context}")
        return event or "economic event"
    words = re.sub(
        r"\b(above|below|over|under|will|the|be|at least|at most)\b",
        " ",
        text,
        flags=re.I,
    )
    words = re.sub(r"[$\d,.:+%-]+", " ", words)
    entity = _clean(words)
    return entity[:120] if entity else None


def _confidence(
    category: str,
    market_type: str,
    entity: str | None,
    threshold: str | None,
) -> str:
    score = 0.25
    if category in LINKED_CATEGORIES:
        score += 0.30
    elif category == CATEGORY_GENERAL:
        score += 0.10
    if market_type != "UNKNOWN":
        score += 0.20
    if entity:
        score += 0.15
    if threshold is not None:
        score += 0.10
    return f"{min(score, 0.95):.2f}"


def _market_context(market: Market) -> str:
    raw = decode_json(market.raw_json)
    parts = [
        market.ticker,
        market.title,
        market.subtitle,
        market.series_ticker,
        market.event_ticker,
        market.market_type,
        market.rules_primary,
        market.rules_secondary,
        raw.get("series_title"),
        raw.get("event_title"),
        raw.get("category"),
        raw.get("tags"),
        _raw_text(raw),
    ]
    return " ".join(str(part or "") for part in parts)


def _looks_like_player_prop(text: str) -> bool:
    return bool(re.search(r"\b[A-Za-z][A-Za-z'.-]+ [A-Za-z][A-Za-z'.-]+:\s*[-+]?\d", text))


def _looks_like_sports_leg_body(text: str) -> bool:
    normalized = text.lower()
    if _looks_like_player_prop(text):
        return True
    return bool(
        re.search(
            r"\b("
            r"runs? scored|goals? scored|points? scored|touchdowns?|home runs?|"
            r"strikeouts?|rebounds?|assists?|saves?|shots on goal|passing yards|"
            r"rushing yards|receiving yards|both teams to score"
            r")\b",
            normalized,
        )
    )


def _crypto_target_price_symbol(leg_text: str, context: str) -> str | None:
    threshold, unit = _extract_threshold(leg_text)
    if unit != "USD" or threshold is None:
        return None
    value = _decimal_or_none(threshold)
    symbol = symbol_for_target_price(value) if value is not None else None
    if symbol is None:
        return None
    evidence = f"{leg_text} {context}".lower()
    if re.search(
        r"\b(target price|crypto|coinbase|coingecko|kraken|binance|"
        r"kxbtc|kxeth|kxsol|kxxrp|kxdoge)\b",
        evidence,
    ):
        return symbol
    return None


def _asset_symbol(text: str) -> str | None:
    return symbol_from_alias_text(text)


def _decimal_or_none(value: object) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, TypeError, ValueError):
        return None


def _weather_location(text: str) -> str | None:
    locations = {
        "kansas_city": r"\b(kansas city|kcmo|mci)\b",
        "new_york": r"\b(new york|nyc|jfk|laguardia|lga)\b",
        "los_angeles": r"\b(los angeles|lax)\b",
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
    for location, pattern in locations.items():
        if re.search(pattern, text, flags=re.I):
            return location
    return None


def _economic_event(text: str) -> str | None:
    normalized = text.lower()
    for key, pattern in {
        "cpi": r"\bcpi|inflation\b",
        "fomc": r"\bfomc|federal reserve|\bfed\b|interest rate",
        "jobs": r"jobs report|payrolls|unemployment",
        "gdp": r"\bgdp\b",
    }.items():
        if re.search(pattern, normalized):
            return key
    return None


def _link_ticker_sets(session: Session) -> dict[str, set[str]]:
    return {
        CATEGORY_CRYPTO: _distinct_tickers(session, CryptoMarketLink),
        CATEGORY_WEATHER: _distinct_tickers(session, WeatherMarketLink),
        CATEGORY_ECONOMIC: _distinct_tickers(session, EconomicMarketLink),
        CATEGORY_SPORTS: _distinct_tickers(session, SportsMarketLink),
        CATEGORY_NEWS: _distinct_tickers(session, NewsMarketLink),
    }


def _link_ticker_counts(session: Session) -> dict[str, int]:
    return {
        CATEGORY_CRYPTO: _distinct_count(session, CryptoMarketLink.ticker),
        CATEGORY_WEATHER: _distinct_count(session, WeatherMarketLink.ticker),
        CATEGORY_ECONOMIC: _distinct_count(session, EconomicMarketLink.ticker),
        CATEGORY_SPORTS: _distinct_count(session, SportsMarketLink.ticker),
        CATEGORY_NEWS: _distinct_count(session, NewsMarketLink.ticker),
    }


def _partial_link_sets(session: Session) -> dict[str, set[str]]:
    return {
        CATEGORY_SPORTS: set(
            _sports_link_reconciliation(session, [])["unresolved_partial_tickers"]
        )
    }


def _derived_link_sets(session: Session) -> dict[str, set[str]]:
    return {
        CATEGORY_SPORTS: set(_sports_link_reconciliation(session, [])["derived_usable_tickers"])
    }


def _market_leg_category_stats(session: Session) -> dict[str, dict[str, int]]:
    rows = session.execute(
        select(
            MarketLeg.category,
            func.count(MarketLeg.id),
            func.count(func.distinct(MarketLeg.ticker)),
        ).group_by(MarketLeg.category)
    )
    return {
        str(category): {
            "parsed_legs": int(parsed_legs or 0),
            "parsed_markets": int(parsed_markets or 0),
        }
        for category, parsed_legs, parsed_markets in rows
    }


def _market_leg_coverage_counts(session: Session) -> dict[str, dict[str, int]]:
    link_pair_selects = [
        select(
            literal(category).label("category"),
            table.ticker.label("ticker"),
        )
        for category, table in LINK_TABLE_BY_CATEGORY.items()
    ]
    link_pairs = union_all(*link_pair_selects).subquery()
    distinct_link_pairs = (
        select(link_pairs.c.category, link_pairs.c.ticker).distinct().subquery()
    )
    linked_ticker = case(
        (distinct_link_pairs.c.ticker.is_not(None), MarketLeg.ticker),
        else_=None,
    )
    linked_leg = case((distinct_link_pairs.c.ticker.is_not(None), 1), else_=0)
    current_market = current_market_predicate(now=utc_now())
    current_ticker = case((current_market, MarketLeg.ticker), else_=None)
    current_leg = case((current_market, 1), else_=0)
    current_linked_ticker = case(
        (
            and_(current_market, distinct_link_pairs.c.ticker.is_not(None)),
            MarketLeg.ticker,
        ),
        else_=None,
    )
    current_linked_leg = case(
        (and_(current_market, distinct_link_pairs.c.ticker.is_not(None)), 1),
        else_=0,
    )
    rows = session.execute(
        select(
            MarketLeg.category,
            func.count(MarketLeg.id).label("parsed_legs"),
            func.count(func.distinct(MarketLeg.ticker)).label("parsed_markets"),
            func.coalesce(func.sum(linked_leg), 0).label("linked_legs"),
            func.count(func.distinct(linked_ticker)).label("linked_markets"),
            func.coalesce(func.sum(current_leg), 0).label("current_parsed_legs"),
            func.count(func.distinct(current_ticker)).label("current_parsed_markets"),
            func.coalesce(func.sum(current_linked_leg), 0).label("current_linked_legs"),
            func.count(func.distinct(current_linked_ticker)).label(
                "current_linked_markets"
            ),
        )
        .join(Market, Market.ticker == MarketLeg.ticker)
        .outerjoin(
            distinct_link_pairs,
            (MarketLeg.category == distinct_link_pairs.c.category)
            & (MarketLeg.ticker == distinct_link_pairs.c.ticker),
        )
        .group_by(MarketLeg.category)
    )
    counts: dict[str, dict[str, int]] = {}
    for (
        category,
        parsed_legs,
        parsed_markets,
        linked_legs,
        linked_markets,
        current_parsed_legs,
        current_parsed_markets,
        current_linked_legs,
        current_linked_markets,
    ) in rows:
        parsed_leg_count = int(parsed_legs or 0)
        linked_leg_count = int(linked_legs or 0)
        current_parsed_leg_count = int(current_parsed_legs or 0)
        current_linked_leg_count = int(current_linked_legs or 0)
        counts[str(category)] = {
            "parsed_legs": parsed_leg_count,
            "parsed_markets": int(parsed_markets or 0),
            "linked_legs": linked_leg_count,
            "linked_markets": int(linked_markets or 0),
            "unlinked_legs": (
                max(parsed_leg_count - linked_leg_count, 0)
                if category in LINKED_CATEGORIES
                else 0
            ),
            "current_parsed_legs": current_parsed_leg_count,
            "current_parsed_markets": int(current_parsed_markets or 0),
            "current_linked_legs": current_linked_leg_count,
            "current_linked_markets": int(current_linked_markets or 0),
            "current_unlinked_legs": (
                max(current_parsed_leg_count - current_linked_leg_count, 0)
                if category in LINKED_CATEGORIES
                else 0
            ),
        }
    return counts


def _linked_market_counts(session: Session) -> dict[str, int]:
    return {
        category: _linked_market_count(session, category, table)
        for category, table in LINK_TABLE_BY_CATEGORY.items()
    }


def _linked_leg_counts(session: Session) -> dict[str, int]:
    return {
        category: _linked_leg_count(session, category, table)
        for category, table in LINK_TABLE_BY_CATEGORY.items()
    }


def _unlinked_leg_counts(session: Session) -> dict[str, int]:
    return {
        category: _unlinked_leg_count(session, category, table)
        for category, table in LINK_TABLE_BY_CATEGORY.items()
    }


def _linked_market_count(session: Session, category: str, table: Any) -> int:
    link_tickers = select(table.ticker).distinct()
    statement = select(func.count(func.distinct(MarketLeg.ticker))).where(
        MarketLeg.category == category,
        MarketLeg.ticker.in_(link_tickers),
    )
    return int(session.scalar(statement) or 0)


def _linked_leg_count(session: Session, category: str, table: Any) -> int:
    link_tickers = select(table.ticker).distinct()
    statement = select(func.count()).select_from(MarketLeg).where(
        MarketLeg.category == category,
        MarketLeg.ticker.in_(link_tickers),
    )
    return int(session.scalar(statement) or 0)


def _unlinked_leg_count(session: Session, category: str, table: Any) -> int:
    link_tickers = select(table.ticker).distinct()
    statement = select(func.count()).select_from(MarketLeg).where(
        MarketLeg.category == category,
        ~MarketLeg.ticker.in_(link_tickers),
    )
    return int(session.scalar(statement) or 0)


def _market_leg_count_for_tickers(
    session: Session,
    tickers: set[str],
    *,
    category: str | None = None,
) -> int:
    if not tickers:
        return 0
    total = 0
    for chunk in _chunks(sorted(tickers), size=500):
        statement = select(func.count()).select_from(MarketLeg).where(MarketLeg.ticker.in_(chunk))
        if category is not None:
            statement = statement.where(MarketLeg.category == category)
        total += int(session.scalar(statement) or 0)
    return total


def _market_leg_distinct_ticker_count(session: Session, category: str) -> int:
    return int(
        session.scalar(
            select(func.count(func.distinct(MarketLeg.ticker))).where(
                MarketLeg.category == category
            )
        )
        or 0
    )


def _chunks(values: list[str], *, size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _sports_link_reconciliation(
    session: Session,
    legs: list[MarketLeg] | None = None,
) -> dict[str, Any]:
    preloaded_legs = legs or None
    sports_legs = (
        [leg for leg in preloaded_legs if leg.category == CATEGORY_SPORTS]
        if preloaded_legs is not None
        else []
    )
    sports_parsed_markets = (
        len({leg.ticker for leg in sports_legs})
        if preloaded_legs is not None
        else _market_leg_distinct_ticker_count(session, CATEGORY_SPORTS)
    )
    provenance_counts = _sports_link_provenance_counts(session)
    raw_partial_tickers = _sports_link_tickers_for_provenance(
        session,
        "partial_market_derived",
    )
    excluded_partial_tickers = _sports_partial_tickers_excluded_from_repair(
        session,
        raw_partial_tickers,
        preloaded_legs=preloaded_legs,
    )
    partial_tickers = raw_partial_tickers - excluded_partial_tickers
    upgraded_tickers = _sports_upgraded_tickers_for_partial_set(session, partial_tickers)
    unresolved_partial_tickers = partial_tickers - upgraded_tickers
    unsupported_multileg = _sports_unsupported_multileg_counts(
        session,
        preloaded_legs=preloaded_legs,
    )

    def leg_count(tickers: set[str]) -> int:
        if not tickers:
            return 0
        if preloaded_legs is not None:
            return sum(1 for leg in preloaded_legs if leg.ticker in tickers)
        return _market_leg_count_for_tickers(session, tickers, category=CATEGORY_SPORTS)

    return {
        "partial_link_rows": provenance_counts["partial_market_derived"]["rows"],
        "raw_partial_markets": len(raw_partial_tickers),
        "excluded_partial_composite_markets": len(excluded_partial_tickers),
        "excluded_partial_composite_tickers": sorted(excluded_partial_tickers),
        "partial_markets": len(partial_tickers),
        "partial_legs": leg_count(partial_tickers),
        "unresolved_partial_markets": len(unresolved_partial_tickers),
        "unresolved_partial_legs": leg_count(unresolved_partial_tickers),
        "unresolved_partial_tickers": sorted(unresolved_partial_tickers),
        "derived_usable_link_rows": provenance_counts["kalshi_event_derived"]["rows"],
        "derived_usable_markets": provenance_counts["kalshi_event_derived"]["markets"],
        "derived_usable_legs": _sports_linked_leg_count_for_provenance(
            session,
            "kalshi_event_derived",
            preloaded_legs=preloaded_legs,
        ),
        "derived_usable_tickers": [],
        "derived_usable_tickers_truncated": True,
        "verified_schedule_link_rows": provenance_counts["verified_schedule"]["rows"],
        "verified_schedule_markets": provenance_counts["verified_schedule"]["markets"],
        "verified_schedule_legs": _sports_linked_leg_count_for_provenance(
            session,
            "verified_schedule",
            preloaded_legs=preloaded_legs,
        ),
        "verified_schedule_tickers": _sports_link_ticker_sample_for_provenance(
            session,
            "verified_schedule",
            limit=25,
        ),
        "other_sports_link_rows": provenance_counts["other"]["rows"],
        "sports_link_rows": sum(row["rows"] for row in provenance_counts.values()),
        "sports_linked_markets": _distinct_count(session, SportsMarketLink.ticker),
        "sports_parsed_markets": sports_parsed_markets,
        "unsupported_multileg_markets": unsupported_multileg["markets"],
        "unsupported_multileg_legs": unsupported_multileg["legs"],
    }


def _sports_unsupported_multileg_counts(
    session: Session,
    *,
    preloaded_legs: list[MarketLeg] | None = None,
) -> dict[str, int]:
    if preloaded_legs is not None:
        unsupported_tickers = {
            leg.ticker
            for leg in preloaded_legs
            if leg.category == CATEGORY_SPORTS and _is_sports_multileg_ticker(leg.ticker)
        }
        return {
            "markets": len(unsupported_tickers),
            "legs": sum(1 for leg in preloaded_legs if leg.ticker in unsupported_tickers),
        }

    link_tickers = select(SportsMarketLink.ticker).distinct()
    statement = (
        select(
            func.count(func.distinct(MarketLeg.ticker)),
            func.count(MarketLeg.id),
        )
        .join(Market, Market.ticker == MarketLeg.ticker)
        .where(
            MarketLeg.category == CATEGORY_SPORTS,
            ~MarketLeg.ticker.in_(link_tickers),
            _unsupported_composite_market_predicate(),
        )
    )
    markets, legs = session.execute(statement).one()
    return {"markets": int(markets or 0), "legs": int(legs or 0)}


def _sports_partial_tickers_excluded_from_repair(
    session: Session,
    tickers: set[str],
    *,
    preloaded_legs: list[MarketLeg] | None = None,
) -> set[str]:
    if not tickers:
        return set()
    cross_category_tickers = {
        ticker for ticker in tickers if _is_cross_category_composite_ticker(ticker)
    }
    if preloaded_legs is not None:
        sports_leg_tickers = {
            leg.ticker
            for leg in preloaded_legs
            if leg.category == CATEGORY_SPORTS and leg.ticker in tickers
        }
    else:
        sports_leg_tickers: set[str] = set()
        for chunk in _chunks(sorted(tickers), size=500):
            sports_leg_tickers.update(
                session.scalars(
                    select(MarketLeg.ticker)
                    .where(
                        MarketLeg.category == CATEGORY_SPORTS,
                        MarketLeg.ticker.in_(chunk),
                    )
                    .distinct()
                )
            )
    return cross_category_tickers | (tickers - sports_leg_tickers)


def _unsupported_composite_counts_by_category(session: Session) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    current_market = current_market_predicate(now=utc_now())
    for category in DISPLAY_CATEGORIES:
        table = LINK_TABLE_BY_CATEGORY.get(category)
        link_tickers = select(table.ticker).distinct() if table is not None else None
        filters = [
            MarketLeg.category == category,
            _unsupported_composite_market_predicate(),
        ]
        if link_tickers is not None:
            filters.append(~MarketLeg.ticker.in_(link_tickers))
        statement = (
            select(
                func.count(func.distinct(MarketLeg.ticker)),
                func.count(MarketLeg.id),
                func.count(
                    func.distinct(case((current_market, MarketLeg.ticker), else_=None))
                ),
                func.coalesce(func.sum(case((current_market, 1), else_=0)), 0),
            )
            .join(Market, Market.ticker == MarketLeg.ticker)
            .where(*filters)
        )
        markets, legs, current_markets, current_legs = session.execute(statement).one()
        counts[category] = {
            "markets": int(markets or 0),
            "legs": int(legs or 0),
            "current_markets": int(current_markets or 0),
            "current_legs": int(current_legs or 0),
        }
    return counts


def _unsupported_composite_market_predicate() -> Any:
    sports_family = "KXMVESPORTSMULTIGAME%"
    cross_category_family = "KXMVECROSSCATEGORY%"
    return (
        func.upper(func.coalesce(Market.ticker, "")).like(sports_family)
        | func.upper(func.coalesce(Market.event_ticker, "")).like(sports_family)
        | func.upper(func.coalesce(Market.series_ticker, "")).like(sports_family)
        | func.upper(func.coalesce(Market.ticker, "")).like(cross_category_family)
        | func.upper(func.coalesce(Market.event_ticker, "")).like(cross_category_family)
        | func.upper(func.coalesce(Market.series_ticker, "")).like(cross_category_family)
    )


def _is_sports_multileg_ticker(ticker: str | None) -> bool:
    return str(ticker or "").upper().startswith("KXMVESPORTSMULTIGAME")


def _is_cross_category_composite_ticker(ticker: str | None) -> bool:
    return str(ticker or "").upper().startswith("KXMVECROSSCATEGORY")


def _sports_link_provenance_case() -> Any:
    reason = func.lower(SportsMarketLink.link_reason)
    game_key = func.lower(SportsMarketLink.game_key)
    return case(
        (
            reason.like("%verified schedule%"),
            "verified_schedule",
        ),
        (
            game_key.like("%kalshi-event-derived%"),
            "kalshi_event_derived",
        ),
        (
            game_key.like("%market-derived%")
            | reason.like("%market-derived%"),
            "partial_market_derived",
        ),
        else_="other",
    )


def _sports_link_provenance_counts(session: Session) -> dict[str, dict[str, int]]:
    provenance = _sports_link_provenance_case().label("provenance")
    rows = session.execute(
        select(
            provenance,
            func.count(SportsMarketLink.id),
            func.count(func.distinct(SportsMarketLink.ticker)),
        ).group_by(provenance)
    )
    counts = {
        "partial_market_derived": {"rows": 0, "markets": 0},
        "kalshi_event_derived": {"rows": 0, "markets": 0},
        "verified_schedule": {"rows": 0, "markets": 0},
        "other": {"rows": 0, "markets": 0},
    }
    for provenance_key, row_count, market_count in rows:
        counts[str(provenance_key)] = {
            "rows": int(row_count or 0),
            "markets": int(market_count or 0),
        }
    return counts


def _sports_link_tickers_for_provenance(session: Session, provenance: str) -> set[str]:
    statement = (
        select(SportsMarketLink.ticker)
        .where(_sports_link_provenance_case() == provenance)
        .distinct()
    )
    return set(session.scalars(statement))


def _sports_upgraded_tickers_for_partial_set(
    session: Session,
    partial_tickers: set[str],
) -> set[str]:
    if not partial_tickers:
        return set()
    upgraded: set[str] = set()
    values = sorted(partial_tickers)
    for chunk in _chunks(values, size=500):
        statement = (
            select(SportsMarketLink.ticker)
            .where(
                SportsMarketLink.ticker.in_(chunk),
                _sports_link_provenance_case().in_(
                    ("kalshi_event_derived", "verified_schedule")
                ),
            )
            .distinct()
        )
        upgraded.update(str(ticker) for ticker in session.scalars(statement))
    return upgraded


def _sports_linked_leg_count_for_provenance(
    session: Session,
    provenance: str,
    *,
    preloaded_legs: list[MarketLeg] | None,
) -> int:
    if preloaded_legs is not None:
        tickers = _sports_link_tickers_for_provenance(session, provenance)
        return sum(1 for leg in preloaded_legs if leg.ticker in tickers)
    link_tickers = (
        select(SportsMarketLink.ticker)
        .where(_sports_link_provenance_case() == provenance)
        .distinct()
    )
    return int(
        session.scalar(
            select(func.count())
            .select_from(MarketLeg)
            .where(MarketLeg.category == CATEGORY_SPORTS, MarketLeg.ticker.in_(link_tickers))
        )
        or 0
    )


def _sports_link_ticker_sample_for_provenance(
    session: Session,
    provenance: str,
    *,
    limit: int,
) -> list[str]:
    statement = (
        select(SportsMarketLink.ticker)
        .where(_sports_link_provenance_case() == provenance)
        .distinct()
        .order_by(SportsMarketLink.ticker)
        .limit(limit)
    )
    return [str(ticker) for ticker in session.scalars(statement)]


def _sports_link_provenance(link: SportsMarketLink) -> str:
    raw = decode_json(link.raw_json)
    source = str(raw.get("source") or "").lower()
    reason = str(link.link_reason or "").lower()
    game_key = str(link.game_key or "").lower()
    if source == "verified_schedule" or "verified schedule" in reason:
        return "verified_schedule"
    if "kalshi-event-derived" in game_key or source == "kalshi_event_derived":
        return "kalshi_event_derived"
    if (
        "market-derived" in game_key
        or source == "market-derived-fallback"
        or "market-derived" in reason
    ):
        return "partial_market_derived"
    return "other"


def _category_row(
    category: str,
    *,
    category_stats: dict[str, dict[str, int]],
    linked_market_counts: dict[str, int],
    table_counts: dict[str, int],
    sports_reconciliation: dict[str, Any],
    unsupported_composite_counts: dict[str, dict[str, int]],
) -> dict[str, Any]:
    stats = category_stats.get(category, {})
    parsed_legs = stats.get("parsed_legs", 0)
    parsed_markets = stats.get("parsed_markets", 0)
    linked_markets = linked_market_counts.get(category, 0)
    current_parsed_legs = stats.get("current_parsed_legs", 0)
    current_parsed_markets = stats.get("current_parsed_markets", 0)
    current_linked_markets = stats.get("current_linked_markets", 0)
    partial_markets = 0
    partial_legs = 0
    partial_link_rows = 0
    derived_markets = 0
    derived_link_rows = 0
    verified_markets = 0
    verified_link_rows = 0
    unsupported_composite = unsupported_composite_counts.get(category, {})
    unsupported_multileg_markets = int(unsupported_composite.get("markets") or 0)
    unsupported_multileg_legs = int(unsupported_composite.get("legs") or 0)
    current_unsupported_multileg_markets = int(
        unsupported_composite.get("current_markets") or 0
    )
    current_unsupported_multileg_legs = int(unsupported_composite.get("current_legs") or 0)
    if category == CATEGORY_SPORTS:
        partial_markets = sports_reconciliation["unresolved_partial_markets"]
        partial_legs = sports_reconciliation["unresolved_partial_legs"]
        partial_link_rows = sports_reconciliation["partial_link_rows"]
        derived_markets = sports_reconciliation["derived_usable_markets"]
        derived_link_rows = sports_reconciliation["derived_usable_link_rows"]
        verified_markets = sports_reconciliation["verified_schedule_markets"]
        verified_link_rows = sports_reconciliation["verified_schedule_link_rows"]
    raw_unlinked_markets = (
        max(parsed_markets - linked_markets, 0) if category in LINKED_CATEGORIES else 0
    )
    unlinked_markets = max(raw_unlinked_markets - unsupported_multileg_markets, 0)
    linkable_markets = (
        max(parsed_markets - unsupported_multileg_markets, 0)
        if category in LINKED_CATEGORIES
        else 0
    )
    current_raw_unlinked_markets = (
        max(current_parsed_markets - current_linked_markets, 0)
        if category in LINKED_CATEGORIES
        else 0
    )
    current_unlinked_markets = max(
        current_raw_unlinked_markets - current_unsupported_multileg_markets,
        0,
    )
    current_linkable_markets = (
        max(current_parsed_markets - current_unsupported_multileg_markets, 0)
        if category in LINKED_CATEGORIES
        else 0
    )
    status = _category_status(
        category,
        parsed_markets,
        linkable_markets,
        linked_markets,
        unlinked_markets,
        partial_markets,
        derived_markets,
        table_counts,
    )
    current_status = _category_status(
        category,
        current_parsed_markets,
        current_linkable_markets,
        current_linked_markets,
        current_unlinked_markets,
        0,
        derived_markets,
        table_counts,
    )
    row = {
        "category": category,
        "parsed_legs": parsed_legs,
        "parsed_markets": parsed_markets,
        "linked_markets": linked_markets,
        "current_parsed_legs": current_parsed_legs,
        "current_parsed_markets": current_parsed_markets,
        "current_linked_markets": current_linked_markets,
        "current_raw_unlinked_markets": current_raw_unlinked_markets,
        "current_unlinked_markets": current_unlinked_markets,
        "current_linkable_markets": current_linkable_markets,
        "historical_unlinked_markets": max(unlinked_markets - current_unlinked_markets, 0),
        "partial_markets": partial_markets,
        "partial_legs": partial_legs,
        "partial_link_rows": partial_link_rows,
        "derived_markets": derived_markets,
        "derived_usable_markets": derived_markets,
        "derived_usable_link_rows": derived_link_rows,
        "verified_schedule_markets": verified_markets,
        "verified_schedule_link_rows": verified_link_rows,
        "linkable_markets": linkable_markets,
        "raw_unlinked_markets": raw_unlinked_markets,
        "unlinked_markets": unlinked_markets,
        "unsupported_multileg_markets": unsupported_multileg_markets,
        "unsupported_multileg_legs": unsupported_multileg_legs,
        "current_unsupported_multileg_markets": current_unsupported_multileg_markets,
        "current_unsupported_multileg_legs": current_unsupported_multileg_legs,
        "coverage_percent": _percent(linked_markets, linkable_markets),
        "current_coverage_percent": _percent(
            current_linked_markets,
            current_linkable_markets,
        ),
        "status": status,
        "status_label": _status_label(status),
        "status_class": _status_class(status),
        "current_status": current_status,
        "current_status_label": _current_status_label(current_status),
        "current_status_class": _status_class(current_status),
        "current_next_action": _current_category_next_action(
            category,
            current_status,
            unsupported_multileg_markets=current_unsupported_multileg_markets,
        ),
        "next_action": _category_next_action(
            category,
            status,
            unsupported_multileg_markets=unsupported_multileg_markets,
        ),
    }
    return row


def _category_status(
    category: str,
    parsed_markets: int,
    linkable_markets: int,
    linked_markets: int,
    unlinked_markets: int,
    partial_markets: int,
    derived_markets: int,
    table_counts: dict[str, int],
) -> str:
    if parsed_markets == 0:
        return "NO_PARSED_MARKETS"
    if category not in LINKED_CATEGORIES:
        return "OBSERVED"
    if linkable_markets == 0:
        return "UNSUPPORTED_MULTI_LEG"
    if linked_markets == 0:
        if _category_feature_count(category, table_counts) > 0:
            return "FEATURES_READY_NO_LINKS"
        return "NEEDS_DATA"
    if partial_markets > 0 or unlinked_markets > 0 or linked_markets < linkable_markets:
        return "PARTIAL"
    if derived_markets >= linkable_markets and linked_markets >= linkable_markets:
        return "DERIVED_CONNECTED"
    return "CONNECTED"


def _category_feature_count(category: str, table_counts: dict[str, int]) -> int:
    return {
        CATEGORY_CRYPTO: table_counts["crypto_features"],
        CATEGORY_WEATHER: table_counts["weather_features"],
        CATEGORY_ECONOMIC: table_counts["economic_features"],
        CATEGORY_SPORTS: table_counts["sports_features"],
        CATEGORY_NEWS: table_counts["news_features"],
    }.get(category, 0)


def _category_next_action(
    category: str,
    status: str,
    *,
    unsupported_multileg_markets: int = 0,
) -> str:
    if status == "NO_PARSED_MARKETS":
        return (
            "No parsed markets in this category snapshot; no coverage action is "
            "required until markets are collected."
        )
    if status == "CONNECTED":
        if unsupported_multileg_markets > 0:
            return (
                "Single-market coverage is complete; unsupported KXMVE composites "
                "are parked outside coverage remediation."
            )
        return (
            "No category coverage action is required; rerun coverage after the next "
            "market refresh."
        )
    if status == "DERIVED_CONNECTED":
        if category == CATEGORY_SPORTS:
            if unsupported_multileg_markets > 0:
                return (
                    "Sports single-market links are covered; KXMVE composites are "
                    "parked for composite support."
                )
            return (
                "Sports single-market links are covered; verified schedules can be "
                "added later for provenance only."
            )
        return (
            "Derived single-market coverage is complete; no category coverage action "
            "is required."
        )
    if status == "UNSUPPORTED_MULTI_LEG":
        return (
            "Parked KXMVE multi-leg composites; keep them out of single-market link "
            "remediation until composite-market support or verified component evidence exists."
        )
    if category == CATEGORY_CRYPTO:
        if status == "FEATURES_READY_NO_LINKS":
            return (
                "Crypto features exist, but no markets are symbol-linked. Rebuild target-price "
                f"asset links with {DEFAULT_CRYPTO_SYMBOLS}, then link-crypto-markets."
            )
        return (
            f"Run ingest-crypto --symbols {DEFAULT_CRYPTO_SYMBOLS}, "
            "build-crypto-features, then link-crypto-markets."
        )
    if category == CATEGORY_WEATHER:
        return (
            "Run ingest-weather with --lat/--lon or --input-file, build-weather-features, "
            "then link-weather-markets."
        )
    if category == CATEGORY_ECONOMIC:
        return (
            "Load economic sample/calendar data, build-economic-features, "
            "then link-economic-markets."
        )
    if category == CATEGORY_SPORTS:
        if status == "UNSUPPORTED_MULTI_LEG":
            return (
                "Sports rows are unsupported KXMVE multi-leg composites; keep them out of "
                "single-game link remediation until composite-market support exists."
            )
        if status == "PARTIAL":
            return (
                "Coverage is connected; run Phase 3AH placeholder/roster watch and "
                "Phase 3Z-R2 provenance repair to find clean verified upgrade candidates."
            )
        if status == "DERIVED_CONNECTED":
            return (
                "Sports single-market links are covered. Remaining raw partial rows are "
                "excluded KXMVE composites; build composite support instead of rerunning "
                "single-market provenance repair."
            )
        if status in {"NEEDS_DATA", "FEATURES_READY_NO_LINKS"}:
            return (
                "Run derive-sports-schedule for Kalshi-derived model links, or ingest verified "
                "sports schedules/teams before link-sports-markets."
            )
        return "Coverage is connected; ingest verified schedules later to upgrade provenance."
    if category == CATEGORY_NEWS:
        return "Run ingest-news, build-news-features, then link-news-markets."
    if category == CATEGORY_CROSS_CATEGORY:
        return (
            "Parked as non-linkable cross-category context; no single-market "
            "coverage action is required."
        )
    if status == "OBSERVED":
        return "Observed as general market context; no category coverage action is required."
    return "Coverage is connected; rerun forecasts after new data ingestion."


def _current_category_next_action(
    category: str,
    status: str,
    *,
    unsupported_multileg_markets: int = 0,
) -> str:
    if status in {"CONNECTED", "DERIVED_CONNECTED"}:
        return (
            "Current coverage is complete; keep linking inside the guarded single-writer "
            "refresh cycle."
        )
    if status == "NO_PARSED_MARKETS":
        return "No current parsed market requires link repair."
    if status == "UNSUPPORTED_MULTI_LEG":
        return _category_next_action(
            category,
            status,
            unsupported_multileg_markets=unsupported_multileg_markets,
        )
    if category == CATEGORY_CRYPTO:
        return (
            "After the writer gate clears, run bounded checkpointed crypto linking for "
            "current markets; keep historical backfill in a separate writer window."
        )
    if category == CATEGORY_WEATHER:
        return (
            "After the writer gate clears, run bounded current-market weather linking "
            "through the single writer."
        )
    return _category_next_action(
        category,
        status,
        unsupported_multileg_markets=unsupported_multileg_markets,
    )


def _top_bottleneck(category_rows: list[dict[str, Any]]) -> dict[str, str]:
    actionable = [row for row in category_rows if row["category"] in LINKED_CATEGORIES]
    if not actionable:
        return {
            "category": "",
            "status": "NEEDS_PARSE",
            "message": "No parsed linkable categories are available yet.",
            "next_action": "Run kalshi-bot market-legs-parse --refresh.",
        }
    row = max(
        actionable,
        key=lambda item: (
            item.get("current_unlinked_markets", item["unlinked_markets"]),
            item["partial_markets"],
        ),
    )
    current_unlinked = int(row.get("current_unlinked_markets", row["unlinked_markets"]))
    if current_unlinked > 0:
        historical_unlinked = int(row.get("historical_unlinked_markets") or 0)
        history_note = (
            f" An additional {historical_unlinked} historical market(s) remain in the "
            "backfill inventory."
            if historical_unlinked > 0
            else ""
        )
        return {
            "category": row["category"],
            "status": "UNLINKED",
            "message": (
                f"{row['category']} has {current_unlinked} current parsed market(s) without "
                f"a matching link table row.{history_note}"
            ),
            "next_action": row.get("current_next_action", row["next_action"]),
        }
    if row["partial_markets"] > 0 and int(row.get("current_parsed_markets") or 0) > 0:
        return {
            "category": row["category"],
            "status": "PARTIAL",
            "message": (
                f"{row['category']} has {row['partial_markets']} partial link(s), usually "
                "market-derived without backing schedule or feature data."
            ),
            "next_action": row["next_action"],
        }
    unsupported_markets = sum(
        int(
            row.get(
                "current_unsupported_multileg_markets",
                row.get("unsupported_multileg_markets") or 0,
            )
        )
        for row in category_rows
    )
    if unsupported_markets > 0:
        return {
            "category": "",
            "status": "CONNECTED",
            "message": (
                "Single-market link coverage is complete; "
                f"{unsupported_markets} unsupported KXMVE composite market(s) are "
                "parked outside single-market remediation."
            ),
            "next_action": (
                "Keep KXMVE composites parked; rerun the Phase 3BB-R3 composite "
                "preview/preflight after fresh component evidence, and proceed only "
                "if paper_composite_review_ready_rows or safe_to_apply_rows is greater "
                "than 0."
            ),
        }
    current_linkable_markets = sum(
        int(row.get("current_linkable_markets") or 0) for row in actionable
    )
    historical_unlinked_markets = sum(
        int(row.get("historical_unlinked_markets") or 0) for row in actionable
    )
    if current_linkable_markets > 0:
        history_note = (
            f" {historical_unlinked_markets} historical market(s) remain queued for "
            "non-urgent backfill."
            if historical_unlinked_markets > 0
            else ""
        )
        return {
            "category": "",
            "status": "CONNECTED",
            "message": (
                f"All {current_linkable_markets} current parsed linkable market(s) are "
                f"covered by link tables.{history_note}"
            ),
            "next_action": (
                "Keep current-market linking in the guarded single-writer cycle; "
                "historical backfill does not block paper readiness."
            ),
        }
    if historical_unlinked_markets > 0:
        return {
            "category": "",
            "status": "CONNECTED",
            "message": (
                "No current parsed linkable market has an actionable link gap; "
                f"{historical_unlinked_markets} historical market(s) remain for bounded "
                "backfill."
            ),
            "next_action": (
                "Do not run an unbounded repair for historical inventory. Backfill it only "
                "through a checkpointed writer window."
            ),
        }
    return {
        "category": "",
        "status": "CONNECTED",
        "message": "Parsed linkable categories are covered by link tables.",
        "next_action": "Keep collecting markets and rerun link coverage after each data refresh.",
    }


def _next_commands(category_rows: list[dict[str, Any]], bottleneck: dict[str, str]) -> list[str]:
    if bottleneck.get("status") == "PARTIAL" and bottleneck.get("category") == CATEGORY_SPORTS:
        return [
            "kalshi-bot phase3ah-round-placeholder-resolution --output-dir reports/phase3ah_sports",
            "kalshi-bot phase3ah-sports-placeholder-watch --output-dir reports/phase3ah_sports",
            "kalshi-bot phase3z-r2-sports-provenance-repair --output-dir reports/phase3z_r2",
            (
                "kalshi-bot phase3ae-roster-candidate-diagnostics "
                "--output-dir reports/phase3ae_roster_candidates"
            ),
            "kalshi-bot link-coverage --output reports/link_coverage_report.md",
        ]
    if any(
        int(row.get("current_unlinked_markets", row["unlinked_markets"])) > 0
        for row in category_rows
    ):
        if bottleneck.get("category") == CATEGORY_CRYPTO:
            return [
                "kalshi-bot db-writer-monitor --json",
                "kalshi-bot db-locks",
                (
                    "kalshi-bot link-crypto-markets --limit 2500 --progress-every 500 "
                    "--checkpoint-every 500 --stop-after-minutes 10 "
                    "--heartbeat-dir reports/crypto_link"
                ),
                "kalshi-bot link-coverage --output reports/link_coverage_report.md",
            ]
        return [
            "kalshi-bot db-writer-monitor --json",
            "kalshi-bot db-locks",
            "kalshi-bot link-remediate",
            "kalshi-bot link-coverage --output reports/link_coverage_report.md",
        ]
    if bottleneck.get("status") == "CONNECTED":
        return []
    return [
        (
            "kalshi-bot phase3bb-r3-composite-preview-gate "
            "--output-dir reports/phase3bb_r3_composites"
        ),
        (
            "kalshi-bot phase3bb-r3-composite-operator-preflight "
            "--output-dir reports/phase3bb_r3_composites"
        ),
        "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage",
        "kalshi-bot link-coverage --output reports/link_coverage_report.md",
    ]


def _example_rows(
    session: Session,
    *,
    link_sets: dict[str, set[str]],
    mode: str,
    limit: int = 20,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if mode == "unlinked":
        for category, table in LINK_TABLE_BY_CATEGORY.items():
            rows.extend(
                _unlinked_example_rows_for_category(
                    session,
                    category=category,
                    table=table,
                    limit=limit - len(rows),
                )
            )
            if len(rows) >= limit:
                break
        return rows[:limit]

    for category, tickers in link_sets.items():
        for chunk in _chunks(sorted(tickers), size=500):
            rows.extend(
                _linked_example_rows_for_ticker_chunk(
                    session,
                    category=category,
                    tickers=chunk,
                    limit=limit - len(rows),
                )
            )
            if len(rows) >= limit:
                return rows[:limit]
    return rows


def _unlinked_example_rows_for_category(
    session: Session,
    *,
    category: str,
    table: Any,
    limit: int,
) -> list[dict[str, str]]:
    if limit <= 0:
        return []
    link_tickers = select(table.ticker).distinct()
    current_market = current_market_predicate(now=utc_now())
    coverage_scope = case(
        (current_market, "CURRENT"),
        else_="HISTORICAL",
    ).label("coverage_scope")
    scope_order = case((current_market, 0), else_=1)
    statement = (
        select(
            MarketLeg.ticker,
            MarketLeg.category,
            MarketLeg.raw_text,
            Market.title,
            coverage_scope,
        )
        .join(Market, Market.ticker == MarketLeg.ticker)
        .where(
            MarketLeg.category == category,
            ~MarketLeg.ticker.in_(link_tickers),
        )
        .order_by(scope_order, MarketLeg.category, MarketLeg.ticker, MarketLeg.leg_index)
        .limit(limit)
    )
    if category in LINKED_CATEGORIES:
        statement = statement.where(~_unsupported_composite_market_predicate())
    return [_coverage_example_payload(row) for row in session.execute(statement)]


def _linked_example_rows_for_ticker_chunk(
    session: Session,
    *,
    category: str,
    tickers: list[str],
    limit: int,
) -> list[dict[str, str]]:
    if limit <= 0 or not tickers:
        return []
    current_market = current_market_predicate(now=utc_now())
    coverage_scope = case(
        (current_market, "CURRENT"),
        else_="HISTORICAL",
    ).label("coverage_scope")
    statement = (
        select(
            MarketLeg.ticker,
            MarketLeg.category,
            MarketLeg.raw_text,
            Market.title,
            coverage_scope,
        )
        .join(Market, Market.ticker == MarketLeg.ticker)
        .where(
            MarketLeg.category == category,
            MarketLeg.ticker.in_(tickers),
        )
        .order_by(MarketLeg.category, MarketLeg.ticker, MarketLeg.leg_index)
        .limit(limit)
    )
    return [_coverage_example_payload(row) for row in session.execute(statement)]


def _coverage_example_payload(row: Any) -> dict[str, str]:
    ticker, category, raw_text, title, coverage_scope = row
    next_action = (
        _current_category_next_action(category, "PARTIAL")
        if coverage_scope == "CURRENT"
        else "Retain for bounded historical backfill; it does not block paper readiness."
    )
    return {
        "ticker": ticker,
        "category": category,
        "title": title or ticker,
        "raw_text": raw_text,
        "scope": coverage_scope,
        "next_action": next_action,
    }


def _link_counts(
    link_counts: dict[str, int],
    sports_reconciliation: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = []
    for category in LINKED_CATEGORIES:
        rows.append(
            {
                "label": f"{category} linked markets",
                "value": int(link_counts.get(category, 0)),
            }
        )
    rows.append(
        {
            "label": "sports partial link rows",
            "value": sports_reconciliation["partial_link_rows"],
        }
    )
    rows.append(
        {
            "label": "sports unresolved partial markets",
            "value": sports_reconciliation["unresolved_partial_markets"],
        }
    )
    rows.append(
        {
            "label": "sports unresolved partial legs",
            "value": sports_reconciliation["unresolved_partial_legs"],
        }
    )
    rows.append(
        {
            "label": "sports unsupported multi-leg composite markets",
            "value": sports_reconciliation["unsupported_multileg_markets"],
        }
    )
    rows.append(
        {
            "label": "sports unsupported multi-leg composite legs",
            "value": sports_reconciliation["unsupported_multileg_legs"],
        }
    )
    rows.append(
        {
            "label": "sports Kalshi-event-derived usable link rows",
            "value": sports_reconciliation["derived_usable_link_rows"],
        }
    )
    rows.append(
        {
            "label": "sports Kalshi-event-derived usable markets",
            "value": sports_reconciliation["derived_usable_markets"],
        }
    )
    rows.append(
        {
            "label": "sports verified schedule link rows",
            "value": sports_reconciliation["verified_schedule_link_rows"],
        }
    )
    rows.append(
        {
            "label": "sports verified schedule markets",
            "value": sports_reconciliation["verified_schedule_markets"],
        }
    )
    return rows


def _count_definitions() -> list[dict[str, str]]:
    return [
        {
            "label": "Current markets",
            "definition": (
                "Markets not explicitly inactive and not past a known close or expiration "
                "time. Current gaps drive the operational bottleneck."
            ),
        },
        {
            "label": "Historical unlinked markets",
            "definition": (
                "Expired or inactive parsed markets without a link row. They remain visible "
                "for bounded backfill but do not block paper readiness."
            ),
        },
        {
            "label": "Partial legs",
            "definition": (
                "Parsed leg rows whose ticker still only has unresolved "
                "market-derived provenance."
            ),
        },
        {
            "label": "Partial markets",
            "definition": (
                "Distinct sports tickers with unresolved market-derived links and no "
                "derived/verified upgrade for that ticker."
            ),
        },
        {
            "label": "Partial link rows",
            "definition": (
                "Raw sports_market_links rows created by market-derived fallback. Multiple "
                "rows can point at one ticker."
            ),
        },
        {
            "label": "Derived-but-usable links",
            "definition": (
                "Kalshi-event-derived sports links that are usable for paper-only model "
                "features, but lack external schedule provenance."
            ),
        },
        {
            "label": "Verified schedule links",
            "definition": "Sports links backed by ingested schedule/team/competition evidence.",
        },
        {
            "label": "Unsupported composites",
            "definition": (
                "KXMVE multi-leg composite markets excluded from the actionable "
                "single-market link gap. They need composite-market support or verified "
                "component evidence, not ordinary link-remediate rows."
            ),
        },
    ]


def _table_counts(session: Session) -> dict[str, int]:
    return {
        "markets": _count(session, Market),
        "market_snapshots": _count(session, MarketSnapshot),
        "market_legs": _count(session, MarketLeg),
        "crypto_features": _count(session, CryptoFeature),
        "crypto_market_links": _count(session, CryptoMarketLink),
        "weather_features": _count(session, WeatherFeature),
        "weather_market_links": _count(session, WeatherMarketLink),
        "economic_features": _count(session, EconomicFeature),
        "economic_market_links": _count(session, EconomicMarketLink),
        "news_features": _count(session, NewsFeature),
        "news_market_links": _count(session, NewsMarketLink),
        "sports_games": _count(session, SportsGame),
        "sports_features": _count(session, SportsFeature),
        "sports_market_links": _count(session, SportsMarketLink),
    }


def _distinct_tickers(session: Session, table: Any) -> set[str]:
    return set(session.scalars(select(table.ticker).distinct()))


def _count(session: Session, table: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(table)) or 0)


def _distinct_count(session: Session, column: Any) -> int:
    return int(session.scalar(select(func.count(func.distinct(column)))) or 0)


def _percent(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "n/a"
    return f"{(numerator / denominator) * 100:.1f}%"


def _status_label(status: str) -> str:
    labels = {
        "NO_PARSED_MARKETS": "No Markets",
        "UNSUPPORTED_MULTI_LEG": "Parked Composite",
    }
    if status in labels:
        return labels[status]
    return status.replace("_", " ").title()


def _current_status_label(status: str) -> str:
    if status == "NO_PARSED_MARKETS":
        return "No Current Markets"
    return _status_label(status)


def _status_class(status: str) -> str:
    if status in {"CONNECTED", "DERIVED_CONNECTED"}:
        return "status-healthy"
    if status in {"PARTIAL", "FEATURES_READY_NO_LINKS"}:
        return "status-degraded"
    return "status-incomplete"


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _raw_text(raw: object) -> str:
    try:
        return json.dumps(raw, sort_keys=True)
    except TypeError:
        return str(raw)
