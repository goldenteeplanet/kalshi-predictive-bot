from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot, upsert_market
from kalshi_predictor.data.schema import Market, MarketSnapshot
from kalshi_predictor.phase3ba_r3 import _first_weather_paper_blocker
from kalshi_predictor.weather_identity_evidence import (
    AUTHORITATIVE_IDENTITY_VERIFIED,
    EXACT_EVENT_AND_SERIES_CATALOG,
    BoundedProtocolCache,
    collect_weather_identity_evidence,
)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
TICKER = "KXTEMPNYCH-26AUG1012-T84.99"
EVENT = "KXTEMPNYCH-26AUG1012"
SERIES = "KXTEMPNYCH"


class FakeClient:
    def __init__(self) -> None:
        self.calls = {"market": 0, "event": 0, "series": 0}
        self.market = {
            "ticker": TICKER,
            "event_ticker": EVENT,
            "status": "open",
        }
        self.event = {"event": {"event_ticker": EVENT, "series_ticker": SERIES}}
        self.series = {"series": {"ticker": SERIES}}

    def get_market(self, ticker: str) -> dict:
        self.calls["market"] += 1
        assert ticker == TICKER
        return dict(self.market)

    def get_event(self, event_ticker: str) -> dict:
        self.calls["event"] += 1
        assert event_ticker == EVENT
        return json.loads(json.dumps(self.event))

    def get_series_by_ticker(self, series_ticker: str) -> dict:
        self.calls["series"] += 1
        assert series_ticker == SERIES
        return json.loads(json.dumps(self.series))


def _session(tmp_path, *, event_ticker: str | None = EVENT, series_ticker: str | None = None):
    engine = init_db(f"sqlite:///{tmp_path / 'identity.db'}")
    factory = get_session_factory(engine)
    session = factory()
    upsert_market(
        session,
        {
            "ticker": TICKER,
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
            "title": "NYC temperature",
            "status": "open",
        },
    )
    session.commit()
    return engine, session


def _collect(session, client, *, cache=None):
    max_age = timedelta(minutes=15)
    return collect_weather_identity_evidence(
        session,
        client,
        tickers=[TICKER],
        deadline_monotonic=time.monotonic() + 30,
        max_age=max_age,
        cache=cache or BoundedProtocolCache(max_entries=8, max_age=max_age),
        now=NOW,
    )


def test_exact_market_event_and_series_produce_shadow_evidence(tmp_path) -> None:
    engine, session = _session(tmp_path)
    try:
        payload = _collect(session, FakeClient())
    finally:
        session.close()
        engine.dispose()

    row = payload["rows"][0]
    assert row["authoritative_identity_verified"] is True
    assert row["status"] == AUTHORITATIVE_IDENTITY_VERIFIED
    assert row["evidence_class"] == EXACT_EVENT_AND_SERIES_CATALOG
    assert row["source_identity"] == {
        "market_ticker": TICKER,
        "event_ticker": EVENT,
        "series_ticker": SERIES,
    }
    assert set(row["source_sha256"]) == {"market", "event", "series"}
    assert all(len(value) == 64 for value in row["source_sha256"].values())
    assert row["kalshi_url_verified"] is None
    assert payload["safety"]["database_writes"] == 0


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        (("market", "ticker", "WRONG"), "MARKET_TICKER_MISMATCH"),
        (("market", "status", "closed"), "CATALOG_MARKET_INACTIVE"),
        (("market", "event_ticker", None), "MARKET_EVENT_MISSING"),
        (("event.event", "event_ticker", "WRONG"), "EVENT_TICKER_MISMATCH"),
        (("event.event", "series_ticker", None), "EVENT_SERIES_MISSING"),
        (("series.series", "ticker", "WRONG"), "SERIES_TICKER_MISMATCH"),
    ],
)
def test_missing_malformed_inactive_and_mismatched_evidence_fail_closed(
    tmp_path, patch, reason
) -> None:
    client = FakeClient()
    target, key, value = patch
    if target == "market":
        client.market[key] = value
    elif target == "event.event":
        client.event["event"][key] = value
    else:
        client.series["series"][key] = value
    engine, session = _session(tmp_path)
    try:
        row = _collect(session, client)["rows"][0]
    finally:
        session.close()
        engine.dispose()
    assert row["authoritative_identity_verified"] is False
    assert row["reason"] == reason


def test_local_conflict_fails_closed_and_no_text_derivation_occurs(tmp_path) -> None:
    engine, session = _session(tmp_path, event_ticker=EVENT, series_ticker="OTHER")
    try:
        row = _collect(session, FakeClient())["rows"][0]
    finally:
        session.close()
        engine.dispose()
    assert row["authoritative_identity_verified"] is False
    assert row["reason"] == "LOCAL_SERIES_CONFLICT"
    assert row["series_ticker"] == SERIES


def test_cache_is_bounded_expires_and_rejects_source_hash_drift() -> None:
    cache = BoundedProtocolCache(max_entries=1, max_age=timedelta(seconds=1))
    cache.put("event", EVENT, {"event": {"event_ticker": EVENT}}, fetched_at=NOW)
    assert cache.get("event", EVENT, now=NOW) is not None
    assert cache.get("event", EVENT, now=NOW + timedelta(seconds=2)) is None

    cache.put("event", EVENT, {"event": {"event_ticker": EVENT}}, fetched_at=NOW)
    with pytest.raises(RuntimeError, match="PROTOCOL_SOURCE_HASH_DRIFT"):
        cache.put(
            "event",
            EVENT,
            {"event": {"event_ticker": EVENT, "series_ticker": SERIES}},
            fetched_at=NOW,
        )


def test_shared_event_and_series_evidence_is_cached_per_invocation(tmp_path) -> None:
    engine, session = _session(tmp_path)
    client = FakeClient()
    cache = BoundedProtocolCache(max_entries=8, max_age=timedelta(minutes=15))
    try:
        _collect(session, client, cache=cache)
        _collect(session, client, cache=cache)
    finally:
        session.close()
        engine.dispose()
    assert client.calls == {"market": 1, "event": 1, "series": 1}


def test_partial_snapshot_preserves_authoritative_identity_in_raw_json(tmp_path) -> None:
    engine, session = _session(tmp_path, event_ticker=EVENT, series_ticker=SERIES)
    try:
        insert_market_snapshot(
            session,
            {"ticker": TICKER, "status": "open", "yes_bid_dollars": "0.40"},
            None,
            NOW,
        )
        session.commit()
        market = session.get(Market, TICKER)
        snapshot = session.scalar(select(MarketSnapshot).where(MarketSnapshot.ticker == TICKER))
        assert market is not None and snapshot is not None
        assert market.event_ticker == EVENT
        assert market.series_ticker == SERIES
        raw = json.loads(snapshot.raw_market_json)
        assert raw["event_ticker"] == EVENT
        assert raw["series_ticker"] == SERIES
    finally:
        session.close()
        engine.dispose()


def test_shadow_fields_cannot_change_weather_selection_or_blocker() -> None:
    row = {
        "current_window_eligible": True,
        "verified_kalshi_url": False,
        "kalshi_url_status": "BUILT_FROM_EXACT_CATALOG",
    }
    before = _first_weather_paper_blocker(row)
    row.update(
        {
            "authoritative_identity_verified": True,
            "authoritative_identity_evidence": EXACT_EVENT_AND_SERIES_CATALOG,
        }
    )
    assert _first_weather_paper_blocker(row) == before
    assert before == "LINK_EXACT_CATALOG_URL_UNCONFIRMED"
