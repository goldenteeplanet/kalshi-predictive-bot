from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import Market
from kalshi_predictor.synthetic_markets.contracts import (
    LISTING_EXACT_MATCH,
    LISTING_NO_EXACT_MATCH,
    LISTING_RELATED,
    LISTING_UNKNOWN,
    ListingCheckResult,
    ListingMatch,
    SyntheticContractSpec,
    SyntheticEventSpec,
    stable_phase_3r_id,
)


def check_local_listing_status(
    session: Session,
    *,
    run_id: str,
    event: SyntheticEventSpec,
    contracts: tuple[SyntheticContractSpec, ...],
    checked_at: datetime,
    market_limit: int = 5000,
) -> ListingCheckResult:
    """Classify equivalence against locally ingested Kalshi markets without API writes."""

    session.flush()
    market_count = int(session.scalar(select(func.count()).select_from(Market)) or 0)
    if market_count == 0:
        return ListingCheckResult(
            listing_check_id=stable_phase_3r_id(
                "listing-check",
                run_id,
                event.synthetic_event_id,
                checked_at.isoformat(),
            ),
            checked_at=checked_at,
            status=LISTING_UNKNOWN,
            pagination_complete=False,
            live_coverage_complete=False,
            historical_coverage_status="UNKNOWN_NO_LOCAL_MARKET_INVENTORY",
            historical_cutoff=None,
            matches=(),
            warnings=(
                "No locally ingested market inventory exists; cannot claim not-listed.",
            ),
        )

    markets = list(session.scalars(select(Market).order_by(Market.ticker).limit(market_limit)))
    historical_cutoff = session.scalar(select(func.min(Market.first_seen_at)))
    coverage_complete = market_count <= market_limit
    if not coverage_complete:
        return ListingCheckResult(
            listing_check_id=stable_phase_3r_id(
                "listing-check",
                run_id,
                event.synthetic_event_id,
                checked_at.isoformat(),
            ),
            checked_at=checked_at,
            status=LISTING_UNKNOWN,
            pagination_complete=False,
            live_coverage_complete=False,
            historical_coverage_status="UNKNOWN_LOCAL_SCAN_TRUNCATED",
            historical_cutoff=historical_cutoff,
            matches=(),
            warnings=(
                f"Local market inventory has {market_count} rows; scan limit was {market_limit}.",
            ),
        )

    exact_matches: list[ListingMatch] = []
    related_matches: list[ListingMatch] = []
    event_text = normalize_market_text(event.canonical_title)
    contract_texts = {normalize_market_text(contract.canonical_question) for contract in contracts}
    for market in markets:
        normalized_fields = {
            "title": normalize_market_text(market.title),
            "subtitle": normalize_market_text(market.subtitle),
            "rules_primary": normalize_market_text(market.rules_primary),
        }
        score = max(
            [_text_similarity(event_text, text) for text in normalized_fields.values()]
            + [
                _text_similarity(contract_text, text)
                for contract_text in contract_texts
                for text in normalized_fields.values()
            ]
        )
        if score == Decimal("1"):
            exact_matches.append(
                _match(event=event, market=market, match_class=LISTING_EXACT_MATCH, score=score)
            )
        elif score >= Decimal("0.35"):
            related_matches.append(
                _match(event=event, market=market, match_class=LISTING_RELATED, score=score)
            )

    matches = tuple(
        exact_matches
        or sorted(related_matches, key=lambda item: item.semantic_score, reverse=True)[:5]
    )
    if exact_matches:
        status = LISTING_EXACT_MATCH
    elif related_matches:
        status = LISTING_RELATED
    else:
        status = LISTING_NO_EXACT_MATCH
    return ListingCheckResult(
        listing_check_id=stable_phase_3r_id(
            "listing-check",
            run_id,
            event.synthetic_event_id,
            checked_at.isoformat(),
        ),
        checked_at=checked_at,
        status=status,
        pagination_complete=True,
        live_coverage_complete=True,
        historical_coverage_status="COMPLETE_RELEVANT_LOCAL_SCOPE",
        historical_cutoff=historical_cutoff,
        matches=matches,
        warnings=("Read-only local repository listing check; no exchange write endpoints called.",),
    )


def normalize_market_text(value: str | None) -> str:
    text = (value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _text_similarity(left: str, right: str) -> Decimal:
    if not left or not right:
        return Decimal("0")
    if left == right:
        return Decimal("1")
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return Decimal("0")
    overlap = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return Decimal(overlap) / Decimal(union)


def _match(
    *,
    event: SyntheticEventSpec,
    market: Market,
    match_class: str,
    score: Decimal,
) -> ListingMatch:
    return ListingMatch(
        match_id=stable_phase_3r_id(
            "listing-match",
            event.synthetic_event_id,
            market.ticker,
            match_class,
        ),
        kalshi_series_ticker=market.series_ticker,
        kalshi_event_ticker=market.event_ticker,
        kalshi_market_ticker=market.ticker,
        match_class=match_class,
        semantic_score=score.quantize(Decimal("0.0001")),
        logical_comparison=(
            "Exact normalized text match."
            if match_class == LISTING_EXACT_MATCH
            else "Related local market text, but settlement semantics were not equivalent."
        ),
        field_differences={
            "synthetic_title": event.canonical_title,
            "market_title": market.title,
            "market_subtitle": market.subtitle,
        },
        effective_at=market.first_seen_at,
    )
