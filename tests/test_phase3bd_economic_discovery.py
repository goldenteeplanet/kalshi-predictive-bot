from datetime import timedelta
from decimal import Decimal

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import EconomicMarketLink
from kalshi_predictor.economic.discovery import (
    economic_series_candidates,
    run_phase3bd_economic_market_discovery,
)
from kalshi_predictor.economic.features import build_economic_features
from kalshi_predictor.economic.linker import detect_economic_market, link_economic_markets
from kalshi_predictor.economic.repository import insert_economic_event
from kalshi_predictor.utils.time import utc_now


def test_economic_series_candidates_require_economics_category() -> None:
    candidates, counts = economic_series_candidates(
        {
            "series": [
                {
                    "ticker": "KXFED",
                    "title": "Fed interest rate decision",
                    "category": "Economics",
                    "tags": ["Rates"],
                },
                {
                    "ticker": "KXSPORTSFEDDE",
                    "title": "Erick Fedde strikeouts",
                    "category": "Sports",
                    "tags": ["MLB"],
                },
            ]
        },
        max_candidates=10,
    )

    assert counts["series_seen"] == 2
    assert counts["economics_series_seen"] == 1
    assert [candidate.series_ticker for candidate in candidates] == ["KXFED"]
    assert candidates[0].matched_event_category == "fed"


def test_economic_linker_uses_safe_series_hints_and_stays_idempotent(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_economic_events(session)
        upsert_market(
            session,
            {
                "ticker": "KXFED-26JUL",
                "series_ticker": "KXFED",
                "event_ticker": "KXFED-26JUL",
                "status": "open",
                "title": "Will the upper bound be above 4.5%?",
            },
        )
        upsert_market(
            session,
            {
                "ticker": "KXMLBFEDDE-26JUL",
                "series_ticker": "KXMLB",
                "event_ticker": "KXMLB-26JUL",
                "status": "open",
                "title": "Will Erick Fedde record 5+ strikeouts?",
            },
        )

        first = link_economic_markets(
            session,
            series_tickers=["KXFED"],
            series_category_hints={"KXFED": "fed"},
        )
        second = link_economic_markets(
            session,
            series_tickers=["KXFED"],
            series_category_hints={"KXFED": "fed"},
        )
        links = session.query(EconomicMarketLink).all()

    assert first.links_created == 1
    assert second.links_created == 0
    assert second.links_skipped_existing == 1
    assert [link.ticker for link in links] == ["KXFED-26JUL"]


def test_economic_detection_does_not_match_sports_fedde_false_positive(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXMLBFEDDE-26JUL",
                "series_ticker": "KXMLB",
                "event_ticker": "KXMLB-26JUL",
                "status": "open",
                "title": "Will Erick Fedde record 5+ strikeouts?",
            },
        )

        category, confidence, reason = detect_economic_market(market)

    assert category is None
    assert confidence == Decimal("0")
    assert reason == "No economic keyword match."


def test_phase3bd_runs_bounded_discovery_and_forecasts(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_economic_events(session)
        build_economic_features(session)

        payload = run_phase3bd_economic_market_discovery(
            session,
            client=_FakeKalshiClient(),
            max_series=4,
            markets_per_series=5,
            snapshot_series_limit=1,
            forecast_limit=10,
        )

    assert payload["summary"]["status"] == "ACTIVE"
    assert payload["summary"]["markets_synced"] == 1
    assert payload["summary"]["links_created"] == 1
    assert payload["summary"]["snapshots_captured"] == 1
    assert payload["summary"]["forecasts_inserted"] == 1


class _FakeKalshiClient:
    def get_series(self, limit=None):
        return {
            "series": [
                {
                    "ticker": "KXFED",
                    "title": "Fed interest rate decision",
                    "category": "Economics",
                    "tags": ["Rates"],
                    "frequency": "custom",
                }
            ]
        }

    def iter_markets(
        self,
        status="open",
        limit=100,
        max_pages=None,
        series_ticker=None,
        event_ticker=None,
        start_cursor=None,
        deadline_monotonic=None,
        page_callback=None,
    ):
        del status, limit, max_pages, event_ticker, start_cursor, deadline_monotonic, page_callback
        if series_ticker != "KXFED":
            return
        now = utc_now()
        yield {
            "ticker": "KXFED-26JUL-T4",
            "series_ticker": "KXFED",
            "event_ticker": "KXFED-26JUL",
            "status": "open",
            "title": "Will the Fed upper bound be above 4.5%?",
            "close_time": (now + timedelta(days=10)).isoformat(),
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.50",
            "volume_fp": "100",
            "open_interest_fp": "20",
            "liquidity_dollars": "1000",
        }

    def get_orderbook(self, ticker):
        assert ticker == "KXFED-26JUL-T4"
        return {"orderbook_fp": {"yes_dollars": [["0.40", "10"]], "no_dollars": [["0.50", "8"]]}}

    def close(self) -> None:
        return None


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bd_economic.db'}")
    return get_session_factory(engine)


def _seed_economic_events(session) -> None:
    now = utc_now()
    insert_economic_event(
        session,
        event_key="fed",
        source="test",
        event_time=now,
        category="fed",
        title="Federal Reserve Interest Rate Decision",
        actual_value="5.25",
        forecast_value="5.25",
        previous_value="5.25",
    )
