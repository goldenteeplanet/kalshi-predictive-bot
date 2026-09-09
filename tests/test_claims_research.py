import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

import httpx
import pytest

from kalshi_predictor.economic.claims_research import ClaimsSourceError, build_initial_claims_panel
from kalshi_predictor.economic.source_research import (
    EconomicSourceError,
    build_economic_source_features,
)
from kalshi_predictor.research.fred import (
    FREDError,
    FREDOriginal,
    FREDResearchClient,
    FREDResponse,
)

RECEIPT = datetime(2026, 9, 9, 8, tzinfo=UTC)
PARAMS = {
    "series_id": "ICSA",
    "file_type": "json",
    "observation_start": "2026-08-22",
    "observation_end": "2026-08-29",
    "realtime_start": "2026-08-22",
    "realtime_end": "2026-09-08",
    "limit": 100,
    "sort_order": "asc",
    "output_type": 4,
    "units": "lin",
    "offset": 0,
}
ARGS = {
    "observation_start": "2026-08-22",
    "observation_end": "2026-08-29",
    "as_of": "2026-09-08",
}


def body():
    return {
        **{
            key: PARAMS[key]
            for key in (
                "observation_start",
                "observation_end",
                "realtime_start",
                "realtime_end",
                "limit",
                "output_type",
                "units",
                "offset",
            )
        },
        "count": 2,
        "observations": [
            {
                "date": "2026-08-22",
                "value": "220000",
                "realtime_start": "2026-08-27",
                "realtime_end": "9999-12-31",
            },
            {
                "date": "2026-08-29",
                "value": "218000.0",
                "realtime_start": "2026-09-03",
                "realtime_end": "9999-12-31",
            },
        ],
    }


def response(data=None, params=None):
    data = body() if data is None else data
    raw = json.dumps(data).encode()
    return FREDResponse(
        "observations",
        "ICSA",
        FREDOriginal(
            "https://api.stlouisfed.org/fred/series/observations?"
            + urlencode(PARAMS if params is None else params),
            RECEIPT,
            hashlib.sha256(raw).hexdigest(),
            raw,
        ),
        data,
    )


def test_saturday_persons_and_receipt_are_not_publication():
    panel = build_initial_claims_panel(response(), decision_at=RECEIPT)
    assert [row.week_ending for row in panel.observations] == [date(2026, 8, 22), date(2026, 8, 29)]
    assert panel.observations[1].persons == Decimal("218000")
    assert panel.observations[1].raw_value == "218000.0"
    assert panel.units == "persons" and panel.seasonal_adjustment == "SA"
    assert panel.missing_weeks == ()
    assert panel.received_at == RECEIPT and panel.as_of == date(2026, 9, 8)
    assert all(row.publication_at is None for row in panel.observations)
    assert not panel.historical_availability_verified
    assert not panel.dol_first_release_verified and not panel.runtime_certified


def test_missing_week_and_missing_symbol_preserved():
    data = body()
    data["observations"] = data["observations"][:1]
    data["count"] = 1
    panel = build_initial_claims_panel(response(data), decision_at=RECEIPT)
    assert panel.missing_weeks == (date(2026, 8, 29),)
    data["observations"][0]["value"] = "."
    panel = build_initial_claims_panel(response(data), decision_at=RECEIPT)
    assert panel.observations[0].persons is None
    assert panel.missing_weeks == (date(2026, 8, 22), date(2026, 8, 29))


@pytest.mark.parametrize("value", ["-1", "1.5", "NaN", "Infinity", "1e20", "oops", 220000])
def test_invalid_counts(value):
    data = body()
    data["observations"][0]["value"] = value
    with pytest.raises(ClaimsSourceError):
        build_initial_claims_panel(response(data), decision_at=RECEIPT)


@pytest.mark.parametrize(
    "change",
    [
        {"date": "2026-08-23"},
        {"date": "2026-08-15"},
        {"realtime_start": "2026-09-10"},
        {"realtime_start": "2026-08-21"},
        {"realtime_end": "2026-08-01"},
    ],
)
def test_wrong_week_or_release_window(change):
    data = body()
    data["observations"][0].update(change)
    with pytest.raises(ClaimsSourceError):
        build_initial_claims_panel(response(data), decision_at=RECEIPT)


def test_duplicate_or_reversed_weeks():
    for observations in ([body()["observations"][0]] * 2, body()["observations"][::-1]):
        data = body() | {"observations": observations}
        with pytest.raises(ClaimsSourceError, match="ORDER_OR_DUPLICATE"):
            build_initial_claims_panel(response(data), decision_at=RECEIPT)


@pytest.mark.parametrize(
    "change",
    [
        {"output_type": 1},
        {"output_type": 2},
        {"output_type": 3},
        {"output_type": "4"},
        {"units": "pch"},
        {"count": 3},
        {"count": True},
        {"offset": 1},
        {"limit": 101},
        {"realtime_end": "2026-09-09"},
        {"observation_end": "2026-09-05"},
    ],
)
def test_schema_mode_or_truncation(change):
    with pytest.raises(ClaimsSourceError):
        build_initial_claims_panel(response(body() | change), decision_at=RECEIPT)


@pytest.mark.parametrize(
    "change",
    [
        {"series_id": "UNRATE"},
        {"output_type": 1},
        {"units": "pch"},
        {"vintage_dates": "2026-09-08"},
        {"api_key": "secret"},
        {"realtime_start": "2026-09-08"},
        {"limit": 1001},
    ],
)
def test_exact_query_required(change):
    with pytest.raises(ClaimsSourceError):
        build_initial_claims_panel(response(params=PARAMS | change), decision_at=RECEIPT)


def test_hash_body_identity_and_no_backdated_availability():
    original = response()
    bad_hash = replace(original.original, sha256="0" * 64)
    with pytest.raises(ClaimsSourceError, match="HASH"):
        build_initial_claims_panel(replace(original, original=bad_hash), decision_at=RECEIPT)
    with pytest.raises(ClaimsSourceError, match="BODY_MISMATCH"):
        build_initial_claims_panel(replace(original, data={}), decision_at=RECEIPT)
    with pytest.raises(ClaimsSourceError, match="SERIES"):
        build_initial_claims_panel(replace(original, series_id="UNRATE"), decision_at=RECEIPT)
    with pytest.raises(ClaimsSourceError, match="NOT_RECEIVED"):
        build_initial_claims_panel(original, decision_at=RECEIPT - timedelta(days=2))
    with pytest.raises(ClaimsSourceError, match="CLOCK"):
        build_initial_claims_panel(original, decision_at=RECEIPT.replace(tzinfo=None))


def test_initial_release_not_accepted_by_legacy_bridge():
    with pytest.raises(EconomicSourceError):
        build_economic_source_features(response(), decision_at=RECEIPT)


def test_dedicated_capture_fixed_query_and_no_secret(caplog):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.params["api_key"] == "a" * 32
        assert request.url.params["series_id"] == "ICSA"
        assert request.url.params["output_type"] == "4"
        return httpx.Response(200, json=body())

    with FREDResearchClient("a" * 32, transport=httpx.MockTransport(handler)) as client:
        result = client.initial_claims_releases(**ARGS)
    assert len(calls) == 1
    assert "a" * 32 not in result.original.url + caplog.text
    assert "a" * 32 not in str(calls[0].url)
    assert result.original.received_at.date() >= date(2026, 9, 8)


def test_old_capture_does_not_admit_icsa():
    def handler(request):
        pytest.fail("ICSA must not enter output1 path")

    with FREDResearchClient("a" * 32, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FREDError, match="SERIES_NOT_ALLOWED"):
            client.observations_as_of(series_id="ICSA", **ARGS)


def test_capture_preserves_original_before_panel_schema_rejection():
    data = body() | {"output_type": 1}
    with FREDResearchClient(
        "a" * 32,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data)),
    ) as client:
        captured = client.initial_claims_releases(**ARGS)
    assert json.loads(captured.original.payload) == data
    with pytest.raises(ClaimsSourceError, match="INITIAL_RELEASE_REQUIRED"):
        build_initial_claims_panel(captured, decision_at=captured.original.received_at)


@pytest.mark.parametrize(
    "change",
    [
        {"limit": True},
        {"limit": 1001},
        {"as_of": "9999-01-01"},
        {"observation_start": "2026-09-09"},
        {"observation_start": "1900-01-01"},
    ],
)
def test_capture_rejects_invalid_window_before_http(change):
    def handler(request):
        pytest.fail("invalid request must not reach HTTP")

    with FREDResearchClient("a" * 32, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FREDError):
            client.initial_claims_releases(**(ARGS | change))


def test_claims_429_halts_without_retry():
    calls = []
    with FREDResearchClient(
        "a" * 32,
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(429)),
    ) as client:
        with pytest.raises(FREDError, match="RATE_OR_QUOTA"):
            client.initial_claims_releases(**ARGS)
        with pytest.raises(FREDError, match="HALTED"):
            client.initial_claims_releases(**ARGS)
    assert len(calls) == 1
