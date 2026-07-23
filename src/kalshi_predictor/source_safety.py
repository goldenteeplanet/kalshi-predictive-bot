from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from kalshi_predictor.utils.time import parse_datetime

OFFICIAL_ECONOMIC_HOSTS = frozenset(
    {
        "api.bls.gov",
        "bls.gov",
        "www.bls.gov",
        "bea.gov",
        "www.bea.gov",
        "fred.stlouisfed.org",
        "federalreserve.gov",
        "www.federalreserve.gov",
    }
)
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})


def canonicalize_source_url(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("source URL must be an absolute HTTP(S) URL")
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
        and not key.lower().startswith(TRACKING_QUERY_PREFIXES)
    ]
    host = parts.hostname.lower()
    port = f":{parts.port}" if parts.port else ""
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), f"{host}{port}", path, urlencode(query), ""))


def validate_point_in_time(
    *,
    published_at: datetime | str | None,
    available_at: datetime | str | None,
    ingested_at: datetime | str | None,
    decision_at: datetime | str | None = None,
) -> dict[str, str | None]:
    published = _required_time(published_at, "published_at")
    available = _required_time(available_at, "available_at")
    ingested = _required_time(ingested_at, "ingested_at")
    decision = parse_datetime(decision_at)
    if published > available:
        raise ValueError("published_at is after available_at")
    if available > ingested:
        raise ValueError("available_at is after ingested_at")
    if decision is not None and available > decision:
        raise ValueError("source was not available at the decision timestamp")
    return {
        "published_at": published.isoformat(),
        "available_at": available.isoformat(),
        "ingested_at": ingested.isoformat(),
        "decision_at": decision.isoformat() if decision else None,
    }


def validate_official_economic_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    canonical_url = canonicalize_source_url(
        str(payload.get("source_url") or payload.get("url") or "")
    )
    host = urlsplit(canonical_url or "").hostname or ""
    if host not in OFFICIAL_ECONOMIC_HOSTS:
        raise ValueError(f"economic source host is not official: {host or 'missing'}")
    timestamps = validate_point_in_time(
        published_at=payload.get("published_at"),
        available_at=payload.get("available_at"),
        ingested_at=payload.get("ingested_at"),
        decision_at=payload.get("decision_at"),
    )
    return {**dict(payload), **timestamps, "source_url": canonical_url, "official_source": True}


def normalize_news_evidence(
    payload: Mapping[str, Any],
    *,
    feed_url: str,
    available_at: datetime | str,
    ingested_at: datetime | str,
) -> dict[str, Any]:
    canonical_feed = canonicalize_source_url(feed_url)
    if urlsplit(canonical_feed or "").scheme != "https":
        raise ValueError("RSS feed URL must use HTTPS")
    published = payload.get("published_at")
    timestamps = validate_point_in_time(
        published_at=published,
        available_at=available_at,
        ingested_at=ingested_at,
    )
    article_url = canonicalize_source_url(
        str(payload.get("source_url") or payload.get("url") or feed_url)
    )
    identity = article_url or "|".join(
        (str(payload.get("source") or ""), str(payload.get("title") or ""), str(published))
    )
    return {
        **dict(payload),
        **timestamps,
        "source_url": article_url,
        "canonical_url": article_url,
        "feed_url": canonical_feed,
        "source_identity": hashlib.sha256(identity.encode()).hexdigest(),
    }


def _required_time(value: datetime | str | None, name: str) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError(f"{name} is required")
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
