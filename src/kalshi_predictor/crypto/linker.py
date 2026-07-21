import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import current_market_predicate
from kalshi_predictor.crypto.assets import (
    symbol_for_target_price,
    symbol_from_alias_text,
    symbol_from_event_ticker,
)
from kalshi_predictor.crypto.repository import (
    get_latest_crypto_link_for_ticker,
    insert_crypto_market_link,
)
from kalshi_predictor.crypto.semantics import (
    AMBIGUOUS,
    EXACT_LINK,
    NOT_CRYPTO,
    UNSUPPORTED,
    parse_crypto_market_terms,
)
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import CryptoMarketLink, Market, MarketLeg


@dataclass(frozen=True)
class CryptoLinkResult:
    markets_scanned: int
    links_created: int
    btc_links: int
    eth_links: int
    generic_links: int
    markets_processed: int = 0
    stopped_early: bool = False
    last_ticker: str | None = None
    already_linked: int = 0
    target_price_links: int = 0
    multi_asset_links: int = 0
    links_by_symbol: dict[str, int] = field(default_factory=dict)
    rejected_by_reason: dict[str, int] | None = None
    exact_semantic_links: int = 0
    ambiguous_markets: int = 0
    unsupported_markets: int = 0


@dataclass(frozen=True)
class CryptoComponent:
    symbol: str
    side: str | None
    direction: str
    threshold_value: str | None
    source_event: str | None
    source_market: str | None
    raw_text: str | None


def link_crypto_markets(
    session: Session,
    *,
    limit: int | None = None,
    tickers: list[str] | None = None,
    current_unlinked_only: bool = False,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    progress_every: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> CryptoLinkResult:
    session.flush()
    statement = select(Market).order_by(Market.ticker)
    scoped_tickers = _unique_tickers(tickers or []) if tickers is not None else None
    if scoped_tickers is not None and not scoped_tickers:
        markets = []
        legs_by_ticker = {}
    else:
        if scoped_tickers is not None:
            statement = statement.where(Market.ticker.in_(scoped_tickers))
        if current_unlinked_only:
            statement = statement.where(
                Market.ticker.in_(
                    select(MarketLeg.ticker)
                    .where(MarketLeg.category == "crypto")
                    .distinct()
                ),
                ~Market.ticker.in_(select(CryptoMarketLink.ticker).distinct()),
                current_market_predicate(),
                ~_unsupported_composite_market_predicate(),
            )
        if limit is not None:
            statement = statement.limit(limit)
        markets = list(session.scalars(statement))
        market_tickers = [market.ticker for market in markets]
        legs_by_ticker = (
            _crypto_legs_by_ticker(session, tickers=market_tickers)
            if limit is not None or scoped_tickers is not None or current_unlinked_only
            else _crypto_legs_by_ticker(session)
        )
    btc_links = 0
    eth_links = 0
    generic_links = 0
    links_created = 0
    already_linked = 0
    target_price_links = 0
    multi_asset_links = 0
    by_symbol: dict[str, int] = {}
    rejected: dict[str, int] = {}
    exact_semantic_links = 0
    ambiguous_markets = 0
    unsupported_markets = 0
    markets_processed = 0
    stopped_early = False
    last_ticker: str | None = None

    for index, market in enumerate(markets, start=1):
        if should_stop is not None and should_stop():
            stopped_early = True
            _emit_progress(
                progress_callback,
                progress_every=progress_every,
                processed=markets_processed,
                total=len(markets),
                ticker=market.ticker,
                status="STOPPED_EARLY",
                created=links_created,
            )
            break
        markets_processed = index
        last_ticker = market.ticker
        legs = legs_by_ticker.get(market.ticker, [])
        terms = parse_crypto_market_terms(market, legs=legs)
        if terms.status == AMBIGUOUS:
            ambiguous_markets += 1
        elif terms.status == UNSUPPORTED:
            unsupported_markets += 1
        symbol, confidence, reason = detect_crypto_market(market, legs=legs)
        if confidence <= 0 or symbol is None:
            rejected[reason] = rejected.get(reason, 0) + 1
            _emit_progress(
                progress_callback,
                progress_every=progress_every,
                processed=index,
                total=len(markets),
                ticker=market.ticker,
                status="REJECTED",
                created=links_created,
            )
            continue
        latest_link = get_latest_crypto_link_for_ticker(session, market.ticker)
        if latest_link is not None and latest_link.symbol == symbol:
            already_linked += 1
            _emit_progress(
                progress_callback,
                progress_every=progress_every,
                processed=index,
                total=len(markets),
                ticker=market.ticker,
                status="ALREADY_LINKED",
                created=links_created,
            )
            continue
        raw_market = decode_json(market.raw_json)
        insert_crypto_market_link(
            session,
            ticker=market.ticker,
            symbol=symbol,
            confidence=confidence,
            reason=reason,
            raw_json={
                "ticker": market.ticker,
                "title": market.title,
                "series_ticker": market.series_ticker,
                "event_ticker": market.event_ticker,
                "raw_market": raw_market,
                "component_symbols": _component_symbols(market, legs),
                "components": [_component_payload(item) for item in _components(market, legs)],
                "structured_terms": terms.as_payload(),
                "lineage": {
                    "linker_version": "crypto_linker_v2_semantic_terms",
                    "semantic_status": terms.status,
                    "semantic_idempotency_key": terms.idempotency_key,
                    "paper_only_safety": "PAPER_ONLY_NO_EXCHANGE_WRITES",
                },
            },
        )
        links_created += 1
        if terms.status == EXACT_LINK:
            exact_semantic_links += 1
        if "target price" in reason.lower():
            target_price_links += 1
        if "+" in symbol:
            multi_asset_links += 1
        if symbol == "BTC":
            btc_links += 1
        elif symbol == "ETH":
            eth_links += 1
        else:
            generic_links += 1
        by_symbol[symbol] = by_symbol.get(symbol, 0) + 1
        _emit_progress(
            progress_callback,
            progress_every=progress_every,
            processed=index,
            total=len(markets),
            ticker=market.ticker,
            status="LINK_CREATED",
            created=links_created,
        )

    _emit_progress(
        progress_callback,
        progress_every=progress_every,
        processed=markets_processed,
        total=len(markets),
        ticker=last_ticker or "",
        status="COMPLETE" if not stopped_early else "STOPPED_EARLY",
        created=links_created,
    )
    return CryptoLinkResult(
        markets_scanned=len(markets),
        links_created=links_created,
        btc_links=btc_links,
        eth_links=eth_links,
        generic_links=generic_links,
        markets_processed=markets_processed,
        stopped_early=stopped_early,
        last_ticker=last_ticker,
        already_linked=already_linked,
        target_price_links=target_price_links,
        multi_asset_links=multi_asset_links,
        links_by_symbol=by_symbol,
        rejected_by_reason=rejected,
        exact_semantic_links=exact_semantic_links,
        ambiguous_markets=ambiguous_markets,
        unsupported_markets=unsupported_markets,
    )


def _unsupported_composite_market_predicate() -> Any:
    columns = (Market.ticker, Market.event_ticker, Market.series_ticker)
    prefixes = ("KXMVECROSSCATEGORY%", "KXMVESPORTSMULTIGAME%")
    return or_(
        *(
            func.upper(func.coalesce(column, "")).like(prefix)
            for column in columns
            for prefix in prefixes
        )
    )


def detect_crypto_market(
    market: Market,
    *,
    legs: list[MarketLeg] | None = None,
) -> tuple[str | None, Decimal, str]:
    resolved_legs = legs or []
    terms = parse_crypto_market_terms(market, legs=resolved_legs)
    target_symbol, target_confidence, target_reason = _target_price_asset_match(
        market,
        resolved_legs,
    )
    if (
        target_symbol is not None or target_reason != "No crypto keyword match."
    ) and ("target price" in _market_text(market).lower() or resolved_legs):
        return target_symbol, target_confidence, target_reason
    if terms.status == EXACT_LINK and terms.symbol is not None:
        normalized = _market_text(market).lower()
        confidence = Decimal("0.90")
        if terms.symbol == "BTC" and (
            re.search(r"(^|[^a-z0-9])(btc|xbt)([^a-z0-9]|$)", normalized)
            or re.search(r"\bkxbtc\b|\bbtcusd\b|\bbitcoin\s+price\b", normalized)
        ):
            confidence = Decimal("1.0")
        elif terms.reason_codes == ("asset_alias_or_event_symbol",):
            confidence = Decimal("0.8")
        return (
            terms.symbol,
            confidence,
            f"Structured crypto settlement semantics matched {terms.symbol}.",
        )
    if target_symbol is not None or target_reason != "No crypto keyword match.":
        return target_symbol, target_confidence, target_reason
    if terms.status == AMBIGUOUS:
        return None, Decimal("0.0"), "; ".join(terms.reason_codes) or "Ambiguous crypto market."
    if terms.status == UNSUPPORTED:
        return (
            None,
            Decimal("0.0"),
            "; ".join(terms.reason_codes) or "Unsupported crypto market terms.",
        )
    if terms.status == NOT_CRYPTO:
        return None, Decimal("0.0"), "No crypto keyword match."

    text = _market_text(market)
    normalized = text.lower()
    alias_symbol = _contextual_alias_symbol(text)
    if re.search(r"(^|[^a-z0-9])(btc|xbt)([^a-z0-9]|$)", normalized) or re.search(
        r"\bkxbtc\b|\bbtcusd\b|\bbitcoin\s+price\b",
        normalized,
    ):
        return "BTC", Decimal("1.0"), "Exact BTC symbol match."
    if re.search(r"(^|[^a-z0-9])eth([^a-z0-9]|$)", normalized) or re.search(
        r"\bkxeth\b|\bethusd\b|\bether\s+price\b",
        normalized,
    ):
        return "ETH", Decimal("1.0"), "Exact ETH symbol match."
    if alias_symbol is not None:
        return alias_symbol, Decimal("0.8"), f"{alias_symbol} alias word match."
    if re.search(r"\b(crypto|cryptocurrency|cryptocurrencies|digital asset)\b", normalized):
        return "CRYPTO", Decimal("0.6"), "Generic crypto keyword match."
    return None, Decimal("0.0"), "No crypto keyword match."


def _contextual_alias_symbol(text: str) -> str | None:
    symbol = symbol_from_alias_text(text)
    if symbol is None:
        return None
    normalized = text.lower()
    if re.search(
        r"\b(target price|crypto|cryptocurrency|cryptocurrencies|digital asset|"
        r"coinbase|coingecko|kraken|binance|"
        r"kxbtc|kxeth|kxsol|kxxrp|kxdoge|"
        r"btcusd|ethusd|solusd|xrpusd|dogeusd|"
        r"btc-usd|eth-usd|sol-usd|xrp-usd|doge-usd)\b",
        normalized,
    ):
        return symbol
    if re.search(r"\$\s*[-+]?\d", text):
        return symbol
    if re.search(
        r"\b(above|below|over|under|exceed|exceeds|greater than|less than|at or above|"
        r"at or below)\s+\$?\s*[-+]?\d",
        normalized,
    ):
        return symbol
    return None


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
    if (
        status not in {"COMPLETE", "STOPPED_EARLY"}
        and cadence
        and processed % cadence != 0
        and processed != total
    ):
        return
    progress_callback(
        {
            "stage": "crypto_link",
            "processed": processed,
            "total": total,
            "ticker": ticker,
            "status": status,
            "created": created,
        }
    )


def _unique_tickers(tickers: list[str]) -> list[str]:
    return list(dict.fromkeys(str(ticker) for ticker in tickers if str(ticker or "").strip()))


def _crypto_legs_by_ticker(
    session: Session,
    *,
    tickers: list[str] | None = None,
) -> dict[str, list[MarketLeg]]:
    statement = (
        select(MarketLeg)
        .where(
            MarketLeg.category == "crypto",
            MarketLeg.market_type == "TARGET_PRICE",
        )
        .order_by(MarketLeg.ticker, MarketLeg.leg_index)
    )
    if tickers is not None:
        if not tickers:
            return {}
        statement = statement.where(MarketLeg.ticker.in_(tickers))
    rows = list(
        session.scalars(statement)
    )
    grouped: dict[str, list[MarketLeg]] = {}
    for row in rows:
        grouped.setdefault(row.ticker, []).append(row)
    return grouped


def _target_price_asset_match(
    market: Market,
    legs: list[MarketLeg],
) -> tuple[str | None, Decimal, str]:
    components = _components(market, legs)
    if components:
        symbol = _link_symbol_from_components(components)
        if "+" in symbol:
            return (
                symbol,
                Decimal("0.72"),
                f"Multi-asset target price component match: {symbol}.",
            )
        return (
            symbol,
            Decimal("0.75"),
            f"{symbol} target price component match.",
        )

    market_text = _market_text(market)
    prices = _target_prices_from_legs(legs)
    if not prices:
        prices = _target_prices_from_text(market_text)
    if not prices:
        return None, Decimal("0.0"), "No crypto keyword match."

    event_symbol = symbol_from_event_ticker(market.event_ticker) or symbol_from_event_ticker(
        market.series_ticker
    )
    if event_symbol is not None:
        return (
            event_symbol,
            Decimal("0.95"),
            f"Explicit {event_symbol} event or series ticker target price match.",
        )

    symbols = {symbol_for_target_price(price) for price in prices}
    supported_symbols = {symbol for symbol in symbols if symbol is not None}
    unsupported_count = sum(1 for symbol in symbols if symbol is None)
    if len(supported_symbols) == 1:
        symbol = next(iter(supported_symbols))
        if unsupported_count:
            return (
                None,
                Decimal("0.0"),
                f"Ambiguous target price legs include unsupported assets beside {symbol}.",
            )
        return (
            symbol,
            Decimal("0.75"),
            f"{symbol} target price range match from parsed Target Price legs.",
        )
    if len(supported_symbols) > 1:
        if unsupported_count:
            joined = "/".join(sorted(supported_symbols))
            return (
                None,
                Decimal("0.0"),
                f"Ambiguous target price legs include unsupported assets beside {joined}.",
            )
        symbol = "+".join(sorted(supported_symbols))
        return (
            symbol,
            Decimal("0.70"),
            f"Multi-asset target price range match: {symbol}.",
        )
    return (
        None,
        Decimal("0.0"),
        "Unsupported target price legs did not match supported crypto ranges.",
    )


def _target_prices_from_legs(legs: list[MarketLeg]) -> list[Decimal]:
    prices: list[Decimal] = []
    for leg in legs:
        if leg.unit != "USD" or leg.threshold_value is None:
            continue
        parsed = _decimal_or_none(leg.threshold_value)
        if parsed is not None:
            prices.append(parsed)
    return prices


def _target_prices_from_text(text: str) -> list[Decimal]:
    prices: list[Decimal] = []
    for match in re.finditer(r"target price[^$]{0,80}\$\s*([-+]?\d[\d,]*(?:\.\d+)?)", text, re.I):
        parsed = _decimal_or_none(match.group(1).replace(",", ""))
        if parsed is not None:
            prices.append(parsed)
    return prices


def _decimal_or_none(value: object) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except Exception:  # noqa: BLE001 - malformed market text should just stay unlinked.
        return None


def _components(market: Market, legs: list[MarketLeg]) -> list[CryptoComponent]:
    raw = decode_json(market.raw_json)
    custom = raw.get("custom_strike")
    if isinstance(custom, dict):
        rows = _components_from_custom_strike(custom, legs)
        if rows:
            return rows
    return _components_from_target_prices(legs)


def _components_from_custom_strike(
    custom: dict[str, Any],
    legs: list[MarketLeg],
) -> list[CryptoComponent]:
    events = _csv_values(custom.get("Associated Events"))
    markets = _csv_values(custom.get("Associated Markets"))
    sides = _csv_values(custom.get("Associated Market Sides"))
    rows: list[CryptoComponent] = []
    for index, event in enumerate(events):
        source_market = markets[index] if index < len(markets) else None
        symbol = symbol_from_event_ticker(event) or symbol_from_event_ticker(source_market)
        if symbol is None:
            continue
        side = sides[index].upper() if index < len(sides) and sides[index] else None
        leg = legs[index] if index < len(legs) else None
        rows.append(
            CryptoComponent(
                symbol=symbol,
                side=side,
                direction=_component_direction(side=side, leg=leg),
                threshold_value=leg.threshold_value if leg else None,
                source_event=event,
                source_market=source_market,
                raw_text=leg.raw_text if leg else None,
            )
        )
    return rows


def _components_from_target_prices(legs: list[MarketLeg]) -> list[CryptoComponent]:
    rows: list[CryptoComponent] = []
    for leg in legs:
        if leg.unit != "USD" or leg.threshold_value is None:
            continue
        price = _decimal_or_none(leg.threshold_value)
        symbol = symbol_for_target_price(price) if price is not None else None
        if symbol is None:
            continue
        rows.append(
            CryptoComponent(
                symbol=symbol,
                side=leg.side,
                direction=_component_direction(side=leg.side, leg=leg),
                threshold_value=leg.threshold_value,
                source_event=None,
                source_market=None,
                raw_text=leg.raw_text,
            )
        )
    return rows


def _component_direction(*, side: str | None, leg: MarketLeg | None) -> str:
    normalized_side = str(side or "").upper()
    operator = str(getattr(leg, "operator", "") or "").upper()
    if operator in {"BELOW", "AT_MOST"}:
        return "BELOW" if normalized_side != "NO" else "ABOVE"
    if operator in {"ABOVE", "AT_LEAST", "EQUALS", "UNKNOWN", ""}:
        return "BELOW" if normalized_side == "NO" else "ABOVE"
    return "UNKNOWN"


def _component_symbols(market: Market, legs: list[MarketLeg]) -> list[str]:
    return sorted({component.symbol for component in _components(market, legs)})


def _link_symbol_from_components(components: list[CryptoComponent]) -> str:
    return "+".join(sorted({component.symbol for component in components}))


def _component_payload(component: CryptoComponent) -> dict[str, Any]:
    return {
        "symbol": component.symbol,
        "side": component.side,
        "direction": component.direction,
        "threshold_value": component.threshold_value,
        "source_event": component.source_event,
        "source_market": component.source_market,
        "raw_text": component.raw_text,
    }


def _csv_values(value: object) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]
