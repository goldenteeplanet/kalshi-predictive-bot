import hashlib
import json
import logging

import httpx
import pytest

from kalshi_predictor.research.bls import BLSError, BLSResearchClient, series_metadata

KEY = "a" * 32
ARGS = {"series_id": "CUUR0000SA0", "year": 2025}


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_numeric_failure_status_without_response_or_retry(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=KEY)

    with BLSResearchClient(KEY, request_budget=1, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BLSError) as failure:
            client.observations(**ARGS)
        assert failure.value.http_status == status
        assert vars(failure.value) == {"http_status": status}
        assert KEY not in repr(failure.value)
        assert failure.value.__context__ is None
        with pytest.raises(BLSError):
            client.observations(**ARGS)
        assert len(calls) == 1


def test_no_status_invented_for_transport_or_format_failure():
    assert BLSError("BLS_KEY_FORMAT_INVALID").http_status is None

    def handler(request):
        raise httpx.ConnectError(KEY, request=request)

    with BLSResearchClient(KEY, request_budget=1, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BLSError, match="BLS_TRANSPORT_FAILED") as failure:
            client.observations(**ARGS)
        assert failure.value.http_status is None
        assert failure.value.__context__ is None
        assert KEY not in repr(failure.value)


def payload(value="123.0001"):
    return {
        "status": "REQUEST_SUCCEEDED",
        "message": [],
        "Results": {
            "series": [
                {
                    "seriesID": ARGS["series_id"],
                    "data": [
                        {
                            "year": "2025",
                            "period": "M08",
                            "periodName": "August",
                            "value": value,
                            "footnotes": [{"code": "P", "text": "Preliminary."}],
                        }
                    ],
                }
            ]
        },
    }


def test_fixed_request_original_and_logs(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request):
        assert str(request.url) == "https://api.bls.gov/publicAPI/v2/timeseries/data/"
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "seriesid": [ARGS["series_id"]],
            "startyear": "2025",
            "endyear": "2025",
            "registrationkey": KEY,
        }
        return httpx.Response(200, json=payload())

    with BLSResearchClient(KEY, transport=httpx.MockTransport(handler)) as client:
        result = client.observations(**ARGS)
        assert KEY not in repr(client)
    assert KEY not in caplog.text
    assert KEY not in repr(result)
    assert result.original.sha256 == hashlib.sha256(result.original.payload).hexdigest()
    assert result.original.received_at.utcoffset().total_seconds() == 0
    assert result.original.research_only
    assert not result.original.runtime_certified
    assert not result.original.historical_availability_verified
    assert result.provider_timestamp is None
    assert result.data == payload()


@pytest.mark.parametrize("value", ["-", "(S)", ".", "123.00000000001"])
def test_symbols_precision_and_footnotes(value):
    with BLSResearchClient(
        KEY, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload(value)))
    ) as client:
        result = client.observations(**ARGS)
    row = result.data["Results"]["series"][0]["data"][0]
    assert row["value"] == value
    assert row["footnotes"] == [{"code": "P", "text": "Preliminary."}]


def test_documented_results_list():
    data = payload()
    data["Results"] = [data["Results"]]
    with BLSResearchClient(
        KEY, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))
    ) as client:
        assert isinstance(client.observations(**ARGS).data["Results"], list)


@pytest.mark.parametrize("status", [400, 401, 403, 429, 302])
def test_http_halts_no_retry(status):
    calls = []
    with BLSResearchClient(
        KEY,
        transport=httpx.MockTransport(
            lambda r: (
                calls.append(r)
                or httpx.Response(status, text=KEY, headers={"location": "https://other.test"})
            )
        ),
    ) as client:
        with pytest.raises(BLSError) as error:
            client.observations(**ARGS)
        assert KEY not in str(error.value)
        with pytest.raises(BLSError, match="HALTED"):
            client.observations(**ARGS)
    assert len(calls) == 1


def test_semantic_failure_on_200():
    with BLSResearchClient(
        KEY,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"status": "REQUEST_NOT_PROCESSED", "message": ["invalid registration"]}
            )
        ),
    ) as client:
        with pytest.raises(BLSError, match="SEMANTIC_REQUEST_FAILED"):
            client.observations(**ARGS)
        with pytest.raises(BLSError, match="HALTED"):
            client.observations(**ARGS)


def test_transport_failure_no_retained_context(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request):
        raise httpx.ReadTimeout(request.content.decode(), request=request)

    with BLSResearchClient(KEY, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BLSError, match="TRANSPORT_FAILED") as error:
            client.observations(**ARGS)
    assert error.value.__context__ is None
    assert KEY not in str(error.value)
    assert KEY not in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        b'{"status":1,"status":2}',
        b'{"x":NaN}',
        b"broken",
        ('{"x":"' + KEY + '"}').encode(),
        ('{"x":"' + "\\u0061" * 32 + '"}').encode(),
        b" " * 1_048_577,
    ],
    ids=["duplicate", "nan", "invalid", "echo", "escaped-echo", "oversize"],
)
def test_bad_bodies(body):
    with BLSResearchClient(
        KEY,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=body, headers={"content-type": "application/json"}
            )
        ),
    ) as client:
        with pytest.raises(BLSError):
            client.observations(**ARGS)


@pytest.mark.parametrize("change", ["series", "year", "period", "duplicate", "empty", "notes"])
def test_response_binding(change):
    data = payload()
    item = data["Results"]["series"][0]
    if change == "series":
        item["seriesID"] = "LNS14000000"
    elif change == "year":
        item["data"][0]["year"] = "2024"
    elif change == "period":
        item["data"][0]["period"] = "Q01"
    elif change == "duplicate":
        item["data"] *= 2
    elif change == "empty":
        item["data"] = []
    else:
        item["data"][0]["footnotes"] = None
    with BLSResearchClient(
        KEY, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))
    ) as client:
        with pytest.raises(BLSError):
            client.observations(**ARGS)


@pytest.mark.parametrize(
    "args",
    [
        {"series_id": "PRIVATE", "year": 2025},
        {"series_id": "CUUR0000SA0", "year": True},
        {"series_id": "CUUR0000SA0", "year": 9999},
    ],
)
def test_arguments_no_http(args):
    def handler(request):
        pytest.fail("unexpected HTTP")

    with BLSResearchClient(KEY, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BLSError):
            client.observations(**args)
        assert client.remaining_requests == 10


def test_budget_and_close():
    with BLSResearchClient(
        KEY,
        request_budget=1,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload())),
    ) as client:
        client.observations(**ARGS)
        with pytest.raises(BLSError, match="LOCAL_QUOTA"):
            client.observations(**ARGS)
    with pytest.raises(BLSError, match="CLOSED"):
        client.observations(**ARGS)


@pytest.mark.parametrize(
    "series_id",
    [
        "CUUR0000SA0",
        "CUSR0000SA0",
        "CUSR0000SA0L1E",
        "LNS14000000",
        "CES0000000001",
        "CES0500000003",
        "WPSFD4",
    ],
)
def test_verified_series_and_catalog(series_id):
    metadata = series_metadata(series_id)
    assert metadata.title
    assert "bls.gov/" in metadata.official_source
    assert metadata.release_identity is None
    assert metadata.provider_timestamp is None
    data = payload()
    item = data["Results"]["series"][0]
    item["seriesID"] = series_id
    item["catalog"] = {"series_id": series_id, "series_title": metadata.title}

    def handler(request):
        assert json.loads(request.content)["catalog"] is True
        return httpx.Response(200, json=data)

    with BLSResearchClient(KEY, transport=httpx.MockTransport(handler)) as client:
        result = client.observations(series_id=series_id, year=2025, catalog=True)
    assert result.data["Results"]["series"][0]["catalog"] == item["catalog"]


@pytest.mark.parametrize("catalog", [None, {}, {"series_id": "WRONG", "series_title": "Wrong"}])
def test_catalog_required_and_bound(catalog):
    data = payload()
    data["Results"]["series"][0]["catalog"] = catalog
    with BLSResearchClient(
        KEY, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))
    ) as client:
        with pytest.raises(BLSError, match="CATALOG_MISSING_OR_IDENTITY"):
            client.observations(**ARGS, catalog=True)
