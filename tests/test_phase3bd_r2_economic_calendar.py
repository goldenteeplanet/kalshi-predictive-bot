from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.economic.calendar import (
    EconomicCalendarEvent,
    EconomicCalendarFetchResult,
    parse_bea_release_schedule,
    parse_bls_release_schedule,
    parse_fed_fomc_calendar,
    run_phase3bd_r2_economic_calendar_freshness,
    select_current_calendar_events,
)
from kalshi_predictor.utils.time import utc_now


def test_parse_bls_release_schedule_extracts_eastern_release_time() -> None:
    html = """
    <table class="release-list">
      <tr><td>June 2026</td><td>Jul. 14, 2026</td><td>08:30 AM</td></tr>
    </table>
    """

    events = parse_bls_release_schedule(
        html,
        source="bls_cpi_schedule",
        source_url="https://www.bls.gov/schedule/news_release/cpi.htm",
        event_key="cpi",
        category="cpi",
        release_title="Consumer Price Index",
    )

    assert len(events) == 1
    assert events[0].event_key == "cpi"
    assert events[0].event_time.isoformat() == "2026-07-14T12:30:00+00:00"
    assert events[0].title == "Consumer Price Index for June 2026"


def test_parse_bea_release_schedule_extracts_gdp_rows() -> None:
    html = """
    <tr class="scheduled-releases-type-press">
      <td class="scheduled-date"><div class="release-date">July 30</div>
      <small class="text-muted">8:30 AM</small></td>
      <td class="release-title views-field views-field-field-scheduled-releases-type">
        Gross Domestic Product, 2nd Quarter 2026 (Advance Estimate)
      </td>
    </tr>
    """

    events = parse_bea_release_schedule(
        html,
        source_url="https://www.bea.gov/news/schedule/full",
    )

    assert len(events) == 1
    assert events[0].event_key == "gdp"
    assert "Gross Domestic Product" in events[0].title


def test_parse_fed_fomc_calendar_uses_final_meeting_day() -> None:
    html = """
    <h4><a id="42828">2026 FOMC Meetings</a></h4>
    <div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>July</strong></div>
      <div class="fomc-meeting__date">28-29*</div>
    </div>
    """

    events = parse_fed_fomc_calendar(
        html,
        source_url="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    )

    assert len(events) == 1
    assert events[0].event_key == "fed"
    assert events[0].event_time.isoformat() == "2026-07-29T18:00:00+00:00"


def test_select_current_calendar_events_prefers_nearest_upcoming() -> None:
    now = datetime(2026, 7, 1, 12, tzinfo=ZoneInfo("UTC"))
    events = [
        _event("cpi", now - timedelta(days=10)),
        _event("cpi", now + timedelta(days=20)),
        _event("cpi", now + timedelta(days=40)),
        _event("jobs", now + timedelta(days=1)),
    ]

    selected = select_current_calendar_events(events, now=now)

    assert [(event.event_key, event.event_time) for event in selected] == [
        ("cpi", now + timedelta(days=20)),
        ("jobs", now + timedelta(days=1)),
    ]


def test_phase3bd_r2_refreshes_calendar_features_and_economic_rankings(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        payload = run_phase3bd_r2_economic_calendar_freshness(
            session,
            calendar_fetcher=lambda: [
                EconomicCalendarFetchResult(
                    source="fixture",
                    url="https://example.test/calendar",
                    attempted=True,
                    succeeded=True,
                    events=[
                        _event("cpi", now + timedelta(days=7)),
                        _event("jobs", now + timedelta(days=1)),
                        _event("fed", now + timedelta(days=30)),
                        _event("gdp", now + timedelta(days=45)),
                    ],
                )
            ],
            kalshi_client=_FakeKalshiClient(),
            max_series=4,
            markets_per_series=5,
            snapshot_series_limit=1,
            forecast_limit=10,
            opportunity_limit=10,
            opportunity_output_path=tmp_path / "opportunities_economic_v1.md",
        )

    assert payload["summary"]["sources_succeeded"] == 1
    assert payload["summary"]["selected_current_events"] == 4
    assert payload["summary"]["events_inserted"] == 4
    assert payload["summary"]["forecasts_inserted"] == 1
    assert payload["summary"]["rankings_inserted"] == 1


class _FakeKalshiClient:
    def get_series(self, limit=None):
        return {
            "series": [
                {
                    "ticker": "KXCPICORE",
                    "title": "US Core CPI inflation",
                    "category": "Economics",
                    "tags": ["Inflation"],
                    "frequency": "monthly",
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
        if series_ticker != "KXCPICORE":
            return
        now = utc_now()
        yield {
            "ticker": "KXCPICORE-26JUL-T3.0",
            "series_ticker": "KXCPICORE",
            "event_ticker": "KXCPICORE-26JUL",
            "status": "open",
            "title": "Will Core CPI inflation be above 3.0%?",
            "close_time": (now + timedelta(days=10)).isoformat(),
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.50",
            "volume_fp": "100",
            "open_interest_fp": "20",
            "liquidity_dollars": "1000",
        }

    def get_orderbook(self, ticker):
        del ticker
        return {}

    def close(self):
        return None


def _event(event_key: str, event_time):
    return EconomicCalendarEvent(
        event_key=event_key,
        source="fixture",
        source_url="https://example.test/calendar",
        event_time=event_time,
        category=event_key,
        title=f"{event_key.upper()} release",
        raw_json={"fixture": True},
    )


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bd_r2.db'}")
    return get_session_factory(engine)
