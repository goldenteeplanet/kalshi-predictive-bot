"""Read-only TestClient coverage of saved provider evidence and app integration."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kalshi_predictor.ui import provider_research


def save(directory, *, provider="unusual-whales", age=5, error=None):
    at = datetime.now(UTC) - timedelta(seconds=age)
    name = at.strftime("%Y%m%dT%H%M%S%fZ") + "-" + provider
    sample = dict(
        asset_id="123",
        market="<script>alert('x')</script>",
        category="Weather",
        resolves="2026-09-09T00:00:00Z",
    )
    body = {"data": {"data": [{**sample, "top_insiders": [{"wallet": "PRIVATE_WALLET"}]}]}}
    raw = json.dumps(body).encode()
    summary = dict(
        provider=provider,
        started_at=at.isoformat(),
        completed_at=at.isoformat(),
        received_at=at.isoformat(),
        research_only=True,
        runtime_certified=False,
        state="CAPTURED",
        record_count=1,
        samples=[sample],
        provider_timestamp=None,
        source_url="https://api.unusualwhales.com/api/predictions/unusual?limit=5&offset=0",
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )
    if error:
        summary.update(state="PROVIDER_ERROR", error_code=error)
    else:
        (directory / (name + ".original.json")).write_bytes(raw)
    path = directory / (name + ".summary.json")
    path.write_text(json.dumps(summary), encoding="utf-8")
    return path, summary


def client(directory):
    app = FastAPI()
    app.include_router(provider_research.create_router(directory))
    return TestClient(app)


def test_verified_read_only_api_and_escaped_html_exclude_personal_fields(tmp_path):
    save(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with client(tmp_path) as api:
        response = api.get("/api/research/providers")
        page = api.get("/research/providers")
    assert response.status_code == page.status_code == 200
    row = response.json()["providers"][0]
    assert row["state"] == "RECORDED_RESEARCH"
    assert row["current_freshness"] == "UNKNOWN" and row["receipt_age_seconds"] >= 5
    assert row["provider_timestamp"] is None
    assert "PRIVATE_WALLET" not in response.text + page.text
    assert "top_insiders" not in response.text + page.text
    assert "<script>alert" not in page.text and "&lt;script&gt;" in page.text
    assert "not live readings" in page.text
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_newest_error_overrides_prior_success_and_missing_provider_is_explicit(tmp_path):
    save(tmp_path, age=10)
    save(tmp_path, age=2, error="UW_TRANSPORT_FAILED")
    rows = provider_research.snapshot(tmp_path)["providers"]
    assert rows[0]["state"] == "PROVIDER_ERROR" and rows[0]["error_code"] == "UW_TRANSPORT_FAILED"
    assert rows[0]["samples"] == []
    assert rows[1]["state"] == rows[2]["state"] == "NO_CAPTURE"


@pytest.mark.parametrize(
    "change", ["hash", "host", "credential_query", "sample", "oversize", "certified"]
)
def test_malformed_latest_capture_fails_closed_without_fallback(tmp_path, change):
    save(tmp_path, age=20)
    path, summary = save(tmp_path, age=2)
    if change == "hash":
        summary["source_sha256"] = "0" * 64
    elif change == "host":
        summary["source_url"] = "https://example.invalid/api/predictions/unusual?limit=5"
    elif change == "credential_query":
        summary["source_url"] += "&token=DO_NOT_EXPORT"
    elif change == "sample":
        summary["samples"][0]["asset_id"] = "different"
    elif change == "certified":
        summary["runtime_certified"] = True
    else:
        path.write_bytes(b"x" * 65_537)
    if change != "oversize":
        path.write_text(json.dumps(summary), encoding="utf-8")
    view = provider_research.snapshot(tmp_path)
    assert view["providers"][0]["state"] == "CAPTURE_INVALID"
    assert "DO_NOT_EXPORT" not in json.dumps(view)


def test_file_count_limit_and_no_arbitrary_filename_or_key_route(tmp_path):
    for i in range(129):
        (tmp_path / f"ignored-{i}").write_text("unused")
    assert all(
        p["state"] == "CAPTURE_DIRECTORY_UNAVAILABLE"
        for p in provider_research.snapshot(tmp_path)["providers"]
    )
    with client(tmp_path) as api:
        assert api.get("/api/research/providers/secret.txt").status_code == 404
        assert api.post("/api/research/providers", json={"key": "NEVER_USED"}).status_code == 405


def test_synoptic_receipt_and_original_observation_time_remain_separate(tmp_path):
    at = datetime.now(UTC) - timedelta(seconds=3)
    clock = at - timedelta(minutes=40)
    sample = dict(
        sensor_id="air_temp_value_1",
        observed_at=clock.isoformat(),
        value_c="17.2",
        unit="Celsius",
        qc={"status": "passed"},
    )
    body = dict(
        STATION=[
            dict(
                STID="KNYC",
                OBSERVATIONS={
                    "air_temp_value_1": dict(
                        date_time=clock.isoformat(), value=17.2, qc={"status": "passed"}
                    )
                },
            )
        ],
        UNITS={"air_temp": "Celsius"},
    )
    raw = json.dumps(body).encode()
    prefix = tmp_path / (at.strftime("%Y%m%dT%H%M%S%fZ") + "-synoptic")
    summary = dict(
        provider="synoptic",
        started_at=at.isoformat(),
        completed_at=at.isoformat(),
        received_at=at.isoformat(),
        research_only=True,
        runtime_certified=False,
        state="CAPTURED",
        record_count=1,
        samples=[sample],
        source_url="https://api.synopticdata.com/v2/stations/latest?stid=KNYC&vars=air_temp&output=json&sensorvars=1&obtimezone=UTC&units=temp%7CC&qc=on&qc_flags=on&qc_remove_data=off&within=120",
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )
    prefix.with_suffix(".original.json").write_bytes(raw)
    prefix.with_suffix(".summary.json").write_text(json.dumps(summary), encoding="utf-8")
    row = provider_research.snapshot(tmp_path)["providers"][1]
    assert row["state"] == "RECORDED_RESEARCH"
    assert row["received_at"] == at.isoformat()
    assert row["samples"][0]["observed_at"] == clock.isoformat()
    assert row["samples"][0]["sensor_qc"] == "PROVIDER_QC_REPORTED"
    assert row["current_freshness"] == "UNKNOWN"


def test_existing_app_registers_read_only_research_routes():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from kalshi_predictor.config import Settings
    from kalshi_predictor.ui.app import create_app

    engine = create_engine("sqlite+pysqlite:///:memory:")
    app = create_app(settings=Settings(), session_factory=sessionmaker(engine))
    with TestClient(app) as api:
        response = api.get("/api/research/providers")
        assert response.status_code == 200
        states = {row["provider"]: row["state"] for row in response.json()["providers"]}
        assert set(states) == {"unusual-whales", "synoptic", "oddpool", "fred", "bls"}
        assert api.get("/research/providers").status_code == 200
        assert api.get("/data-sources").status_code == 200
        assert api.get("/api/data-sources").json()["research_only"] is True
        assert api.post("/api/research/providers").status_code == 405
    engine.dispose()


@pytest.mark.parametrize("provider,list_shape", [("fred", False), ("bls", False), ("bls", True)])
def test_economic_provider_original_binding_and_availability_limits(tmp_path, provider, list_shape):
    at = datetime.now(UTC) - timedelta(seconds=2)
    day = at.date().isoformat()
    if provider == "fred":
        rows = [dict(date=day, value="3.62", realtime_start=day, realtime_end=day)]
        body = dict(observations=rows, count=1)
        url = (
            "https://api.stlouisfed.org/fred/series/observations?series_id=DFF&file_type=json"
            "&limit=5&sort_order=asc&observation_start="
            + day
            + "&observation_end="
            + day
            + "&realtime_start="
            + day
            + "&realtime_end="
            + day
        )
    else:
        rows = [
            dict(
                year=str(at.year),
                period="M07",
                periodName="July",
                value="333.918",
                footnotes=[{"code": "P", "text": "Preliminary"}],
            )
        ]
        result = {"series": [{"seriesID": "CUUR0000SA0", "data": rows}]}
        body = dict(status="REQUEST_SUCCEEDED", Results=[result] if list_shape else result)
        url = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    raw = json.dumps(body).encode()
    prefix = tmp_path / (at.strftime("%Y%m%dT%H%M%S%fZ") + "-" + provider)
    summary = dict(
        provider=provider,
        started_at=at.isoformat(),
        completed_at=at.isoformat(),
        received_at=at.isoformat(),
        research_only=True,
        runtime_certified=False,
        state="CAPTURED",
        record_count=1,
        samples=rows,
        total_available=1,
        publication_time_verified=False,
        historical_availability_verified=False,
        source_url=url,
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )
    prefix.with_suffix(".original.json").write_bytes(raw)
    path = prefix.with_suffix(".summary.json")
    path.write_text(json.dumps(summary), encoding="utf-8")
    row = next(
        p for p in provider_research.snapshot(tmp_path)["providers"] if p["provider"] == provider
    )
    assert row["state"] == "RECORDED_RESEARCH"
    assert row["publication_time_verified"] is row["historical_availability_verified"] is False
    assert row["provider_timestamp"] is None
    assert row["samples"][0]["value"] == rows[0]["value"]
    if provider == "bls":
        assert row["samples"][0]["footnotes"] == rows[0]["footnotes"]
    summary["samples"][0]["value"] = "tampered"
    path.write_text(json.dumps(summary), encoding="utf-8")
    changed = next(
        p for p in provider_research.snapshot(tmp_path)["providers"] if p["provider"] == provider
    )
    assert changed["state"] == "CAPTURE_INVALID"


def test_provider_view_has_discoverable_shared_navigation_link():
    from pathlib import Path

    template = Path(provider_research.__file__).parent / "templates" / "base.html"
    assert '<a href="/research/providers">Provider research captures</a>' in template.read_text(
        encoding="utf-8"
    )
