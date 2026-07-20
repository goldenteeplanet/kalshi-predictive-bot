import json
from pathlib import Path

import httpx

from kalshi_predictor.phase_nyc_w3 import write_nyc_w3_report


def test_nyc_w3_generates_bounded_read_only_alignment_report(tmp_path: Path) -> None:
    markets = [{
        "ticker": "KXTEMPNYCH-26JUL1523-T80.99",
        "series_ticker": "KXTEMPNYCH",
        "event_ticker": "KXTEMPNYCH-26JUL1523",
        "status": "open",
        "strike_type": "greater",
        "floor_strike": 80.99,
        "cap_strike": None,
        "close_time": "2026-07-16T03:00:00Z",
        "rules_primary": "The Weather Company coordinates KNYC",
    }]
    observations = {"features": [{"properties": {
        "station": "https://api.weather.gov/stations/KNYC",
        "timestamp": "2026-07-16T02:55:00Z",
        "temperature": {"unitCode": "wmoUnit:degC", "value": 25},
    }}]}

    def kalshi_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "5"
        assert request.url.params["series_ticker"] == "KXTEMPNYCH"
        return httpx.Response(200, json={"markets": markets})

    def nws_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/stations/KNYC/observations"
        return httpx.Response(200, json=observations)

    with (
        httpx.Client(
            transport=httpx.MockTransport(kalshi_handler), base_url="https://kalshi.test"
        ) as kalshi_client,
        httpx.Client(
            transport=httpx.MockTransport(nws_handler), base_url="https://api.weather.gov"
        ) as nws_client,
    ):
        path = write_nyc_w3_report(
            output_dir=tmp_path, user_agent="test@example.com", market_limit=5,
            tolerance_minutes=10, kalshi_client=kalshi_client, nws_client=nws_client,
        )
    report = json.loads(path.read_text())
    assert report["database_writes"] == 0
    assert report["execution_enabled"] is False
    assert report["weather_v2_connected"] is False
    assert report["summary"]["metadata_passed"] == 1
    assert report["summary"]["alignment_passed"] == 1
    assert report["summary"]["minimum_offset_seconds"] == 300


def test_nyc_w3_reports_parse_and_metadata_failures(tmp_path: Path) -> None:
    markets = [
        {"ticker": "NOT-WEATHER", "event_ticker": "KXTEMPNYCH-26JUL1523"},
        {
            "ticker": "KXTEMPNYCH-26JUL1523-T80.99",
            "event_ticker": "KXTEMPNYCH-26JUL1523",
            "strike_type": "less", "floor_strike": 80.99, "cap_strike": None,
            "close_time": "2026-07-16T03:00:00Z",
            "rules_primary": "The Weather Company coordinates KNYC",
        },
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"markets": markets})

    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://kalshi.test"
    ) as kalshi_client:
        path = write_nyc_w3_report(
            output_dir=tmp_path, user_agent="test@example.com",
            kalshi_client=kalshi_client,
        )
    report = json.loads(path.read_text())
    assert report["summary"]["metadata_failed"] == 2
    assert report["summary"]["metadata_blocker_counts"] == {
        "STRIKE_TYPE_MISMATCH": 1, "TICKER_PARSE_FAILED": 1,
    }
    assert report["summary"]["alignment_passed"] == 0


def test_nyc_w3_can_certify_an_exact_rolled_ticker(tmp_path: Path) -> None:
    market = {
        "ticker": "KXTEMPNYCH-26JUL1523-T80.99",
        "event_ticker": "KXTEMPNYCH-26JUL1523",
        "status": "closed", "strike_type": "greater", "floor_strike": 80.99,
        "cap_strike": None, "close_time": "2026-07-16T03:00:00Z",
        "rules_primary": "The Weather Company coordinates KNYC",
    }

    def kalshi_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/markets/KXTEMPNYCH-26JUL1523-T80.99")
        return httpx.Response(200, json={"market": market})

    def nws_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"features": [{"properties": {
            "station": "https://api.weather.gov/stations/KNYC",
            "timestamp": "2026-07-16T02:51:00Z",
            "temperature": {"unitCode": "wmoUnit:degC", "value": 25},
        }}]})

    with (
        httpx.Client(
            transport=httpx.MockTransport(kalshi_handler), base_url="https://kalshi.test"
        ) as kalshi_client,
        httpx.Client(
            transport=httpx.MockTransport(nws_handler), base_url="https://nws.test"
        ) as nws_client,
    ):
        path = write_nyc_w3_report(
            output_dir=tmp_path, user_agent="test@example.com",
            exact_tickers=["KXTEMPNYCH-26JUL1523-T80.99"],
            kalshi_client=kalshi_client, nws_client=nws_client,
        )
    report = json.loads(path.read_text())
    assert report["exact_ticker_certification"] is True
    assert report["summary"]["metadata_passed"] == 1
    assert report["summary"]["alignment_passed"] == 1
