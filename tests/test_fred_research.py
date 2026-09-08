import hashlib
import logging

import httpx
import pytest

from kalshi_predictor.research.fred import FREDError, FREDResearchClient

KEY = "a" * 32
WINDOW = {"realtime_start": "2026-09-08", "realtime_end": "2026-09-08"}
ARGS = {
    "series_id": "DFF",
    "observation_start": "2026-09-01",
    "observation_end": "2026-09-08",
    **WINDOW,
}


def payload(value="4.3300"):
    return {**WINDOW, "observations": [{**WINDOW, "date": "2026-09-01", "value": value}]}


def test_original_vintage_and_logging(caplog):
    caplog.set_level(logging.DEBUG)
    seen = []

    def handler(request):
        assert request.url.params["api_key"] == KEY
        assert request.method == "GET"
        seen.append(request)
        return httpx.Response(200, json=payload())

    with FREDResearchClient(KEY, transport=httpx.MockTransport(handler)) as client:
        result = client.observations(**ARGS)
        assert KEY not in repr(client)
    assert KEY not in caplog.text
    assert KEY not in str(seen[0].url)
    assert KEY not in result.original.url
    assert result.original.sha256 == hashlib.sha256(result.original.payload).hexdigest()
    assert result.data["observations"][0]["value"] == "4.3300"
    assert result.data["observations"][0]["date"] != WINDOW["realtime_start"]
    assert result.original.research_only
    assert not result.original.runtime_certified
    assert not result.original.historical_availability_verified
    assert result.provider_timestamp is None


def test_release_metadata_not_publication():
    with FREDResearchClient(
        KEY,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"releases": [{**WINDOW, "id": 10, "name": "Employment Situation"}]}
            )
        ),
    ) as client:
        result = client.series_release(series_id="UNRATE", **WINDOW)
    assert result.provider_timestamp is None
    assert result.data["releases"][0]["id"] == 10


@pytest.mark.parametrize("status", [400, 401, 403, 429, 302])
def test_halts_no_retry_or_secret(status):
    calls = []
    with FREDResearchClient(
        KEY,
        transport=httpx.MockTransport(
            lambda r: (
                calls.append(r)
                or httpx.Response(status, text=KEY, headers={"location": "https://x.test"})
            )
        ),
    ) as client:
        with pytest.raises(FREDError) as error:
            client.observations(**ARGS)
        assert KEY not in str(error.value)
        with pytest.raises(FREDError, match="HALTED"):
            client.observations(**ARGS)
    assert len(calls) == 1


def test_transport_error_has_no_request_context(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request):
        raise httpx.ReadTimeout(str(request.url), request=request)

    with FREDResearchClient(KEY, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FREDError, match="TRANSPORT_FAILED") as error:
            client.observations(**ARGS)
    assert error.value.__context__ is None
    assert KEY not in caplog.text


@pytest.mark.parametrize("value", [".", "123.000000000000001"])
def test_missing_and_precise_value(value):
    with FREDResearchClient(
        KEY, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload(value)))
    ) as client:
        assert client.observations(**ARGS).data["observations"][0]["value"] == value


@pytest.mark.parametrize(
    "body",
    [
        b'{"observations":[],"observations":[]}',
        b'{"x":NaN}',
        b"broken",
        b'{"observations":[{"value":"NaN"}]}',
        ('{"x":"' + KEY + '"}').encode(),
        ('{"x":"' + "\\u0061" * 32 + '"}').encode(),
        b" " * 1_048_577,
    ],
    ids=["duplicate", "nan", "invalid", "bad-row", "echo", "escaped-echo", "oversize"],
)
def test_bad_originals(body):
    with FREDResearchClient(
        KEY,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=body, headers={"content-type": "application/json"}
            )
        ),
    ) as client:
        with pytest.raises(FREDError):
            client.observations(**ARGS)


@pytest.mark.parametrize(
    "overrides",
    [
        {"series_id": "PRIVATE"},
        {"observation_start": "2026-99-01"},
        {"realtime_end": "2026-01-01"},
        {"limit": 1001},
        {"limit": True},
    ],
)
def test_rejected_before_http(overrides):
    def handler(request):
        pytest.fail("unexpected HTTP")

    with FREDResearchClient(KEY, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FREDError):
            client.observations(**(ARGS | overrides))
        assert client.remaining_requests == 10


def test_budget():
    with FREDResearchClient(
        KEY,
        request_budget=1,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload())),
    ) as client:
        client.observations(**ARGS)
        with pytest.raises(FREDError, match="LOCAL_QUOTA"):
            client.observations(**ARGS)


@pytest.mark.parametrize("series_id", ["DFF", "CPIAUCSL", "UNRATE", "CPILFESL", "GDPC1", "PAYEMS"])
def test_alfred_exact_vintage_not_today(series_id):
    vintage = "2025-09-08"
    data = {
        "observations": [
            {
                "date": "2025-08-01",
                "value": "300.123",
                "realtime_start": vintage,
                "realtime_end": vintage,
            }
        ]
    }

    def handler(request):
        assert request.url.params["realtime_start"] == vintage
        assert request.url.params["realtime_end"] == vintage
        assert request.url.params["series_id"] == series_id
        return httpx.Response(200, json=data)

    with FREDResearchClient(KEY, transport=httpx.MockTransport(handler)) as client:
        result = client.observations_as_of(
            series_id=series_id,
            observation_start="2025-08-01",
            observation_end="2025-08-31",
            as_of=vintage,
        )
    assert result.original.received_at.date().isoformat() > vintage
    assert result.data == data
    assert result.provider_timestamp is None
    assert not result.original.historical_availability_verified


def test_future_vintage_rejected_before_http():
    def handler(request):
        pytest.fail("unexpected HTTP")

    with FREDResearchClient(KEY, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FREDError, match="FUTURE_VINTAGE"):
            client.observations_as_of(
                series_id="DFF",
                observation_start="2025-01-01",
                observation_end="2025-01-02",
                as_of="9999-12-31",
            )
