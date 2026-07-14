from datetime import datetime, timedelta

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.economic.actuals import (
    EconomicValueFetchResult,
    EconomicValueObservation,
    parse_bls_value_response,
    parse_fred_csv_observation,
    run_phase3bd_r3_economic_value_capture,
)
from kalshi_predictor.economic.features import build_economic_features
from kalshi_predictor.economic.repository import (
    get_latest_economic_feature,
    insert_economic_event,
)
from kalshi_predictor.utils.time import utc_now


def test_parse_bls_value_response_computes_cpi_and_payroll_actuals() -> None:
    payload = {
        "Results": {
            "series": [
                {
                    "seriesID": "CUUR0000SA0L1E",
                    "data": [
                        {"year": "2026", "period": "M05", "value": "336.846"},
                        {"year": "2026", "period": "M04", "value": "335.803"},
                        {"year": "2025", "period": "M05", "value": "323.520"},
                        {"year": "2025", "period": "M04", "value": "321.100"},
                    ],
                },
                {
                    "seriesID": "CES0000000001",
                    "data": [
                        {"year": "2026", "period": "M05", "value": "159001"},
                        {"year": "2026", "period": "M04", "value": "158829"},
                        {"year": "2026", "period": "M03", "value": "158650"},
                    ],
                },
                {
                    "seriesID": "LNS14000000",
                    "data": [
                        {"year": "2026", "period": "M05", "value": "4.3"},
                    ],
                },
            ]
        }
    }

    observations = parse_bls_value_response(payload)
    by_key = {observation.event_key: observation for observation in observations}

    assert by_key["cpi"].actual_value == "4.12"
    assert by_key["cpi"].forecast_value is None
    assert by_key["jobs"].actual_value == "172"
    assert by_key["jobs"].previous_value == "179"
    assert by_key["jobs"].raw_json["unemployment_rate"] == "4.3"


def test_parse_fred_csv_observation_extracts_latest_and_previous_values() -> None:
    csv_text = """observation_date,DFEDTARU
2026-06-29,3.75
2026-06-30,3.75
2026-07-01,4.00
"""

    observation = parse_fred_csv_observation(
        csv_text,
        source="fred_fed_target_upper",
        source_url="https://fred.stlouisfed.org/series/DFEDTARU",
        series_id="DFEDTARU",
        event_key="fed",
        category="fed",
        title="Federal funds target range upper limit",
    )

    assert observation is not None
    assert observation.event_key == "fed"
    assert observation.actual_value == "4.00"
    assert observation.previous_value == "3.75"
    assert observation.forecast_value is None


def test_latest_economic_feature_prefers_actual_signal_over_future_calendar_placeholder(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        insert_economic_event(
            session,
            event_key="cpi",
            source="official_actual_fixture",
            event_time=now - timedelta(days=30),
            category="cpi",
            title="Latest official CPI actual",
            actual_value="3.4",
            previous_value="3.2",
        )
        insert_economic_event(
            session,
            event_key="cpi",
            source="calendar_fixture",
            event_time=now + timedelta(days=14),
            category="cpi",
            title="Upcoming CPI release",
        )
        build_economic_features(session)

        feature = get_latest_economic_feature(session, "cpi")

    assert feature is not None
    assert feature.surprise_score is not None
    assert feature.confidence_score == "70"


def test_phase3bd_r3_value_capture_refreshes_features_and_rankings(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        payload = run_phase3bd_r3_economic_value_capture(
            session,
            value_fetcher=lambda: [
                EconomicValueFetchResult(
                    source="fixture",
                    url="https://example.test/values",
                    attempted=True,
                    succeeded=True,
                    observations=[
                        _observation("cpi", now - timedelta(days=1), "3.4", "3.2"),
                        _observation("fed", now - timedelta(days=2), "4.00", "3.75"),
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
    assert payload["summary"]["value_observations_seen"] == 2
    assert payload["summary"]["value_observations_inserted"] == 2
    assert payload["summary"]["actual_value_observations"] == 2
    assert payload["summary"]["consensus_value_observations"] == 0
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
    previous_value: str,
) -> EconomicValueObservation:
    return EconomicValueObservation(
        event_key=event_key,
        source="fixture",
        source_url="https://example.test/values",
        event_time=event_time,
        category=event_key,
        title=f"{event_key.upper()} official value",
        actual_value=actual_value,
        forecast_value=None,
        previous_value=previous_value,
        raw_json={"fixture": True},
    )


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bd_r3.db'}")
    return get_session_factory(engine)
