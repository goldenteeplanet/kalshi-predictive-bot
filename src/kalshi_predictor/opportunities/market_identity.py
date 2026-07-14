from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import re
from typing import Any
from urllib.parse import quote, urlparse

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Market, MarketRanking
from kalshi_predictor.ui.market_display import classify_market_category
from kalshi_predictor.utils.time import utc_now

VERIFIED = "VERIFIED"
VERIFIED_BUT_CLOSED = "VERIFIED_BUT_CLOSED"
VERIFIED_BUT_SETTLED = "VERIFIED_BUT_SETTLED"
VERIFIED_BUT_PAUSED = "VERIFIED_BUT_PAUSED"
BUILT_FROM_EXACT_CATALOG = "BUILT_FROM_EXACT_CATALOG"
UNVERIFIED = "UNVERIFIED"
MALFORMED_URL = "MALFORMED_URL"
API_NOT_FOUND = "API_NOT_FOUND"
MISSING_MARKET_TICKER = "MISSING_MARKET_TICKER"
MARKET_NOT_IN_CATALOG = "MARKET_NOT_IN_CATALOG"
CATALOG_MATCH_MISSING = "CATALOG_MATCH_MISSING"
CATALOG_MATCH_AMBIGUOUS = "CATALOG_MATCH_AMBIGUOUS"
TICKER_MISMATCH = "TICKER_MISMATCH"
CATALOG_STALE = "CATALOG_STALE"
STALE_CATALOG = "STALE_CATALOG"
SYNTHETIC_ONLY = "SYNTHETIC_ONLY"
COMPOSITE_LOCAL_ONLY = "COMPOSITE_LOCAL_ONLY"
PLACEHOLDER_BLOCKED = "PLACEHOLDER_BLOCKED"
PARTIAL_PROVENANCE_BLOCKED = "PARTIAL_PROVENANCE_BLOCKED"
GENERAL_SOURCE_NOT_SAFE = "GENERAL_SOURCE_NOT_SAFE"
AMBIGUOUS_MARKET_IDENTITY = "AMBIGUOUS_MARKET_IDENTITY"

CLICKABLE_STATUSES = {
    VERIFIED,
    VERIFIED_BUT_CLOSED,
    VERIFIED_BUT_SETTLED,
    VERIFIED_BUT_PAUSED,
}
TRADEABLE_STATUSES = {VERIFIED}


@dataclass(frozen=True)
class MarketIdentity:
    market_ticker: str
    event_ticker: str | None
    series_ticker: str | None
    market_title: str
    event_title: str | None
    category: str
    kalshi_url: str | None
    url_verified: bool
    url_verification_status: str
    verified_at: str | None
    source: str
    reason: str
    market_lifecycle_status: str | None
    catalog_last_seen_at: str | None
    source_lineage: str
    api_url: str | None
    diagnostic_only: bool
    tradeable: bool
    badge_kind: str
    status_label: str
    trace: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "kalshi_url_verified": self.url_verified,
                "kalshi_url_status": self.url_verification_status,
                "kalshi_url_reason": self.reason,
                "kalshi_url_verified_at": self.verified_at,
            }
        )
        return payload


@dataclass(frozen=True)
class KalshiUrlBuildResult:
    kalshi_url: str | None
    kalshi_url_status: str
    kalshi_url_reason: str
    kalshi_url_verified: bool
    verified_at: str | None
    event_slug: str | None
    series_slug: str | None
    builder_version: str
    trace: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def market_identity_fields(identity: MarketIdentity) -> dict[str, Any]:
    """Flat API fields required by Phase 3AO opportunity contracts."""
    return {
        "market_ticker": identity.market_ticker,
        "event_ticker": identity.event_ticker,
        "series_ticker": identity.series_ticker,
        "market_title": identity.market_title,
        "event_title": identity.event_title,
        "category": identity.category,
        "kalshi_url": identity.kalshi_url,
        "kalshi_url_verified": identity.url_verified,
        "kalshi_url_status": identity.url_verification_status,
        "kalshi_url_reason": identity.reason,
        "kalshi_url_verified_at": identity.verified_at,
        "market_lifecycle_status": identity.market_lifecycle_status,
        "catalog_last_seen_at": identity.catalog_last_seen_at,
        "source_lineage": identity.source_lineage,
        "diagnostic_only": identity.diagnostic_only,
    }


def annotated_opportunity_row(
    session: Session,
    row: dict[str, Any],
    *,
    ticker: str | None = None,
    ranking: MarketRanking | None = None,
    market: Market | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Attach canonical Phase 3AO market identity fields to a UI/report row."""
    identity = verify_market_identity(
        session,
        ticker=ticker or row.get("ticker") or row.get("market_ticker"),
        ranking=ranking,
        market=market,
        settings=settings,
    )
    payload = dict(row)
    payload.update(market_identity_fields(identity))
    payload["market_identity"] = identity.as_dict()
    if identity.market_title:
        if "market" in payload:
            payload["market"] = identity.market_title
        if "title" in payload:
            payload["title"] = identity.market_title
    if identity.diagnostic_only:
        payload["recommendation"] = _diagnostic_recommendation(identity)
    return payload


def verify_market_identity(
    session: Session,
    *,
    ticker: str | None = None,
    ranking: MarketRanking | None = None,
    market: Market | None = None,
    settings: Settings | None = None,
) -> MarketIdentity:
    resolved_settings = settings or get_settings()
    market_ticker = _clean_text(ticker or _field(ranking, "ticker") or _field(market, "ticker"))
    generated_at = utc_now()
    if not market_ticker:
        return _identity(
            market_ticker="",
            status=MISSING_MARKET_TICKER,
            reason="Opportunity row has no exact market ticker.",
            generated_at=generated_at,
        )

    exact_market = market or session.get(Market, market_ticker)
    market_raw = decode_json(exact_market.raw_json if exact_market else None)
    ranking_raw = decode_json(ranking.raw_json if ranking is not None else None)
    title = _clean_text(
        _field(exact_market, "title")
        or _field(ranking, "title")
        or market_raw.get("title")
        or market_ticker
    )
    event_ticker = _clean_text(
        _field(exact_market, "event_ticker")
        or _field(ranking, "event_ticker")
        or market_raw.get("event_ticker")
    )
    series_ticker = _clean_text(
        _field(exact_market, "series_ticker")
        or _field(ranking, "series_ticker")
        or market_raw.get("series_ticker")
    )
    event_title = _clean_text(
        market_raw.get("event_title")
        or market_raw.get("event_subtitle")
        or market_raw.get("event_name")
        or _field(exact_market, "subtitle")
    )
    category = classify_market_category(title, series_ticker)
    source_lineage = _source_lineage(ranking=ranking, market_raw=market_raw, ranking_raw=ranking_raw)
    common = {
        "market_ticker": market_ticker,
        "event_ticker": event_ticker or None,
        "series_ticker": series_ticker or None,
        "market_title": title or market_ticker,
        "event_title": event_title or None,
        "category": category,
        "market_lifecycle_status": _clean_text(_field(exact_market, "status") if exact_market else None)
        or _clean_text(_field(ranking, "status")),
        "catalog_last_seen_at": _iso(_field(exact_market, "last_seen_at")),
        "source_lineage": source_lineage,
        "trace": _trace(
            market=exact_market,
            ranking=ranking,
            market_raw=market_raw,
            ranking_raw=ranking_raw,
        ),
    }

    pre_catalog_status = _pre_catalog_blocker(
        market_ticker=market_ticker,
        market_raw=market_raw,
        ranking_raw=ranking_raw,
        title=title,
        category=category,
    )
    if pre_catalog_status is not None:
        status, reason = pre_catalog_status
        return _identity(status=status, reason=reason, generated_at=generated_at, **common)

    if exact_market is None:
        return _identity(
            status=MARKET_NOT_IN_CATALOG,
            reason="No exact market ticker exists in the local Kalshi market catalog.",
            generated_at=generated_at,
            **common,
        )

    mismatch = _catalog_mismatch(ranking=ranking, market=exact_market)
    if mismatch:
        return _identity(
            status=AMBIGUOUS_MARKET_IDENTITY,
            reason=mismatch,
            generated_at=generated_at,
            **common,
        )

    evidence_blocker = _evidence_blocker(
        ranking=ranking,
        market_raw=market_raw,
        ranking_raw=ranking_raw,
        category=category,
    )
    if evidence_blocker is not None:
        status, reason = evidence_blocker
        return _identity(status=status, reason=reason, generated_at=generated_at, **common)

    stale_reason = _stale_catalog_reason(
        exact_market,
        generated_at=generated_at,
        stale_after_seconds=resolved_settings.phase_3t_stale_after_seconds,
    )
    if stale_reason is not None:
        return _identity(
            status=STALE_CATALOG,
            reason=stale_reason,
            generated_at=generated_at,
            **common,
        )

    url_build = build_canonical_kalshi_url(
        market=exact_market,
        settings=resolved_settings,
        allow_deterministic_slug=True,
    )
    if url_build.kalshi_url and not _url_matches_market(url_build.kalshi_url, exact_market):
        return _identity(
            status=AMBIGUOUS_MARKET_IDENTITY,
            reason="Stored or built Kalshi URL does not match the exact market or event ticker.",
            generated_at=generated_at,
            kalshi_url=url_build.kalshi_url,
            trace={**common["trace"], "kalshi_url_build": url_build.as_dict()},
            **{key: value for key, value in common.items() if key != "trace"},
        )
    if url_build.kalshi_url_status == VERIFIED:
        lifecycle_status = _lifecycle_verified_status(exact_market.status)
        return _identity(
            status=lifecycle_status,
            reason=_verified_reason(lifecycle_status),
            generated_at=generated_at,
            kalshi_url=url_build.kalshi_url,
            trace={**common["trace"], "kalshi_url_build": url_build.as_dict()},
            **{key: value for key, value in common.items() if key != "trace"},
        )
    if url_build.kalshi_url_status == BUILT_FROM_EXACT_CATALOG and url_build.kalshi_url:
        return _identity(
            status=BUILT_FROM_EXACT_CATALOG,
            reason=url_build.kalshi_url_reason,
            generated_at=generated_at,
            kalshi_url=url_build.kalshi_url,
            trace={**common["trace"], "kalshi_url_build": url_build.as_dict()},
            **{key: value for key, value in common.items() if key != "trace"},
        )
    if not url_build.kalshi_url:
        return _identity(
            status=MALFORMED_URL,
            reason=url_build.kalshi_url_reason,
            generated_at=generated_at,
            trace={**common["trace"], "kalshi_url_build": url_build.as_dict()},
            **{key: value for key, value in common.items() if key != "trace"},
        )
    return _identity(
        status=url_build.kalshi_url_status,
        reason=url_build.kalshi_url_reason,
        generated_at=generated_at,
        kalshi_url=url_build.kalshi_url,
        trace={**common["trace"], "kalshi_url_build": url_build.as_dict()},
        **{key: value for key, value in common.items() if key != "trace"},
    )


def canonical_kalshi_url(market: Market | None) -> str | None:
    result = build_canonical_kalshi_url(market=market)
    return result.kalshi_url if result.kalshi_url_status == VERIFIED else None


def build_canonical_kalshi_url(
    *,
    market: Market | None = None,
    market_ticker: str | None = None,
    event_ticker: str | None = None,
    series_ticker: str | None = None,
    market_title: str | None = None,
    event_title: str | None = None,
    series_title: str | None = None,
    catalog_raw: dict[str, Any] | None = None,
    generated_at: datetime | None = None,
    settings: Settings | None = None,
    allow_deterministic_slug: bool = False,
    allow_stale_proposal: bool = False,
) -> KalshiUrlBuildResult:
    """Build or verify the canonical Kalshi market URL from exact catalog identity."""
    now = generated_at or utc_now()
    resolved = settings or get_settings()
    raw = dict(catalog_raw if catalog_raw is not None else decode_json(market.raw_json if market else None))
    ticker = _clean_text(market_ticker or _field(market, "ticker") or raw.get("ticker"))
    event = _clean_text(event_ticker or _field(market, "event_ticker") or raw.get("event_ticker"))
    series = _clean_text(series_ticker or _field(market, "series_ticker") or raw.get("series_ticker"))
    title = _clean_text(market_title or _field(market, "title") or raw.get("title"))
    event_name = _clean_text(
        event_title
        or raw.get("event_title")
        or raw.get("event_subtitle")
        or raw.get("event_name")
        or _field(market, "subtitle")
    )
    series_name = _clean_text(series_title or raw.get("series_title") or raw.get("series_name"))
    trace = {
        "market_ticker": ticker,
        "event_ticker": event,
        "series_ticker": series,
        "market_title": title,
        "event_title": event_name,
        "series_title": series_name,
        "allow_deterministic_slug": allow_deterministic_slug,
        "allow_stale_proposal": allow_stale_proposal,
    }
    if not ticker:
        return _url_build_result(
            status=MISSING_MARKET_TICKER,
            reason="Exact market_ticker is required before building a Kalshi URL.",
            generated_at=now,
            trace=trace,
        )
    if _blocked_internal_ticker(ticker) or _truthy(raw.get("synthetic_only") or raw.get("synthetic_market")):
        return _url_build_result(
            status=SYNTHETIC_ONLY if not ticker.upper().startswith("KXMVECROSSCATEGORY-") else COMPOSITE_LOCAL_ONLY,
            reason="Synthetic, composite, internal, or local identifiers cannot be promoted to Kalshi URLs.",
            generated_at=now,
            trace=trace,
        )
    if market is None:
        return _url_build_result(
            status=CATALOG_MATCH_MISSING,
            reason="No exact catalog market row is available for this ticker.",
            generated_at=now,
            trace=trace,
        )
    stale_reason = _stale_catalog_reason(
        market,
        generated_at=now,
        stale_after_seconds=resolved.phase_3t_stale_after_seconds,
    )
    if stale_reason is not None and not (allow_deterministic_slug and allow_stale_proposal):
        return _url_build_result(
            status=CATALOG_STALE,
            reason=stale_reason,
            generated_at=now,
            trace=trace,
        )
    stored_url = _stored_kalshi_url(raw)
    if stored_url:
        if _url_matches_market(stored_url, market):
            return _url_build_result(
                status=VERIFIED,
                reason="Stored Kalshi URL matches the exact catalog market or event ticker.",
                generated_at=now,
                kalshi_url=stored_url,
                event_slug=_slug_value(raw, "event_slug", "market_slug", "slug", "event_path", "market_path") or None,
                series_slug=_slug_value(raw, "series_slug", "series_path") or None,
                trace=trace,
            )
        return _url_build_result(
            status=TICKER_MISMATCH,
            reason="Stored Kalshi URL does not contain the exact market or event ticker.",
            generated_at=now,
            kalshi_url=stored_url,
            trace=trace,
        )
    if not allow_deterministic_slug:
        return _url_build_result(
            status=MALFORMED_URL,
            reason="Exact catalog row has no trusted stored Kalshi URL or slug.",
            generated_at=now,
            trace=trace,
        )
    event_slug = _slug_value(raw, "event_slug", "market_slug", "slug", "event_path", "market_path")
    if not event_slug:
        event_slug = _slugify(event_name or title)
    series_slug = _slug_value(raw, "series_slug", "series_path") or _slugify(series_name)
    if not series_slug:
        series_slug = (series or _series_from_ticker(event or ticker)).lower()
    if not event:
        return _url_build_result(
            status=UNVERIFIED,
            reason="Catalog row lacks event_ticker, so the Kalshi event URL shape is uncertain.",
            generated_at=now,
            trace={**trace, "event_slug": event_slug, "series_slug": series_slug},
        )
    if not event_slug:
        return _url_build_result(
            status=MALFORMED_URL,
            reason="Catalog title did not produce a usable URL slug.",
            generated_at=now,
            trace=trace,
        )
    if not series_slug:
        return _url_build_result(
            status=MALFORMED_URL,
            reason="Catalog series slug could not be derived from exact catalog identity.",
            generated_at=now,
            trace={**trace, "event_slug": event_slug},
        )
    built_url = (
        "https://kalshi.com/markets/"
        f"{quote(series_slug.lower(), safe='')}/"
        f"{quote(event_slug.lower(), safe='')}/"
        f"{quote(event.lower(), safe='')}"
    )
    if not _url_matches_market(built_url, market):
        return _url_build_result(
            status=UNVERIFIED,
            reason="Built URL shape does not include the exact market or event ticker.",
            generated_at=now,
            kalshi_url=built_url,
            event_slug=event_slug,
            series_slug=series_slug,
            trace=trace,
        )
    return _url_build_result(
        status=CATALOG_STALE if stale_reason is not None else BUILT_FROM_EXACT_CATALOG,
        reason=stale_reason
        or "Built from exact catalog ticker, event ticker, and deterministic catalog title slug.",
        generated_at=now,
        kalshi_url=built_url,
        event_slug=event_slug,
        series_slug=series_slug,
        trace={**trace, "deterministic_slug": True},
    )


def _url_build_result(
    *,
    status: str,
    reason: str,
    generated_at: datetime,
    kalshi_url: str | None = None,
    event_slug: str | None = None,
    series_slug: str | None = None,
    trace: dict[str, Any] | None = None,
) -> KalshiUrlBuildResult:
    verified = status == VERIFIED and bool(kalshi_url)
    return KalshiUrlBuildResult(
        kalshi_url=kalshi_url,
        kalshi_url_status=status,
        kalshi_url_reason=reason,
        kalshi_url_verified=verified,
        verified_at=generated_at.isoformat() if verified else None,
        event_slug=event_slug,
        series_slug=series_slug,
        builder_version="phase3ar_url_builder_v1",
        trace=trace or {},
    )


def _stored_kalshi_url(raw: dict[str, Any]) -> str:
    for key in (
        "kalshi_url",
        "market_url",
        "trade_url",
        "web_url",
        "event_url",
        "url",
    ):
        value = _clean_text(raw.get(key))
        if value:
            return value
    return ""


def _blocked_internal_ticker(ticker: str) -> bool:
    ticker_upper = ticker.upper()
    return ticker_upper.startswith(("KXMVECROSSCATEGORY-", "KXMVESPORTSMULTIGAMEEXTENDED-"))


def _series_from_ticker(ticker: str) -> str:
    return ticker.split("-", 1)[0] if ticker else ""


def _slugify(value: str) -> str:
    text = _clean_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:90].strip("-")


def _legacy_canonical_kalshi_url(market: Market | None) -> str | None:
    if market is None:
        return None
    raw = decode_json(market.raw_json)
    for key in (
        "kalshi_url",
        "market_url",
        "trade_url",
        "web_url",
        "event_url",
        "url",
    ):
        value = _clean_text(raw.get(key))
        if _is_kalshi_market_url(value):
            return value

    event_ticker = _clean_text(market.event_ticker or raw.get("event_ticker"))
    if not event_ticker:
        return None
    event_slug = _slug_value(
        raw,
        "event_slug",
        "market_slug",
        "slug",
        "event_path",
        "market_path",
    )
    if not event_slug:
        return None
    series_slug = _slug_value(raw, "series_slug", "series_path") or _clean_text(
        market.series_ticker or raw.get("series_ticker")
    ).lower()
    if not series_slug:
        return None
    return (
        "https://kalshi.com/markets/"
        f"{quote(series_slug.lower(), safe='')}/"
        f"{quote(event_slug.lower(), safe='')}/"
        f"{quote(event_ticker.lower(), safe='')}"
    )


def kalshi_api_market_url(ticker: str | None) -> str | None:
    text = _clean_text(ticker)
    if not text:
        return None
    return f"https://external-api.kalshi.com/trade-api/v2/markets/{quote(text, safe='')}"


def is_tradeable_identity(identity: MarketIdentity | dict[str, Any]) -> bool:
    if isinstance(identity, MarketIdentity):
        return identity.tradeable
    return str(identity.get("kalshi_url_status") or identity.get("url_verification_status")) in (
        TRADEABLE_STATUSES
    )


def _identity(
    *,
    market_ticker: str,
    status: str,
    reason: str,
    generated_at: datetime,
    event_ticker: str | None = None,
    series_ticker: str | None = None,
    market_title: str = "",
    event_title: str | None = None,
    category: str = "General",
    kalshi_url: str | None = None,
    market_lifecycle_status: str | None = None,
    catalog_last_seen_at: str | None = None,
    source_lineage: str = "unknown",
    trace: dict[str, Any] | None = None,
) -> MarketIdentity:
    url_verified = bool(kalshi_url) and status in CLICKABLE_STATUSES
    tradeable = status in TRADEABLE_STATUSES and url_verified
    return MarketIdentity(
        market_ticker=market_ticker,
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        market_title=market_title or market_ticker or "Unknown market",
        event_title=event_title,
        category=category,
        kalshi_url=kalshi_url,
        url_verified=url_verified,
        url_verification_status=status,
        verified_at=generated_at.isoformat() if url_verified else None,
        source="kalshi_market_catalog",
        reason=reason,
        market_lifecycle_status=market_lifecycle_status,
        catalog_last_seen_at=catalog_last_seen_at,
        source_lineage=source_lineage,
        api_url=kalshi_api_market_url(market_ticker),
        diagnostic_only=not tradeable,
        tradeable=tradeable,
        badge_kind=_badge_kind(status),
        status_label=status.replace("_", " ").title(),
        trace=trace or {},
    )


def _pre_catalog_blocker(
    *,
    market_ticker: str,
    market_raw: dict[str, Any],
    ranking_raw: dict[str, Any],
    title: str,
    category: str,
) -> tuple[str, str] | None:
    raws = (market_raw, ranking_raw)
    ticker_upper = market_ticker.upper()
    if ticker_upper.startswith("KXMVECROSSCATEGORY-"):
        return (
            COMPOSITE_LOCAL_ONLY,
            "Cross-category composite rows are local-only and have no direct Kalshi market.",
        )
    if ticker_upper.startswith("KXMVESPORTSMULTIGAMEEXTENDED-"):
        return (
            COMPOSITE_LOCAL_ONLY,
            "Extended sports multi-game rows are composite/local-only and not direct markets.",
        )
    if any(_truthy(raw.get("synthetic_only") or raw.get("synthetic_market")) for raw in raws):
        return SYNTHETIC_ONLY, "Synthetic/internal market rows cannot receive Kalshi trade URLs."
    if any(_truthy(raw.get("composite_local_only") or raw.get("local_only")) for raw in raws):
        return COMPOSITE_LOCAL_ONLY, "Composite/local-only row has no direct Kalshi listing."
    if any(_contains_token(raw, "ROUND_PLACEHOLDER", "PLACEHOLDER_BLOCKED") for raw in raws):
        return PLACEHOLDER_BLOCKED, "Sports placeholder provenance is not an exact market identity."
    if category == "Sports" and _placeholder_text(title):
        return PLACEHOLDER_BLOCKED, "Sports placeholder labels are blocked until exact teams exist."
    if any(_contains_token(raw, "PARTIAL_PROVENANCE", "PARTIAL_PROVENANCE_ONLY") for raw in raws):
        return (
            PARTIAL_PROVENANCE_BLOCKED,
            "Partial provenance rows are diagnostic-only until exact market lineage is repaired.",
        )
    return None


def _catalog_mismatch(*, ranking: MarketRanking | None, market: Market) -> str | None:
    if ranking is None:
        return None
    if ranking.event_ticker and market.event_ticker and ranking.event_ticker != market.event_ticker:
        return (
            "Ranking event ticker does not match the exact catalog market event ticker; "
            "sibling or related ticker substitution is blocked."
        )
    if ranking.series_ticker and market.series_ticker and ranking.series_ticker != market.series_ticker:
        return (
            "Ranking series ticker does not match the exact catalog market series ticker; "
            "sibling or related ticker substitution is blocked."
        )
    ranking_raw = decode_json(ranking.raw_json)
    for key in ("sibling_ticker", "related_market_ticker", "substituted_market_ticker"):
        value = _clean_text(ranking_raw.get(key))
        if value and value != market.ticker:
            return "Sibling, related, or substituted ticker evidence cannot create a trade link."
    return None


def _evidence_blocker(
    *,
    ranking: MarketRanking | None,
    market_raw: dict[str, Any],
    ranking_raw: dict[str, Any],
    category: str,
) -> tuple[str, str] | None:
    raws = (market_raw, ranking_raw)
    if any(_truthy(raw.get("general_source_candidate")) for raw in raws):
        if not any(_truthy(raw.get("source_evidence_ready") or raw.get("link_safe")) for raw in raws):
            return (
                GENERAL_SOURCE_NOT_SAFE,
                "General-source candidate lacks source-readiness evidence for link safety.",
            )
    if category == "General" and ranking is not None:
        if _contains_token(ranking_raw, "GENERAL_SOURCE_NOT_SAFE", "SOURCE_EVIDENCE_MISSING"):
            return (
                GENERAL_SOURCE_NOT_SAFE,
                "General-source evidence gates have not marked this row link-safe.",
            )
    return None


def _stale_catalog_reason(
    market: Market,
    *,
    generated_at: datetime,
    stale_after_seconds: int,
) -> str | None:
    lifecycle = str(market.status or "").lower()
    if any(token in lifecycle for token in ("closed", "settled", "final", "paused", "halt")):
        return None
    last_seen = market.last_seen_at
    if last_seen is None:
        return "Catalog market has no last_seen_at timestamp."
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    age_seconds = int((generated_at - last_seen.astimezone(UTC)).total_seconds())
    if age_seconds > stale_after_seconds:
        return (
            f"Catalog row is stale: last_seen_at is {age_seconds}s old "
            f"with a {stale_after_seconds}s threshold."
        )
    return None


def _lifecycle_verified_status(status: Any) -> str:
    text = str(status or "").lower()
    if "settled" in text or "final" in text:
        return VERIFIED_BUT_SETTLED
    if "closed" in text or "expired" in text:
        return VERIFIED_BUT_CLOSED
    if "paused" in text or "halt" in text or "suspend" in text:
        return VERIFIED_BUT_PAUSED
    return VERIFIED


def _verified_reason(status: str) -> str:
    if status == VERIFIED_BUT_CLOSED:
        return "Exact market is in the local catalog and has a real Kalshi URL, but is closed."
    if status == VERIFIED_BUT_SETTLED:
        return "Exact market is in the local catalog and has a real Kalshi URL, but is settled."
    if status == VERIFIED_BUT_PAUSED:
        return "Exact market is in the local catalog and has a real Kalshi URL, but is paused."
    return "Exact market ticker exists in the local catalog and has a real Kalshi market URL."


def _diagnostic_recommendation(identity: MarketIdentity) -> str:
    if identity.url_verification_status == SYNTHETIC_ONLY:
        return "Internal only - no Kalshi listing."
    if identity.url_verification_status == COMPOSITE_LOCAL_ONLY:
        return "Composite/local - no direct Kalshi market."
    if identity.url_verification_status in {
        PLACEHOLDER_BLOCKED,
        PARTIAL_PROVENANCE_BLOCKED,
    }:
        return "Blocked until exact market identity is verified."
    if identity.url_verification_status == BUILT_FROM_EXACT_CATALOG:
        return "Exact catalog URL proposed; run Phase 3AR URL repair before paper entry."
    return f"Diagnostic only - {identity.reason}"


def _url_matches_market(url: str, market: Market) -> bool:
    if not _is_kalshi_market_url(url):
        return False
    path = urlparse(url).path.lower()
    ticker = _clean_text(market.ticker).lower()
    event = _clean_text(market.event_ticker).lower()
    return bool((ticker and ticker in path) or (event and event in path))


def _is_kalshi_market_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    host = parsed.netloc.lower()
    return parsed.scheme == "https" and host in {"kalshi.com", "www.kalshi.com"} and (
        parsed.path.startswith("/markets/")
    )


def _slug_value(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _clean_text(raw.get(key))
        if value:
            return value.strip("/")
    return ""


def _source_lineage(
    *,
    ranking: MarketRanking | None,
    market_raw: dict[str, Any],
    ranking_raw: dict[str, Any],
) -> str:
    candidates = [
        ranking_raw.get("source_lineage"),
        ranking_raw.get("source"),
        ranking_raw.get("lineage"),
        market_raw.get("source"),
        market_raw.get("data_source"),
    ]
    for value in candidates:
        if isinstance(value, dict):
            text = value.get("source") or value.get("name") or value.get("stage")
        else:
            text = value
        cleaned = _clean_text(text)
        if cleaned:
            return cleaned
    if ranking is not None:
        return f"market_rankings:{ranking.forecast_model}"
    return "kalshi_market_catalog"


def _trace(
    *,
    market: Market | None,
    ranking: MarketRanking | None,
    market_raw: dict[str, Any],
    ranking_raw: dict[str, Any],
) -> dict[str, Any]:
    return {
        "parser_version": market_raw.get("parser_version") or ranking_raw.get("parser_version"),
        "linker_version": market_raw.get("linker_version") or ranking_raw.get("linker_version"),
        "forecast_id": ranking_raw.get("forecast_id"),
        "opportunity_id": ranking_raw.get("opportunity_id") or _field(ranking, "id"),
        "paper_decision_id": ranking_raw.get("paper_decision_id"),
        "risk_decision_id": ranking_raw.get("risk_decision_id"),
        "raw_title": _field(market, "title") or _field(ranking, "title"),
    }


def _contains_token(raw: dict[str, Any], *tokens: str) -> bool:
    text = " ".join(str(value) for value in raw.values()).upper()
    return any(token in text for token in tokens)


def _placeholder_text(value: str) -> bool:
    text = value.upper()
    return any(token in text for token in ("TBD", "PLACEHOLDER", "TEAM 1", "TEAM 2", "ROUND "))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "safe", "ready"}


def _badge_kind(status: str) -> str:
    if status == VERIFIED:
        return "good"
    if status in {BUILT_FROM_EXACT_CATALOG, VERIFIED_BUT_CLOSED, VERIFIED_BUT_SETTLED, VERIFIED_BUT_PAUSED}:
        return "caution"
    if status in {UNVERIFIED, MALFORMED_URL, API_NOT_FOUND, STALE_CATALOG}:
        return "warn"
    return "risk"


def _field(value: Any, name: str) -> Any:
    if value is None:
        return None
    return getattr(value, name, None)


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return _clean_text(value) or None
