from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import is_inactive_market_status
from kalshi_predictor.data.schema import Market
from kalshi_predictor.utils.time import utc_now

EXACT_EVENT_AND_SERIES_CATALOG = "EXACT_EVENT_AND_SERIES_CATALOG"
AUTHORITATIVE_IDENTITY_VERIFIED = "AUTHORITATIVE_IDENTITY_VERIFIED"


@dataclass(frozen=True)
class WeatherIdentityArtifacts:
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True)
class _CacheEntry:
    payload: dict[str, Any]
    fetched_at: datetime
    source_sha256: str


class BoundedProtocolCache:
    """Small invocation-scoped cache for exact public protocol responses."""

    def __init__(self, *, max_entries: int, max_age: timedelta) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        self.max_entries = max_entries
        self.max_age = max_age
        self._entries: dict[tuple[str, str], _CacheEntry] = {}

    def get(self, kind: str, identity: str, *, now: datetime) -> dict[str, Any] | None:
        entry = self._entries.get((kind, identity))
        if entry is None:
            return None
        if _as_utc(now) - entry.fetched_at > self.max_age:
            del self._entries[(kind, identity)]
            return None
        return dict(entry.payload)

    def put(
        self,
        kind: str,
        identity: str,
        payload: Mapping[str, Any],
        *,
        fetched_at: datetime,
    ) -> None:
        key = (kind, identity)
        normalized = dict(payload)
        source_sha256 = canonical_source_sha256(normalized)
        existing = self._entries.get(key)
        if existing is not None and existing.source_sha256 != source_sha256:
            raise RuntimeError(f"PROTOCOL_SOURCE_HASH_DRIFT:{kind}:{identity}")
        if key not in self._entries and len(self._entries) >= self.max_entries:
            oldest = min(self._entries, key=lambda item: self._entries[item].fetched_at)
            del self._entries[oldest]
        self._entries[key] = _CacheEntry(
            payload=normalized,
            fetched_at=_as_utc(fetched_at),
            source_sha256=source_sha256,
        )


def canonical_source_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def collect_weather_identity_evidence(
    session: Session,
    client: Any,
    *,
    tickers: list[str],
    deadline_monotonic: float,
    max_age: timedelta,
    cache: BoundedProtocolCache,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated_at = _as_utc(now or utc_now())
    rows = [
        _collect_row(
            session,
            client,
            ticker=ticker,
            deadline_monotonic=deadline_monotonic,
            max_age=max_age,
            cache=cache,
            now=generated_at,
        )
        for ticker in tickers
    ]
    return {
        "generated_at": generated_at.isoformat(),
        "mode": "SHADOW_ONLY_AUTHORITATIVE_WEATHER_IDENTITY",
        "tickers": list(tickers),
        "rows": rows,
        "summary": {
            "requested": len(tickers),
            "authoritative_identity_verified": sum(
                bool(row["authoritative_identity_verified"]) for row in rows
            ),
            "blocked": sum(not bool(row["authoritative_identity_verified"]) for row in rows),
            "reasons": _reason_counts(rows),
        },
        "safety": {
            "diagnostic_only": True,
            "database_open_mode": "sqlite_mode_ro_query_only",
            "database_writes": 0,
            "public_get_endpoints_only": True,
            "portfolio_or_order_apis_imported": False,
            "ticker_or_text_derivation": False,
            "kalshi_url_verified_changed": False,
            "candidate_selection_changed": False,
            "paper_readiness_changed": False,
            "historical_rankings_rewritten": False,
            "thresholds_changed": False,
            "orders_created": 0,
        },
    }


def write_weather_identity_evidence(
    payload: dict[str, Any], *, output_dir: Path
) -> WeatherIdentityArtifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "weather_identity_evidence.json"
    markdown_path = output_dir / "weather_identity_evidence.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return WeatherIdentityArtifacts(json_path=json_path, markdown_path=markdown_path)


def _collect_row(
    session: Session,
    client: Any,
    *,
    ticker: str,
    deadline_monotonic: float,
    max_age: timedelta,
    cache: BoundedProtocolCache,
    now: datetime,
) -> dict[str, Any]:
    base = _blocked_row(ticker, "PROTOCOL_EVIDENCE_MISSING", now=now, max_age=max_age)
    local = session.get(Market, ticker)
    if local is None:
        return {**base, "reason": "LOCAL_MARKET_MISSING"}
    if is_inactive_market_status(local.status):
        return {**base, "reason": "LOCAL_MARKET_INACTIVE"}
    try:
        market = _exact_payload(
            client,
            cache,
            kind="market",
            identity=ticker,
            fetch=lambda: client.get_market(ticker),
            now=now,
            deadline_monotonic=deadline_monotonic,
        )
    except (RuntimeError, TimeoutError) as exc:
        return {**base, "reason": str(exc)}
    if _clean(market.get("ticker")) != ticker:
        return {**base, "reason": "MARKET_TICKER_MISMATCH"}
    if is_inactive_market_status(market.get("status")):
        return {**base, "reason": "CATALOG_MARKET_INACTIVE"}
    event_ticker = _clean(market.get("event_ticker"))
    if event_ticker is None:
        return {**base, "reason": "MARKET_EVENT_MISSING"}
    if local.event_ticker not in (None, event_ticker):
        return {**base, "reason": "LOCAL_EVENT_CONFLICT", "event_ticker": event_ticker}
    try:
        event_payload = _exact_payload(
            client,
            cache,
            kind="event",
            identity=event_ticker,
            fetch=lambda: client.get_event(event_ticker),
            now=now,
            deadline_monotonic=deadline_monotonic,
        )
    except (RuntimeError, TimeoutError) as exc:
        return {**base, "reason": str(exc), "event_ticker": event_ticker}
    event = event_payload.get("event")
    if not isinstance(event, Mapping):
        return {**base, "reason": "EVENT_EVIDENCE_MALFORMED", "event_ticker": event_ticker}
    if _clean(event.get("event_ticker")) != event_ticker:
        return {**base, "reason": "EVENT_TICKER_MISMATCH", "event_ticker": event_ticker}
    series_ticker = _clean(event.get("series_ticker"))
    if series_ticker is None:
        return {**base, "reason": "EVENT_SERIES_MISSING", "event_ticker": event_ticker}
    explicit_market_series = _clean(market.get("series_ticker"))
    if explicit_market_series not in (None, series_ticker):
        return {
            **base,
            "reason": "MARKET_EVENT_SERIES_CONFLICT",
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
        }
    if local.series_ticker not in (None, series_ticker):
        return {
            **base,
            "reason": "LOCAL_SERIES_CONFLICT",
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
        }
    try:
        series_payload = _exact_payload(
            client,
            cache,
            kind="series",
            identity=series_ticker,
            fetch=lambda: client.get_series_by_ticker(series_ticker),
            now=now,
            deadline_monotonic=deadline_monotonic,
        )
    except (RuntimeError, TimeoutError) as exc:
        return {
            **base,
            "reason": str(exc),
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
        }
    series = series_payload.get("series")
    if not isinstance(series, Mapping):
        return {
            **base,
            "reason": "SERIES_EVIDENCE_MALFORMED",
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
        }
    if _clean(series.get("ticker")) != series_ticker:
        return {
            **base,
            "reason": "SERIES_TICKER_MISMATCH",
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
        }
    return {
        **base,
        "authoritative_identity_verified": True,
        "status": AUTHORITATIVE_IDENTITY_VERIFIED,
        "reason": AUTHORITATIVE_IDENTITY_VERIFIED,
        "evidence_class": EXACT_EVENT_AND_SERIES_CATALOG,
        "event_ticker": event_ticker,
        "series_ticker": series_ticker,
        "source_identity": {
            "market_ticker": ticker,
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
        },
        "source_sha256": {
            "market": canonical_source_sha256(market),
            "event": canonical_source_sha256(event_payload),
            "series": canonical_source_sha256(series_payload),
        },
    }


def _exact_payload(
    client: Any,
    cache: BoundedProtocolCache,
    *,
    kind: str,
    identity: str,
    fetch: Any,
    now: datetime,
    deadline_monotonic: float,
) -> dict[str, Any]:
    if time.monotonic() >= deadline_monotonic:
        raise TimeoutError("PROTOCOL_DEADLINE_EXPIRED")
    cached = cache.get(kind, identity, now=now)
    if cached is not None:
        return cached
    payload = fetch()
    if time.monotonic() > deadline_monotonic:
        raise TimeoutError("PROTOCOL_DEADLINE_EXPIRED")
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"{kind.upper()}_EVIDENCE_MALFORMED")
    cache.put(kind, identity, payload, fetched_at=now)
    return dict(payload)


def _blocked_row(
    ticker: str, reason: str, *, now: datetime, max_age: timedelta
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "authoritative_identity_verified": False,
        "kalshi_url_verified": None,
        "status": "AUTHORITATIVE_IDENTITY_BLOCKED",
        "reason": reason,
        "evidence_class": None,
        "event_ticker": None,
        "series_ticker": None,
        "source_identity": None,
        "source_sha256": None,
        "fetched_at": now.isoformat(),
        "freshness_status": "FRESH",
        "max_age_seconds": int(max_age.total_seconds()),
        "diagnostic_only": True,
    }


def _reason_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        reason = str(row["reason"])
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Authoritative Weather Identity Shadow Evidence",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        "- Diagnostic only: `true`",
        "- Candidate selection changed: `false`",
        "- Paper readiness changed: `false`",
        "- Database writes: `0`",
        "",
        "| Ticker | Verified | Evidence | Reason | Event | Series |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        "| {ticker} | {verified} | {evidence} | {reason} | {event} | {series} |".format(
            ticker=row["ticker"],
            verified=str(row["authoritative_identity_verified"]).lower(),
            evidence=row.get("evidence_class") or "n/a",
            reason=row["reason"],
            event=row.get("event_ticker") or "n/a",
            series=row.get("series_ticker") or "n/a",
        )
        for row in payload["rows"]
    )
    return "\n".join(lines) + "\n"


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
