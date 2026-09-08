"""Offline Synoptic shapes and credential containment; no real token or network."""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from kalshi_predictor.research.synoptic import SynopticError, SynopticResearchClient

TOKEN = "a1" * 16


def payload(service="latest"):
    clock = (datetime.now(UTC) - timedelta(minutes=2)).replace(second=0, microsecond=0).isoformat()
    station = dict(
        STID="KNYC", SENSOR_VARIABLES={"air_temp": {"air_temp_value_1": {"position": "2"}}}
    )
    if service == "latest":
        station["OBSERVATIONS"] = {
            "air_temp_value_1": {"value": 22.4, "date_time": clock, "qc": ["range_flag"]}
        }
    elif service == "timeseries":
        station["OBSERVATIONS"] = {"date_time": [clock], "air_temp_set_1": [22.4]}
        station["QC"] = {"air_temp_set_1": [["range_flag"]]}
    return dict(
        SUMMARY={"RESPONSE_CODE": 1, "NUMBER_OF_OBJECTS": 1},
        STATION=[station],
        UNITS={"air_temp": "Celsius"},
        QC_SUMMARY={"QC_CHECKS_APPLIED": ["sl_range_check"]},
    )


def client_for(body, **kwargs):
    return SynopticResearchClient(
        TOKEN, transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)), **kwargs
    )


@pytest.mark.parametrize("service", ["metadata", "latest", "timeseries"])
def test_request_scope_original_clocks_units_and_qc_preserved(service, caplog):
    body = payload(service)
    seen = []

    def handler(request):
        assert request.method == "GET" and request.url.host == "api.synopticdata.com"
        assert request.url.params["token"] == TOKEN
        assert request.url.params["stid"] == "KNYC"
        assert request.url.params["vars"] == "air_temp"
        assert "authorization" not in request.headers
        seen.append(request)
        logging.getLogger("httpcore.http11").debug("synthetic echoed header %s", TOKEN)
        return httpx.Response(200, json=body)

    with (
        caplog.at_level(logging.DEBUG),
        SynopticResearchClient(TOKEN, transport=httpx.MockTransport(handler)) as client,
    ):
        result = getattr(client, service)()
    assert TOKEN not in caplog.text
    assert TOKEN not in str(seen[0].url)
    assert "token=" not in result.original.url
    assert TOKEN not in repr(result)
    assert result.original.sha256 == hashlib.sha256(result.original.payload).hexdigest()
    assert json.loads(result.original.payload) == body
    assert result.original.provenance_role == "THIRD_PARTY_OBSERVATION_RESEARCH_ONLY"
    assert result.station["SENSOR_VARIABLES"] == body["STATION"][0]["SENSOR_VARIABLES"]
    if service != "metadata":
        row = result.observations[0]
        assert row.value_c == Decimal("22.4") and row.unit == "Celsius"
        assert row.qc == ["range_flag"]
        assert row.observed_at < result.original.received_at
        assert row.sensor_id.startswith("air_temp_")


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_http_failures_never_return_provider_body_or_credential(status):
    def handler(request):
        return httpx.Response(
            status, text=str(request.url), headers={"Location": "https://example.invalid/" + TOKEN}
        )

    with SynopticResearchClient(
        TOKEN, request_budget=1, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SynopticError) as failed:
            client.latest()
        assert TOKEN not in str(failed.value)
        assert failed.value.__cause__ is None
        with pytest.raises(SynopticError, match="QUOTA_EXHAUSTED"):
            client.latest()


def test_transport_exception_request_is_sanitized_before_escape(caplog):
    retained = []

    def handler(request):
        retained.append(request)
        raise httpx.ConnectError(str(request.url), request=request)

    with (
        caplog.at_level(logging.DEBUG),
        SynopticResearchClient(TOKEN, transport=httpx.MockTransport(handler)) as client,
    ):
        with pytest.raises(SynopticError, match="TRANSPORT_FAILED") as failed:
            client.latest()
    assert TOKEN not in caplog.text and TOKEN not in str(failed.value)
    assert "token=" not in str(retained[0].url)
    assert failed.value.__suppress_context__ is True


@pytest.mark.parametrize("escaped", [False, True])
def test_raw_or_json_escaped_token_echo_is_not_archived(escaped):
    raw = json.dumps(payload()).encode()
    echo = TOKEN if not escaped else "".join(f"\\u{ord(char):04x}" for char in TOKEN)
    raw = raw[:-1] + (',"echo":"' + echo + '"}').encode()
    with SynopticResearchClient(
        TOKEN,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, content=raw, headers={"content-type": "application/json"}
            )
        ),
    ) as client:
        with pytest.raises(SynopticError, match="CREDENTIAL_ECHO"):
            client.latest()


@pytest.mark.parametrize("change", ["station", "unit", "clock", "qc", "alignment"])
def test_malformed_observation_identity_or_alignment_fails_closed(change):
    body = payload("timeseries")
    station = body["STATION"][0]
    if change == "station":
        station["STID"] = "OTHER"
    elif change == "unit":
        body["UNITS"]["air_temp"] = "Fahrenheit"
    elif change == "clock":
        station["OBSERVATIONS"]["date_time"] = ["2026-09-08T00:00:00"]
    elif change == "qc":
        station["QC"]["air_temp_set_1"] = []
    else:
        station["OBSERVATIONS"]["air_temp_set_1"] = []
    with client_for(body) as client, pytest.raises(SynopticError):
        client.timeseries()


def test_window_and_credential_limits_are_local_and_no_auth_endpoint_exists():
    with pytest.raises(SynopticError, match="TOKEN_FORMAT"):
        SynopticResearchClient("Ky" + "x" * 38)
    with SynopticResearchClient(
        TOKEN, transport=httpx.MockTransport(lambda req: pytest.fail("unexpected request"))
    ) as client:
        with pytest.raises(SynopticError, match="WINDOW"):
            client.latest(within_minutes=121)
        with pytest.raises(SynopticError, match="WINDOW"):
            client.timeseries(recent_minutes=121)
        end = datetime.now(UTC).replace(second=0, microsecond=0)
        with pytest.raises(SynopticError, match="WINDOW"):
            client.timeseries(start=end - timedelta(hours=7), end=end)
        with pytest.raises(SynopticError, match="ENDPOINT"):
            client._get("auth", {})
        assert client.remaining_requests == 3


def test_explicit_utc_window_and_null_values_preserved():
    body = payload("timeseries")
    body["STATION"][0]["OBSERVATIONS"]["air_temp_set_1"] = [None]
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    with client_for(body) as client:
        result = client.timeseries(start=end - timedelta(hours=6), end=end)
    assert result.observations[0].value_c is None
    assert "recent=" not in result.original.url


def test_response_byte_limit_and_api_authentication_code():
    with SynopticResearchClient(
        TOKEN,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, content=b"x" * 1_048_577, headers={"content-type": "application/json"}
            )
        ),
    ) as client:
        with pytest.raises(SynopticError, match="TOO_LARGE"):
            client.latest()


def test_live_knyc_shape_preserves_absent_sensor_qc_without_inventing_pass():
    # Shape from the secret-free 2026-09-08 11:03 UTC public response; only the
    # observation clock is relative so this test has no wall-clock expiry.
    body = payload()
    station = body["STATION"][0]
    station.update(
        ID="5111",
        NAME="New York City, Central Park",
        MNET_ID="1",
        LATITUDE="40.78333",
        LONGITUDE="-73.96667",
        QC_FLAGGED=False,
        SENSOR_VARIABLES={"air_temp": {"air_temp_value_1": {}}},
    )
    station["OBSERVATIONS"]["air_temp_value_1"].pop("qc")
    station["OBSERVATIONS"]["air_temp_value_1"]["value"] = 17.2
    with client_for(body) as client:
        result = client.latest()
    assert result.observations[0].value_c == Decimal("17.2")
    assert result.observations[0].qc is None
    assert result.station["QC_FLAGGED"] is False
    assert result.qc_summary == body["QC_SUMMARY"]
    with client_for({"SUMMARY": {"RESPONSE_CODE": 200}}) as client:
        with pytest.raises(SynopticError, match="AUTHENTICATION_FAILED"):
            client.latest()
