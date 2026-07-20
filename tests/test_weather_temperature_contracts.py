from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from kalshi_predictor.weather.station_observations import (
    StationObservation,
    align_point_observation,
    fetch_nws_station_observations,
    parse_nws_station_observations,
)
from kalshi_predictor.weather.temperature_contracts import (
    parse_point_temperature_ticker,
    validate_point_temperature_market,
)


def test_parse_exact_new_york_hourly_threshold() -> None:
    parsed = parse_point_temperature_ticker("KXTEMPNYCH-26JUL1523-T80.99")
    assert parsed is not None
    assert parsed.station_id == "KNYC"
    assert parsed.settlement_source == "the_weather_company"
    assert parsed.contract_kind == "ABOVE"
    assert parsed.raw_strike == Decimal("80.99")
    assert parsed.discrete_threshold_f == Decimal("81")
    assert parsed.target_local_time == datetime(
        2026, 7, 15, 23, tzinfo=ZoneInfo("America/New_York")
    )
    assert parsed.target_utc_time == datetime(2026, 7, 16, 3, tzinfo=ZoneInfo("UTC"))


def test_bucket_requires_authoritative_market_metadata() -> None:
    parsed = parse_point_temperature_ticker("KXTEMPNYCH-26JUL1523-B80.5")
    assert parsed is not None
    assert parsed.contract_kind == "BUCKET_METADATA_REQUIRED"
    assert parsed.discrete_threshold_f is None


def test_parser_rejects_fuzzy_or_invalid_tickers() -> None:
    assert parse_point_temperature_ticker("KXTEMPCHI-26JUL1523-T80.99") is None
    assert parse_point_temperature_ticker("KXTEMPNYCH-26JUL1524-T80.99") is None
    assert parse_point_temperature_ticker("KXTEMPNYCH-26JUL1523-X80.99") is None
    assert parse_point_temperature_ticker("KXTEMPNYCH-26FEB3023-T80.99") is None


def _payload() -> dict:
    return {"features": [
        {"properties": {"station": "https://api.weather.gov/stations/KNYC",
                         "timestamp": "2026-07-16T02:51:00+00:00",
                         "temperature": {"unitCode": "wmoUnit:degC", "value": 25}}},
        {"properties": {"station": "https://api.weather.gov/stations/KLGA",
                         "timestamp": "2026-07-16T02:51:00+00:00",
                         "temperature": {"unitCode": "wmoUnit:degC", "value": 30}}},
        {"properties": {"station": "https://api.weather.gov/stations/KNYC",
                         "timestamp": "2026-07-16T04:01:00+00:00",
                         "temperature": {"unitCode": "wmoUnit:degC", "value": 24}}},
    ]}


def test_observations_require_exact_station_and_local_date() -> None:
    rows = parse_nws_station_observations(
        _payload(), station_id="KNYC", target_local_date=date(2026, 7, 15),
        timezone="America/New_York",
    )
    assert len(rows) == 1
    assert rows[0].temperature_f == Decimal("77")
    assert rows[0].source == "noaa_nws_observation_non_settlement_evidence"


def test_fetch_uses_exact_station_and_local_day_bounds() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["start"] = request.url.params["start"]
        observed["end"] = request.url.params["end"]
        return httpx.Response(200, json=_payload())

    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.weather.gov"
    ) as client:
        rows = fetch_nws_station_observations(
            station_id="KNYC", target_local_date=date(2026, 7, 15),
            timezone="America/New_York", user_agent="test@example.com", client=client,
        )
    assert len(rows) == 1
    assert observed["path"] == "/stations/KNYC/observations"
    assert str(observed["start"]).startswith("2026-07-15T04:00:00")
    assert str(observed["end"]).startswith("2026-07-16T03:59:59")


def _market(**overrides: object) -> dict[str, object]:
    market: dict[str, object] = {
        "ticker": "KXTEMPNYCH-26JUL1523-T80.99",
        "series_ticker": "KXTEMPNYCH",
        "event_ticker": "KXTEMPNYCH-26JUL1523",
        "strike_type": "greater",
        "floor_strike": 80.99,
        "cap_strike": None,
        "close_time": "2026-07-16T03:00:00Z",
        "rules_primary": (
            "If the temperature recorded at Central Park, New York City for Jul 15, 2026 "
            "11 PM EDT as reported by The Weather Company (for coordinates KNYC), is above "
            "80.99 degrees, then the market resolves to Yes."
        ),
    }
    market.update(overrides)
    return market


def test_market_metadata_validation_passes_exact_public_contract() -> None:
    contract = parse_point_temperature_ticker("KXTEMPNYCH-26JUL1523-T80.99")
    assert contract is not None
    result = validate_point_temperature_market(contract, _market())
    assert result.passed is True
    assert result.blockers == ()


def test_market_metadata_accepts_exact_query_scope_when_response_omits_series() -> None:
    contract = parse_point_temperature_ticker("KXTEMPNYCH-26JUL1523-T80.99")
    assert contract is not None
    market = _market(series_ticker=None)
    result = validate_point_temperature_market(
        contract, market, series_scope="KXTEMPNYCH"
    )
    assert result.passed is True


def test_market_metadata_validation_reports_each_truth_mismatch() -> None:
    contract = parse_point_temperature_ticker("KXTEMPNYCH-26JUL1523-T80.99")
    assert contract is not None
    result = validate_point_temperature_market(
        contract,
        _market(
            strike_type="less", floor_strike=81, cap_strike=82,
            close_time="2026-07-16T04:00:00Z", rules_primary="another source and station",
        ),
    )
    assert set(result.blockers) == {
        "TARGET_TIME_MISMATCH", "SETTLEMENT_SOURCE_MISMATCH", "STATION_MISMATCH",
        "STRIKE_TYPE_MISMATCH", "FLOOR_STRIKE_MISMATCH", "UNEXPECTED_CAP_STRIKE",
    }


def _observation(timestamp: str, station: str = "KNYC") -> StationObservation:
    observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return StationObservation(
        station_id=station,
        source="noaa_nws_observation_non_settlement_evidence",
        observed_at=observed_at,
        local_date=observed_at.astimezone(ZoneInfo("America/New_York")).date(),
        temperature_f=Decimal("77"),
        raw_json={},
    )


def test_alignment_selects_nearest_exact_station_observation() -> None:
    contract = parse_point_temperature_ticker("KXTEMPNYCH-26JUL1523-T80.99")
    assert contract is not None
    validation = validate_point_temperature_market(contract, _market())
    result = align_point_observation(
        validation,
        [_observation("2026-07-16T02:51:00Z"), _observation("2026-07-16T03:04:00Z")],
        tolerance_minutes=10,
    )
    assert result.passed is True
    assert result.offset_seconds == 240
    assert result.observation is not None
    assert result.observation.observed_at == datetime(
        2026, 7, 16, 3, 4, tzinfo=ZoneInfo("UTC")
    )


def test_alignment_rejects_wrong_station_and_outside_tolerance() -> None:
    contract = parse_point_temperature_ticker("KXTEMPNYCH-26JUL1523-T80.99")
    assert contract is not None
    validation = validate_point_temperature_market(contract, _market())
    result = align_point_observation(
        validation,
        [_observation("2026-07-16T03:01:00Z", "KLGA"), _observation("2026-07-16T03:16:00Z")],
        tolerance_minutes=15,
    )
    assert result.blocker == "NO_EXACT_POINT_OBSERVATION_WITHIN_TOLERANCE"


def test_alignment_refuses_unverified_market_metadata() -> None:
    contract = parse_point_temperature_ticker("KXTEMPNYCH-26JUL1523-T80.99")
    assert contract is not None
    validation = validate_point_temperature_market(contract, _market(strike_type="less"))
    result = align_point_observation(validation, [_observation("2026-07-16T03:00:00Z")])
    assert result.blocker == "MARKET_METADATA_NOT_VERIFIED"


def test_alignment_accepts_exact_prior_date_observation_across_midnight() -> None:
    contract = parse_point_temperature_ticker("KXTEMPNYCH-26JUL1600-T80.99")
    assert contract is not None
    market = _market(
        ticker=contract.ticker,
        event_ticker="KXTEMPNYCH-26JUL1600",
        close_time="2026-07-16T04:00:00Z",
    )
    validation = validate_point_temperature_market(contract, market)
    result = align_point_observation(
        validation, [_observation("2026-07-16T03:51:00Z")], tolerance_minutes=15
    )
    assert result.passed is True
    assert result.offset_seconds == 540
