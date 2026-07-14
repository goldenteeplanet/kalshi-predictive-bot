import json
from datetime import datetime, timedelta

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.economic.actuals import (
    EconomicValueFetchResult,
    EconomicValueObservation,
    parse_trading_economics_calendar_values,
    run_phase3bd_r4_verified_consensus_source,
)
from kalshi_predictor.economic.repository import get_latest_economic_feature
from kalshi_predictor.utils.time import utc_now


def test_parse_trading_economics_values_captures_consensus_fields() -> None:
    observations = parse_trading_economics_calendar_values(
        [
            {
                "Country": "United States",
                "Category": "Jobs",
                "Event": "Non Farm Payrolls",
                "Date": "2026-07-03T12:30:00Z",
                "ActualValue": 172000,
                "ForecastValue": 175000,
                "PreviousValue": 179000,
                "TEForecastValue": 180000,
                "Unit": "",
                "Importance": 3,
                "Ticker": "NFP TCH",
            }
        ],
        request_url="https://api.tradingeconomics.com/calendar/country/united%20states",
    )

    assert len(observations) == 1
    observation = observations[0]
    assert observation.event_key == "jobs"
    assert observation.actual_value == "172000"
    assert observation.forecast_value == "175000"
    assert observation.previous_value == "179000"
    assert observation.raw_json["provider"] == "Trading Economics"


def test_phase3bd_r4_blocks_without_verified_consensus_source(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        payload = run_phase3bd_r4_verified_consensus_source(session)

    assert payload["summary"]["status"] == "BLOCKED_BY_MISSING_CONSENSUS_SOURCE"
    assert payload["summary"]["sources_attempted"] == 0
    assert payload["summary"]["value_observations_inserted"] == 0
    assert "TRADING_ECONOMICS_API_KEY" in payload["recommended_next_action"]


def test_phase3bd_r4_verified_file_requires_source_url_and_builds_signal(tmp_path) -> None:
    input_file = tmp_path / "verified_consensus.json"
    input_file.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "event_key": "cpi",
                        "event_time": "2026-07-01T12:30:00Z",
                        "title": "Core CPI year-over-year",
                        "source": "verified_vendor_export",
                        "source_url": "https://example.test/economic-calendar/core-cpi",
                        "actual_value": "3.2%",
                        "forecast_value": "3.0%",
                        "previous_value": "3.1%",
                    },
                    {
                        "event_key": "jobs",
                        "event_time": "2026-07-03T12:30:00Z",
                        "forecast_value": "113K",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        payload = run_phase3bd_r4_verified_consensus_source(
            session,
            input_file=input_file,
            kalshi_client=_FakeKalshiClient(),
            max_series=4,
            markets_per_series=5,
            snapshot_series_limit=1,
            forecast_limit=10,
            opportunity_limit=10,
            opportunity_output_path=tmp_path / "opportunities_economic_v1.md",
        )
        feature = get_latest_economic_feature(session, "cpi")

    assert payload["summary"]["status"] == "ACTIVE_WITH_VERIFIED_CONSENSUS"
    assert payload["summary"]["consensus_value_observations"] == 1
    assert payload["summary"]["actual_and_consensus_observations"] == 1
    assert payload["sources"][0]["error"].startswith("row 1: source_url is required")
    assert feature is not None
    assert feature.surprise_score is not None
    assert feature.confidence_score == "70"


def test_phase3bd_r4_refreshes_forecasts_from_verified_consensus(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        payload = run_phase3bd_r4_verified_consensus_source(
            session,
            value_fetcher=lambda: [
                EconomicValueFetchResult(
                    source="verified_fixture",
                    url="https://example.test/consensus",
                    attempted=True,
                    succeeded=True,
                    observations=[
                        _observation("cpi", now - timedelta(days=1), "3.4", "3.1", "3.2")
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
    assert payload["summary"]["actual_and_consensus_observations"] == 1
    assert payload["summary"]["value_observations_inserted"] == 1
    assert payload["summary"]["features_inserted"] >= 1
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


def _observation(
    event_key: str,
    event_time: datetime,
    actual_value: str,
    forecast_value: str,
    previous_value: str,
) -> EconomicValueObservation:
    return EconomicValueObservation(
        event_key=event_key,
        source="verified_fixture",
        source_url="https://example.test/consensus",
        event_time=event_time,
        category=event_key,
        title=f"{event_key.upper()} verified consensus value",
        actual_value=actual_value,
        forecast_value=forecast_value,
        previous_value=previous_value,
        raw_json={"fixture": True},
    )


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bd_r4.db'}")
    return get_session_factory(engine)
